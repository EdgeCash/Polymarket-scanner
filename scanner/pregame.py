"""The pre-game scan: every sport ESPN posts a book's line for, against Polymarket.

Shadow only. Every PREGAME_SCAN_MINUTES it reads each sport's scoreboard for
yesterday, today and tomorrow (US Eastern), lists the matching Polymarket league,
pairs the games, and for each game still to start compares Polymarket's buy
prices with the book's vig-free probabilities at the same line. A side whose edge
after the fee clears PREGAME_MIN_EDGE is a gap: what this scan would have told the
owner to buy. Gaps are written to the diary at the first price seen, kept up to
date while they persist, given the book's closing line once the game starts, and
graded from the final score. Nothing here sends anything or counts against a cap.

Runs in its own thread with its own, slower request ceiling, so the live
football scanner never waits for it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from scanner.books import SPORTS, BookFeed, BookFeedError, BookLine, PregameEvent, Sport
from scanner.config import PREGAME_GRADE_DELAY_HOURS, PREGAME_MAX_RPS, Settings
from scanner.diary import OUTCOME_LOSS, OUTCOME_NOT_GRADED, OUTCOME_PUSH, OUTCOME_WIN, Diary
from scanner.fees import fee_per_contract
from scanner.matching import Match, match_games
from scanner.models import PregameGap, PregameLineRecord, Side
from scanner.polymarket import PolymarketError, PolymarketReader, short_error
from scanner.web import RuntimeStatus

log = logging.getLogger(__name__)

ESPN_ZONE = ZoneInfo("America/New_York")
MAX_EXTRA_FETCHES = 5  # scoreboards fetched per scan just to grade old gaps


@dataclass
class PregameSummary:
    sports: list[str] = field(default_factory=list)
    matched: int = 0
    unmatched: int = 0
    with_lines: int = 0
    gaps_new: int = 0
    gaps_updated: int = 0
    closed: int = 0
    graded: int = 0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"pre-game scan: sports {', '.join(self.sports) or 'none'}; {self.matched} games "
            f"matched ({self.with_lines} with a book line), {self.unmatched} unmatched; "
            f"gaps {self.gaps_new} new, {self.gaps_updated} updated; {self.closed} closed, "
            f"{self.graded} graded" + (f"; errors: {'; '.join(self.errors)}" if self.errors else "")
        )


# -- pure decisions ---------------------------------------------------------------


def _gap(
    *,
    match: Match,
    sport: Sport,
    now: datetime,
    market: str,
    pick: str,
    pick_side: str,
    line: float | None,
    market_slug: str,
    side_label: str,
    buy: float | None,
    theta: float,
    book_fair: float,
    book_odds: int | None,
    min_edge: float,
) -> PregameGap | None:
    if buy is None or not 0.0 < buy < 1.0:
        return None
    fee = fee_per_contract(buy, theta)
    edge = book_fair - buy - fee
    if edge < min_edge - 1e-12:
        return None
    event: PregameEvent = match.espn
    return PregameGap(
        created_at=now,
        sport=sport.key,
        feed_id=event.feed_id,
        event_slug=match.polymarket.event_slug,
        start=event.kickoff,
        home=event.home.abbreviation,
        away=event.away.abbreviation,
        market=market,
        pick=pick,
        pick_side=pick_side,
        line=line,
        market_slug=market_slug,
        side_label=side_label,
        buy_price=buy,
        fee=fee,
        book_fair=book_fair,
        book_odds=book_odds,
        edge=edge,
        provider=event.book.provider if event.book else "",
    )


def find_gaps(match: Match, sport: Sport, min_edge: float, now: datetime) -> list[PregameGap]:
    """Every side of this game that Polymarket sells below the book's vig-free number.

    Moneylines only where a draw is impossible; totals and spreads only at the
    book's own line, since a different number is a different question.
    """
    event: PregameEvent = match.espn
    game = match.polymarket
    book = event.book
    if book is None:
        return []
    home_mt, away_mt = match.home_team, match.away_team
    common = dict(match=match, sport=sport, now=now, min_edge=min_edge)
    gaps: list[PregameGap | None] = []

    fair_ml = book.fair_moneyline() if sport.two_way_moneyline else None
    if fair_ml and game.moneyline_slug and game.moneyline_active and not game.moneyline_closed:
        sides = (
            (Side.HOME, home_mt, event.home, fair_ml[0], book.home_ml),
            (Side.AWAY, away_mt, event.away, fair_ml[1], book.away_ml),
        )
        for side, market_team, team, fair_p, odds in sides:
            gaps.append(
                _gap(
                    **common,
                    market="moneyline",
                    pick=team.abbreviation,
                    pick_side=side.value,
                    line=None,
                    market_slug=game.moneyline_slug,
                    side_label="long" if market_team.is_long else "short",
                    buy=market_team.quote,
                    theta=game.moneyline_theta,
                    book_fair=fair_p,
                    book_odds=odds,
                )
            )

    fair_total = book.fair_total()
    if fair_total and book.total is not None:
        for total in game.totals:
            if total.closed or not total.active or abs(total.line - book.total) > 1e-9:
                continue
            over_label = "long" if total.over_is_long else "short"
            under_label = "short" if total.over_is_long else "long"
            gaps.append(
                _gap(
                    **common,
                    market="total",
                    pick=f"OVER {total.line:g}",
                    pick_side="over",
                    line=total.line,
                    market_slug=total.market_slug,
                    side_label=over_label,
                    buy=total.over_quote,
                    theta=total.theta,
                    book_fair=fair_total[0],
                    book_odds=book.over_odds,
                )
            )
            gaps.append(
                _gap(
                    **common,
                    market="total",
                    pick=f"UNDER {total.line:g}",
                    pick_side="under",
                    line=total.line,
                    market_slug=total.market_slug,
                    side_label=under_label,
                    buy=total.under_quote,
                    theta=total.theta,
                    book_fair=fair_total[1],
                    book_odds=book.under_odds,
                )
            )

    fair_spread = book.fair_spread()
    if fair_spread and book.home_spread is not None:
        for spread in game.spreads:
            if spread.closed or not spread.active:
                continue
            if spread.long_team_id == home_mt.team_id and spread.short_team_id == away_mt.team_id:
                long_side = Side.HOME
            elif spread.long_team_id == away_mt.team_id and spread.short_team_id == home_mt.team_id:
                long_side = Side.AWAY
            else:
                continue
            long_book_line = book.home_spread if long_side is Side.HOME else -book.home_spread
            if abs(spread.long_line - long_book_line) > 1e-9:
                continue
            short_side = Side.AWAY if long_side is Side.HOME else Side.HOME
            long_team = event.home if long_side is Side.HOME else event.away
            short_team = event.away if long_side is Side.HOME else event.home
            fair_long = fair_spread[0] if long_side is Side.HOME else fair_spread[1]
            fair_short = fair_spread[1] if long_side is Side.HOME else fair_spread[0]
            odds_long = book.home_spread_odds if long_side is Side.HOME else book.away_spread_odds
            odds_short = book.away_spread_odds if long_side is Side.HOME else book.home_spread_odds
            gaps.append(
                _gap(
                    **common,
                    market="spread",
                    pick=f"{long_team.abbreviation} {spread.long_line:+g}",
                    pick_side=long_side.value,
                    line=spread.long_line,
                    market_slug=spread.market_slug,
                    side_label="long",
                    buy=spread.long_quote,
                    theta=spread.theta,
                    book_fair=fair_long,
                    book_odds=odds_long,
                )
            )
            gaps.append(
                _gap(
                    **common,
                    market="spread",
                    pick=f"{short_team.abbreviation} {-spread.long_line:+g}",
                    pick_side=short_side.value,
                    line=-spread.long_line,
                    market_slug=spread.market_slug,
                    side_label="short",
                    buy=spread.short_quote,
                    theta=spread.theta,
                    book_fair=fair_short,
                    book_odds=odds_short,
                )
            )
    return [g for g in gaps if g is not None]


def line_record(match: Match, sport: Sport, now: datetime) -> PregameLineRecord:
    """The compact reading of a game's lines that the diary keeps when it changes."""
    event: PregameEvent = match.espn
    game = match.polymarket
    book = event.book
    polymarket: dict = {
        "home": match.home_team.quote,
        "away": match.away_team.quote,
    }
    if book is not None and book.total is not None:
        for total in game.totals:
            if abs(total.line - book.total) < 1e-9:
                polymarket["total"] = [total.line, total.over_quote, total.under_quote]
    if book is not None and book.home_spread is not None:
        for spread in game.spreads:
            home_is_long = spread.long_team_id == match.home_team.team_id
            home_line = spread.long_line if home_is_long else -spread.long_line
            if abs(home_line - book.home_spread) < 1e-9:
                home_quote = spread.long_quote if home_is_long else spread.short_quote
                away_quote = spread.short_quote if home_is_long else spread.long_quote
                polymarket["spread"] = [home_line, home_quote, away_quote]
    return PregameLineRecord(
        scanned_at=now,
        sport=sport.key,
        feed_id=event.feed_id,
        event_slug=game.event_slug,
        start=event.kickoff,
        home=event.home.abbreviation,
        away=event.away.abbreviation,
        book=book.as_dict() if book else {},
        polymarket=polymarket,
    )


def closing_for(gap: dict, book: BookLine | None) -> tuple[float | None, int | None]:
    """The book's closing probability and price for a gap's pick, at the same line only."""
    if book is None:
        return None, None
    market, side, line = gap["market"], gap["pick_side"], gap["line"]
    if market == "moneyline":
        fair_ml = book.fair_moneyline()
        if fair_ml is None:
            return None, None
        if side == Side.HOME.value:
            return fair_ml[0], book.home_ml
        return fair_ml[1], book.away_ml
    if market == "total":
        fair_total = book.fair_total()
        if fair_total is None or book.total is None or line is None:
            return None, None
        if abs(book.total - line) > 1e-9:
            return None, None  # the line moved: not the same bet
        if side == "over":
            return fair_total[0], book.over_odds
        return fair_total[1], book.under_odds
    if market == "spread":
        fair_spread = book.fair_spread()
        if fair_spread is None or book.home_spread is None or line is None:
            return None, None
        if side == Side.HOME.value:
            if abs(book.home_spread - line) > 1e-9:
                return None, None
            return fair_spread[0], book.home_spread_odds
        if abs(-book.home_spread - line) > 1e-9:
            return None, None
        return fair_spread[1], book.away_spread_odds
    return None, None


def pregame_outcome(gap: dict, home_score: int, away_score: int) -> tuple[str, float | None]:
    """Win, loss or push for a gap's pick from the final score."""
    market, side, line = gap["market"], gap["pick_side"], gap["line"]
    if market == "moneyline":
        if home_score == away_score:
            return OUTCOME_PUSH, 0.5
        winner = Side.HOME.value if home_score > away_score else Side.AWAY.value
        return (OUTCOME_WIN, 1.0) if side == winner else (OUTCOME_LOSS, 0.0)
    if market == "total":
        if line is None:
            return OUTCOME_NOT_GRADED, None
        points = home_score + away_score
        if abs(points - line) < 1e-9:
            return OUTCOME_PUSH, 0.5
        over = points > line
        won = over if side == "over" else not over
        return (OUTCOME_WIN, 1.0) if won else (OUTCOME_LOSS, 0.0)
    if market == "spread":
        if line is None or side not in (Side.HOME.value, Side.AWAY.value):
            return OUTCOME_NOT_GRADED, None
        mine, theirs = (
            (home_score, away_score) if side == Side.HOME.value else (away_score, home_score)
        )
        margin = mine + line - theirs
        if abs(margin) < 1e-9:
            return OUTCOME_PUSH, 0.5
        return (OUTCOME_WIN, 1.0) if margin > 0 else (OUTCOME_LOSS, 0.0)
    return OUTCOME_NOT_GRADED, None


# -- the scanner -------------------------------------------------------------------


class PregameScanner:
    def __init__(
        self,
        settings: Settings,
        diary: Diary,
        status: RuntimeStatus,
        *,
        reader: PolymarketReader | None = None,
        feed: BookFeed | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        gamelog=None,
    ) -> None:
        self.settings = settings
        self.diary = diary
        self.status = status
        self.gamelog = gamelog  # a FootballLog, refreshed after each scan on this thread
        self.reader = reader or PolymarketReader(max_rps=PREGAME_MAX_RPS)
        self.feed = feed or BookFeed()
        self._now = now
        self.sports: list[Sport] = [SPORTS[k] for k in settings.pregame_sports if k in SPORTS]
        self.leagues: dict[str, str] = {}  # sport key -> Polymarket league slug
        self.discovered = False
        self.stop_event = threading.Event()

    # -- plumbing ---------------------------------------------------------------

    def discover(self) -> dict[str, str]:
        """The Polymarket league slug for each configured sport, by slug, name or abbreviation."""
        leagues = self.reader.list_leagues()
        found: dict[str, str] = {}
        for sport in self.sports:
            wanted = set(sport.polymarket)
            for league in leagues:
                names = {
                    str(league.get(key) or "").strip().lower()
                    for key in ("slug", "name", "abbreviation")
                }
                if names & wanted and league.get("slug"):
                    found[sport.key] = str(league["slug"])
                    break
        missing = [s.key for s in self.sports if s.key not in found]
        if missing:
            log.info("pre-game: Polymarket lists no league for %s; skipped", missing)
        self.leagues = found
        self.discovered = True
        return found

    def dates(self, now: datetime) -> list[str]:
        """Yesterday, today and tomorrow in US Eastern, the way ESPN counts days."""
        today = now.astimezone(ESPN_ZONE).date()
        return [(today + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)]

    def _fetch_events(self, sport: Sport, dates: list[str]) -> list[PregameEvent]:
        seen: dict[str, PregameEvent] = {}
        for date in dates:
            for event in self.feed.fetch(sport, date):
                seen[event.feed_id] = event
        return list(seen.values())

    # -- one scan ---------------------------------------------------------------

    def scan_once(self, now: datetime | None = None) -> PregameSummary:
        now = now or self._now()
        summary = PregameSummary()
        if not self.discovered:
            try:
                self.discover()
            except PolymarketError as exc:
                summary.errors.append(f"leagues: {short_error(str(exc))}")
                self._update_status(now, summary)
                return summary
        events_by_sport: dict[str, list[PregameEvent]] = {}
        dates = self.dates(now)
        for sport in self.sports:
            slug = self.leagues.get(sport.key)
            if slug is None:
                continue
            try:
                events = self._fetch_events(sport, dates)
            except BookFeedError as exc:
                summary.errors.append(short_error(str(exc)))
                continue
            events_by_sport[sport.key] = events
            try:
                games = self.reader.list_games(sport.league, slug)
            except PolymarketError as exc:
                summary.errors.append(f"{sport.key}: {short_error(str(exc))}")
                continue
            summary.sports.append(sport.key)
            self._scan_sport(sport, games, events, now, summary)
        self._close_gaps(now, summary)
        self._grade_gaps(events_by_sport, now, summary)
        self._update_status(now, summary)
        log.info(summary.describe())
        return summary

    def _scan_sport(self, sport, games, events, now, summary) -> None:
        horizon = now + timedelta(hours=self.settings.PREGAME_HORIZON_HOURS)
        upcoming = [
            e for e in events if e.status == "pre" and e.kickoff and now <= e.kickoff <= horizon
        ]
        listed = [
            g
            for g in games
            if g.start_time
            and now <= g.start_time <= horizon
            and not g.ended
            and g.live is not True
        ]
        result = match_games(listed, upcoming, nicknames=True)
        summary.matched += len(result.matches)
        summary.unmatched += len(result.unmatched_polymarket)
        for match in result.matches:
            if match.espn.book is None:
                continue
            summary.with_lines += 1
            self.diary.record_pregame_line(line_record(match, sport, now))
            for gap in find_gaps(match, sport, self.settings.PREGAME_MIN_EDGE, now):
                if self.diary.upsert_pregame_gap(gap, now) == "new":
                    summary.gaps_new += 1
                    log.info(
                        "pre-game gap: %s %s at %s: %s buy %.3f book %.3f edge %+.3f",
                        gap.sport,
                        gap.away,
                        gap.home,
                        gap.pick,
                        gap.buy_price,
                        gap.book_fair,
                        gap.edge,
                    )
                else:
                    summary.gaps_updated += 1

    def _close_gaps(self, now: datetime, summary: PregameSummary) -> None:
        """Give every gap whose game has started the book's last line before it did."""
        for gap in self.diary.pregame_gaps_unclosed(now):
            start = datetime.fromisoformat(gap["start_time"])
            last = self.diary.last_pregame_line(
                gap["sport"], gap["feed_id"], before=start + timedelta(minutes=10)
            )
            book = BookLine.from_dict(last["book"]) if last else None
            closing_fair, closing_odds = closing_for(gap, book)
            clv = None
            if closing_fair is not None:
                clv = closing_fair - gap["buy_price"] - gap["fee"]
            self.diary.set_pregame_closing(gap["id"], closing_fair, closing_odds, clv, now)
            summary.closed += 1

    def _grade_gaps(self, events_by_sport, now: datetime, summary: PregameSummary) -> None:
        due = now - timedelta(hours=PREGAME_GRADE_DELAY_HOURS)
        index = {
            (key, event.feed_id): event
            for key, events in events_by_sport.items()
            for event in events
        }
        extra_fetches = 0
        for gap in self.diary.pregame_gaps_ungraded(due):
            key = (gap["sport"], gap["feed_id"])
            if key not in index and extra_fetches < MAX_EXTRA_FETCHES:
                # An older game is no longer on the three-day window: fetch its day.
                sport = SPORTS.get(gap["sport"])
                start = datetime.fromisoformat(gap["start_time"])
                if sport is not None:
                    extra_fetches += 1
                    try:
                        for event in self.feed.fetch(
                            sport, start.astimezone(ESPN_ZONE).strftime("%Y%m%d")
                        ):
                            index.setdefault((sport.key, event.feed_id), event)
                    except BookFeedError as exc:
                        summary.errors.append(short_error(str(exc)))
            event = index.get(key)
            if event is None:
                continue
            if event.status == "postponed":
                self.diary.grade_pregame_gap(gap["id"], OUTCOME_NOT_GRADED, None, None, None, now)
                summary.graded += 1
            elif (
                event.status == "final"
                and event.home_score is not None
                and event.away_score is not None
            ):
                outcome, settlement = pregame_outcome(gap, event.home_score, event.away_score)
                self.diary.grade_pregame_gap(
                    gap["id"], outcome, settlement, event.home_score, event.away_score, now
                )
                summary.graded += 1

    def _update_status(self, now: datetime, summary: PregameSummary) -> None:
        self.status.pregame = {
            "last_scan": now.isoformat(),
            "sports": list(summary.sports),
            "matched": summary.matched,
            "unmatched": summary.unmatched,
            "with_lines": summary.with_lines,
            "gaps_new": summary.gaps_new,
            "open_gaps": self.diary.open_pregame_gap_count(),
            "error": "; ".join(summary.errors[:2]) or None,
        }

    # -- the thread ---------------------------------------------------------------

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            if self.settings.PREGAME_ENABLED:
                try:
                    self.scan_once()
                except Exception as exc:  # the thread must survive anything
                    log.exception("pre-game scan failed")
                    self.status.pregame = {
                        **(self.status.pregame or {}),
                        "error": short_error(f"{type(exc).__name__}: {exc}"),
                    }
            if self.gamelog is not None:
                self.refresh_gamelog()
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(30.0, self.settings.PREGAME_SCAN_MINUTES * 60 - elapsed))

    def refresh_gamelog(self) -> None:
        """Bring the football game log up to date and note the result on the status page."""
        now = self._now()
        try:
            summaries = self.gamelog.refresh_all(now)
        except Exception as exc:  # the thread must survive anything
            log.exception("game log refresh failed")
            self.status.gamelog = {
                **(self.status.gamelog or {}),
                "error": short_error(f"{type(exc).__name__}: {exc}"),
            }
            return
        errors = [e for s in summaries for e in s.errors]
        self.status.gamelog = {
            "last_refresh": now.isoformat(),
            "games": self.diary.football_game_counts(),
            "upcoming": {s.sport: s.upcoming for s in summaries},
            "backlog": sum(s.backlog for s in summaries),
            "fetched": sum(s.summaries_fetched for s in summaries),
            "error": "; ".join(short_error(e) for e in errors[:2]) or None,
        }
        log.info(
            "game log: %s games, %d fetched, %d still to fetch",
            self.status.gamelog["games"],
            self.status.gamelog["fetched"],
            self.status.gamelog["backlog"],
        )

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="pregame", daemon=True)
        thread.start()
        return thread
