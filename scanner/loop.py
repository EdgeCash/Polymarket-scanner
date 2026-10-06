"""The scan loop and the game-window scheduler.

One pass: refresh the game list (every 60 s), read both ESPN scoreboards,
match games, grade finished ones, find candidates, read prices only for
those, apply the rules, send and record. The loop sleeps when no game is near
and wakes 30 minutes before the first kickoff.

Everything that touches the network is behind an injectable object so the
tests drive the loop with fixtures and a fake clock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from scanner.config import (
    CLINCHED_REPOLL_SECONDS,
    FEED_FAILURE_ALERT_SECONDS,
    FEED_FAILURE_REPEAT_SECONDS,
    GAME_LIST_REFRESH_SECONDS,
    OBSERVATION_INTERVAL_SECONDS,
    PERIOD_READS_PER_PASS,
    PERIOD_REPOLL_SECONDS,
    WAKE_BEFORE_KICKOFF_SECONDS,
    Settings,
)
from scanner.diary import Diary
from scanner.fees import fee_per_contract
from scanner.matching import Match, match_games
from scanner.models import (
    Alert,
    AlertType,
    Book,
    GameState,
    GameStatus,
    League,
    NearMiss,
    Observation,
    PeriodMarket,
    PolymarketGame,
    Quote,
    Side,
)
from scanner.notify import (
    ZONE_LABELS,
    Notifier,
    feed_failure_message,
    heartbeat_message,
    paused_message,
)
from scanner.periods import PeriodDecision, PeriodSides, decide, last_period, period_sides
from scanner.polymarket import PolymarketError, PolymarketReader, RateLimited, short_error
from scanner.rules import (
    AlertHistory,
    Decision,
    evaluate_clinched,
    evaluate_period,
    evaluate_winner,
    pick_clinched,
)
from scanner.scores import ScoreFeed, ScoreFeedError
from scanner.tracker import GameTracker
from scanner.web import RuntimeStatus
from scanner.winprob import WinProbModel, fair_price

log = logging.getLogger(__name__)

MAX_GAME_HOURS = 5.0  # a game older than this with no final is treated as over
PRE_KICKOFF_POLL_SECONDS = 60.0  # score polling cadence before any game is live
ASLEEP_REFRESH_SECONDS = 15 * 60  # how often the game list is re-read while asleep
NEAR_MISS_REPEAT_SECONDS = 60.0  # the same near miss is written at most this often
MAX_SLEEP_SECONDS = 60.0  # the loop never sleeps longer than this in one go


# A market whose BBO state says one of these is settled; it is not read again.
SETTLED_STATE_WORDS = ("CLOSED", "RESOLVED", "SETTLED")


@dataclass(frozen=True, slots=True)
class PeriodCandidate:
    """A decided quarter or half market that is due for a price read."""

    match: Match
    market: PeriodMarket
    sides: PeriodSides
    decision: PeriodDecision
    last_read: datetime | None


@dataclass
class PassSummary:
    alerts: list[Alert] = field(default_factory=list)
    near_misses: list[NearMiss] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    candidates: int = 0
    live_games: int = 0
    matched: int = 0


class Scanner:
    def __init__(
        self,
        settings: Settings,
        *,
        reader: PolymarketReader | None = None,
        feed: ScoreFeed | None = None,
        notifier: Notifier | None = None,
        diary: Diary | None = None,
        model: WinProbModel | None = None,
        status: RuntimeStatus | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.reader = reader or PolymarketReader()
        self.feed = feed or ScoreFeed()
        self.notifier = notifier or Notifier(settings)
        self.diary = diary or Diary(settings.DATABASE_PATH)
        self.model = model or WinProbModel.load()
        self.status = status or RuntimeStatus()
        self._now = now
        self._sleep = sleep
        self.tracker = GameTracker()
        self.history = AlertHistory(settings.TZ)
        # Quarter and half alerts keep their own repeat gap and daily cap, so a
        # shadow-only period alert can never use up the winner alerts' cap.
        self.period_history = AlertHistory(settings.TZ)
        # The observation window runs the winner rules with a wider time filter.
        self.observe_settings = settings.model_copy(
            update={
                "MAX_MINUTES_LEFT": max(
                    settings.OBSERVATION_MINUTES_LEFT, settings.MAX_MINUTES_LEFT
                )
            }
        )
        self.leagues: dict[League, str] = {}
        self.games: dict[League, list[PolymarketGame]] = {}
        self.games_refreshed_at: datetime | None = None
        self.last_scores: dict[League, list[GameState]] = {}
        self.score_fail_since: datetime | None = None
        self.price_fail_since: datetime | None = None
        self.feed_message_at: dict[str, datetime] = {}
        self.heartbeat_day = None
        self.finished: set[tuple[League, str]] = set()
        self.clinch_reads: dict[str, tuple[datetime, int]] = {}  # slug -> (read at, points)
        self.period_reads: dict[str, tuple[datetime, tuple]] = {}  # slug -> (read at, decision)
        self.period_done: set[str] = set()  # period markets seen settled; not read again
        self.observe_reads: dict[tuple[League, str], datetime] = {}  # game -> last observation
        self.stop_event = threading.Event()

    # -- startup ------------------------------------------------------------

    def start(self) -> None:
        wanted = {League(name) for name in self.settings.leagues}
        found = self.reader.discover_leagues()
        self.leagues = {lg: slug for lg, slug in found.items() if lg in wanted}
        missing = wanted - set(self.leagues)
        if missing:
            raise PolymarketError(f"leagues not available on Polymarket: {sorted(missing)}")
        restored = self.diary.restore_history(self.history, self._now(), self.period_history)
        self.status.leagues = {lg.value: slug for lg, slug in self.leagues.items()}
        self.status.alerts_today = self.history.count_today(self._now())
        log.info("started: leagues %s, %d alerts restored from the diary", self.leagues, restored)

    # -- the game window ----------------------------------------------------

    def window(self, now: datetime) -> tuple[bool, str, datetime | None]:
        """(awake?, note, when this answer may change)."""
        upcoming: list[datetime] = []
        for league, games in self.games.items():
            for game in games:
                if game.start_time is None or game.ended:
                    continue
                key = (league, game.event_slug)
                if key in self.finished:
                    continue
                wake_at = game.start_time - timedelta(seconds=WAKE_BEFORE_KICKOFF_SECONDS)
                over_at = game.start_time + timedelta(hours=MAX_GAME_HOURS)
                if wake_at <= now < over_at:
                    return True, f"{game.event_slug} in window", over_at
                if wake_at > now:
                    upcoming.append(wake_at)
        if upcoming:
            next_wake = min(upcoming)
            return False, f"next wake {self._local(next_wake)}", next_wake
        return False, "no games listed", None

    def _local(self, moment: datetime) -> str:
        """A moment in the owner's time zone, the way the status page shows times."""
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo(self.settings.TZ))
        label = ZONE_LABELS.get(self.settings.TZ) or local.tzname() or self.settings.TZ
        return f"{local.strftime('%a %-I:%M %p')} {label}"

    # -- helpers ------------------------------------------------------------

    def _refresh_games(self, now: datetime, force: bool = False) -> None:
        age = (
            None
            if self.games_refreshed_at is None
            else (now - self.games_refreshed_at).total_seconds()
        )
        if not force and age is not None and age < GAME_LIST_REFRESH_SECONDS:
            return
        try:
            for league, slug in self.leagues.items():
                self.games[league] = self.reader.list_games(league, slug)
            self.games_refreshed_at = now
            self._price_ok(now)
            self.status.games_watched = sum(len(g) for g in self.games.values())
        except PolymarketError as exc:
            self._price_failed(now, f"game list: {exc}")
        except Exception as exc:  # a surprise must not take the scanner down
            log.exception("game list refresh failed")
            self._price_failed(now, f"game list: {type(exc).__name__}: {exc}")

    def _price_failed(self, now: datetime, detail: str) -> None:
        if self.price_fail_since is None:
            self.price_fail_since = now
            self.diary.log_event("price_feed_failure", detail, now)
        self.status.price_feed_failing_since = self.price_fail_since.isoformat()
        self.status.last_error = short_error(detail)
        log.warning("price feed: %s", short_error(detail))

    def _price_ok(self, now: datetime) -> None:
        if self.price_fail_since is not None:
            self.diary.log_event("price_feed_recovered", "", now)
        self.price_fail_since = None
        self.status.price_feed_failing_since = None
        self.status.last_price_poll = now.isoformat()

    def _fetch_scores(self, now: datetime, leagues: list[League]) -> None:
        failed = False
        for league in leagues:
            try:
                states = self.feed.fetch(league)
            except ScoreFeedError as exc:
                failed = True
                self.status.last_error = short_error(str(exc))
                log.warning("score feed: %s", short_error(str(exc)))
                continue
            except Exception as exc:  # malformed data the parser did not expect
                failed = True
                self.status.last_error = short_error(f"{type(exc).__name__}: {exc}")
                log.exception("score feed %s failed unexpectedly", league.value)
                continue
            self.last_scores[league] = states
            self.tracker.update_all(states)
            self.status.last_score_poll[league.value] = now.isoformat()
        if failed:
            if self.score_fail_since is None:
                self.score_fail_since = now
                self.diary.log_event("score_feed_failure", "", now)
            self.status.score_feed_failing_since = self.score_fail_since.isoformat()
        else:
            if self.score_fail_since is not None:
                self.diary.log_event("score_feed_recovered", "", now)
            self.score_fail_since = None
            self.status.score_feed_failing_since = None

    def _feed_messages(self, now: datetime) -> None:
        for name, since in (("score", self.score_fail_since), ("price", self.price_fail_since)):
            if since is None:
                continue
            if (now - since).total_seconds() < FEED_FAILURE_ALERT_SECONDS:
                continue
            last = self.feed_message_at.get(name)
            if last is not None and (now - last).total_seconds() < FEED_FAILURE_REPEAT_SECONDS:
                continue
            self.feed_message_at[name] = now
            self.notifier.send_system(feed_failure_message(name, since, self.settings.TZ))

    def _heartbeat(self, now: datetime, games_today: int) -> None:
        today = self.history.local_date(now)
        if self.heartbeat_day == today:
            return
        self.heartbeat_day = today
        self.diary.log_event("heartbeat", heartbeat_message(games_today), now)
        self.notifier.send_system(heartbeat_message(games_today))

    def _quotes(self, game: PolymarketGame) -> dict[str, Quote]:
        return self.reader.quotes_for_game(game)

    def _book(self, slug: str) -> Book:
        return self.reader.book(slug)

    def _record_near_miss(self, miss: NearMiss) -> None:
        last = self.diary.last_near_miss(miss.league, miss.feed_id, miss.pick)
        if last is not None and last["reason"] == miss.reason:
            last_at = datetime.fromisoformat(last["created_at"])
            if (miss.created_at - last_at).total_seconds() < NEAR_MISS_REPEAT_SECONDS:
                return
        self.diary.record_near_miss(miss)

    def _deliver(self, decision: Decision, now: datetime, summary: PassSummary) -> None:
        if decision.alert is not None:
            alert = decision.alert
            sent = self.notifier.send_alert(alert)
            self.diary.record_alert(alert, sent)
            is_period = alert.alert_type is AlertType.PERIOD
            (self.period_history if is_period else self.history).record(alert)
            summary.alerts.append(alert)
            self.status.alerts_today = self.history.count_today(now)
            self.status.last_alert_at = now.isoformat()
            log.info(
                "ALERT %s %s %s edge %.3f%s",
                alert.league.value,
                alert.event_slug,
                alert.pick,
                alert.edge,
                "" if sent else " (not sent)",
            )
            if not is_period and self.history.paused_message_due(
                now, self.settings.MAX_ALERTS_PER_DAY
            ):
                self.diary.log_event(
                    "paused", paused_message(self.settings.MAX_ALERTS_PER_DAY), now
                )
                self.notifier.send_system(paused_message(self.settings.MAX_ALERTS_PER_DAY))
        elif decision.near_miss is not None:
            self._record_near_miss(decision.near_miss)
            summary.near_misses.append(decision.near_miss)

    # -- candidates ---------------------------------------------------------

    def _winner_candidates(self, match: Match, state: GameState) -> list[tuple[Side, object]]:
        """Sides worth a price read: late, live, and with a fair price near the bar."""
        if state.status is not GameStatus.LIVE or state.is_overtime or state.period != 4:
            return []
        if state.seconds_left is None or state.seconds_left > self.settings.MAX_MINUTES_LEFT * 60:
            return []
        if not match.polymarket.moneyline_slug:
            return []
        out = []
        for side in (Side.HOME, Side.AWAY):
            fair = fair_price(state, side, self.settings, self.model)
            best = fair.fair if fair.fair is not None else fair.model_price
            if best is not None and best >= self.settings.MIN_FAIR:
                out.append((side, fair))
        return out

    def _evaluate_winner(
        self, match: Match, state: GameState, side: Side, fair, quote: Quote, now: datetime
    ) -> Decision:
        common = dict(
            match=match,
            state=state,
            side=side,
            fair=fair,
            quote=quote,
            settings=self.settings,
            tracker=self.tracker,
            history=self.history,
            now=now,
        )
        decision = evaluate_winner(book=None, **common)
        if decision.reason == "rule 4: no book":
            book = self._book(match.polymarket.moneyline_slug or "")
            common["now"] = self._now()
            decision = evaluate_winner(book=book, **common)
        return decision

    def _clinched_decisions(self, match: Match, state: GameState, now: datetime) -> list[Decision]:
        if not self.settings.CLINCHED_OVERS_ENABLED:
            return []
        if state.status not in (GameStatus.LIVE, GameStatus.HALFTIME) or state.total_points is None:
            return []
        age = self.tracker.seconds_since_score_change(state, now)
        if age is None or age < self.settings.CLINCH_COOLDOWN_SECONDS:
            return []  # nothing to read yet; the rules would only say "cooldown"
        decisions = []
        for total in match.polymarket.totals:
            if state.total_points <= total.line or total.closed or not total.active:
                continue
            # A clinched line is priced at 99 cents within seconds and stays there.
            # Re-read it once a minute, or sooner only after the score changed; a
            # 60-point game otherwise means two dozen reads every pass.
            last = self.clinch_reads.get(total.market_slug)
            if last is not None:
                read_at, points = last
                if (
                    points == state.total_points
                    and (now - read_at).total_seconds() < CLINCHED_REPOLL_SECONDS
                ):
                    continue
            self.clinch_reads[total.market_slug] = (now, state.total_points)
            quote = self.reader.over_quote(total)
            common = dict(
                match=match,
                state=state,
                total=total,
                quote=quote,
                settings=self.settings,
                tracker=self.tracker,
                history=self.history,
                now=now,
            )
            decision = evaluate_clinched(book=None, **common)
            if decision.reason == "rule 4: no book":
                book = self._book(total.market_slug)
                common["now"] = self._now()
                decision = evaluate_clinched(book=book, **common)
            decisions.append(decision)
        return decisions

    def _period_candidates(
        self, match: Match, state: GameState, now: datetime
    ) -> list[PeriodCandidate]:
        """Quarter and half markets of one game whose result is settled and are due a read."""
        if state.status not in (GameStatus.LIVE, GameStatus.HALFTIME):
            return []
        if not state.home_linescores or not state.away_linescores:
            return []
        out = []
        for market in match.polymarket.period_markets:
            if market.closed or not market.active or market.market_slug in self.period_done:
                continue
            sides = period_sides(match, market)
            decided = decide(market, state, sides)
            if decided is None:
                continue
            if decided.by_period_end:
                age = self.tracker.seconds_since_period_completed(state, last_period(market), now)
            else:
                age = self.tracker.seconds_since_score_change(state, now)
            if age is None or age < self.settings.CLINCH_COOLDOWN_SECONDS:
                continue  # nothing to read yet; the rules would only say "cooldown"
            # Decided markets are priced near a dollar within seconds and stay there:
            # read each one at most every couple of minutes, or sooner only if the
            # decision itself changed (a later period, more points).
            token = (decided.side_label, decided.points, state.completed_periods)
            last = self.period_reads.get(market.market_slug)
            last_read = None
            if last is not None:
                read_at, last_token = last
                if last_token == token:
                    if (now - read_at).total_seconds() < PERIOD_REPOLL_SECONDS:
                        continue
                    last_read = read_at
            out.append(PeriodCandidate(match, market, sides, decided, last_read))
        return out

    def _period_pass(self, live: list[Match], now: datetime, summary: PassSummary) -> None:
        """Read a few decided period markets this pass, the closest calls first.

        A game can carry a hundred decided lines at halftime, so they are rationed:
        PERIOD_READS_PER_PASS per pass across all games, never-read markets before
        re-reads, and among those the narrowest margins first, since a line the
        result only just cleared is the one a slow market is most likely to misprice.
        """
        if not self.settings.PERIOD_MARKETS_ENABLED:
            return
        candidates: list[PeriodCandidate] = []
        for match in live:
            candidates.extend(self._period_candidates(match, match.espn, now))
        candidates.sort(
            key=lambda c: (c.last_read is not None, c.last_read or now, c.decision.margin)
        )
        for cand in candidates[:PERIOD_READS_PER_PASS]:
            state = cand.match.espn
            token = (cand.decision.side_label, cand.decision.points, state.completed_periods)
            self.period_reads[cand.market.market_slug] = (now, token)
            quote = self.reader.side_quotes(cand.market)[cand.decision.side_label]
            if quote.state and any(word in quote.state for word in SETTLED_STATE_WORDS):
                self.period_done.add(cand.market.market_slug)
            common = dict(
                match=cand.match,
                state=state,
                market=cand.market,
                decision=cand.decision,
                sides=cand.sides,
                quote=quote,
                settings=self.settings,
                tracker=self.tracker,
                history=self.period_history,
                now=now,
            )
            decision = evaluate_period(book=None, **common)
            if decision.reason == "rule 4: no book":
                book = self._book(cand.market.market_slug)
                common["now"] = self._now()
                decision = evaluate_period(book=book, **common)
            summary.candidates += 1
            self._deliver(decision, now, summary)

    def _observe_winners(
        self, match: Match, state: GameState, now: datetime, summary: PassSummary
    ) -> None:
        """Run the winner rules just outside the late-game filter and only record the answer.

        Nothing here is sent, counted against a cap or remembered as an alert; the
        diary keeps what the rules would have done with more time left so the owner
        can judge the filter on data.
        """
        low = self.settings.MAX_MINUTES_LEFT * 60
        high = self.settings.OBSERVATION_MINUTES_LEFT * 60
        if high <= low:
            return
        if state.status is not GameStatus.LIVE or state.is_overtime or state.period != 4:
            return
        if state.seconds_left is None or not (low < state.seconds_left <= high):
            return
        if not match.polymarket.moneyline_slug:
            return
        key = (state.league, state.feed_id)
        last = self.observe_reads.get(key)
        if last is not None and (now - last).total_seconds() < OBSERVATION_INTERVAL_SECONDS:
            return
        sides = []
        for side in (Side.HOME, Side.AWAY):
            fair = fair_price(state, side, self.settings, self.model)
            best = fair.fair if fair.fair is not None else fair.model_price
            if best is not None and best >= self.settings.MIN_FAIR:
                sides.append((side, fair))
        if not sides:
            return
        self.observe_reads[key] = now
        quotes = self._quotes(match.polymarket)
        for side, fair in sides:
            team = match.market_team_for(side)
            quote = quotes.get(team.abbreviation)
            if quote is None:
                continue
            common = dict(
                match=match,
                state=state,
                side=side,
                fair=fair,
                quote=quote,
                settings=self.observe_settings,
                tracker=self.tracker,
                history=AlertHistory(self.settings.TZ),  # no cap, no repeat gap: pure rules
                now=now,
            )
            decision = evaluate_winner(book=None, **common)
            if decision.reason == "rule 4: no book":
                book = self._book(match.polymarket.moneyline_slug or "")
                common["now"] = self._now()
                decision = evaluate_winner(book=book, **common)
            alert, miss = decision.alert, decision.near_miss
            edge = alert.edge if alert else (miss.edge if miss else None)
            buy = quote.buy_price
            observation = Observation(
                created_at=now,
                league=state.league,
                feed_id=state.feed_id,
                event_slug=match.polymarket.event_slug,
                pick=team.abbreviation,
                pick_side=side.value,
                minutes_left=state.seconds_left / 60.0,
                home_score=state.home_score,
                away_score=state.away_score,
                fair_price=fair.fair,
                model_price=fair.model_price,
                espn_price=fair.espn_price,
                buy_price=buy,
                fee=None if buy is None else fee_per_contract(buy, quote.theta),
                edge=edge,
                dollars_available=alert.dollars_available if alert else None,
                would_alert=decision.fired,
                reason=decision.reason,
            )
            self.diary.record_observation(observation)
            summary.observations.append(observation)

    # -- one pass -----------------------------------------------------------

    def scan_once(self, now: datetime | None = None) -> PassSummary:
        now = now or self._now()
        summary = PassSummary()
        self._refresh_games(now)
        leagues = [lg for lg in self.leagues if self.games.get(lg)]
        self._fetch_scores(now, leagues)
        now = self._now()  # the reads above took time; rules measure from here

        matches: list[Match] = []
        unmatched = 0
        for league in leagues:
            states = self.last_scores.get(league, [])
            result = match_games(self.games.get(league, []), states)
            matches.extend(result.matches)
            unmatched += len(result.unmatched_polymarket)
        self.status.unmatched_polymarket = unmatched
        summary.matched = len(matches)

        # Grade finished games and remember them so the window closes.
        ungraded = self.diary.ungraded_games()
        for match in matches:
            state = match.espn
            if state.status in (GameStatus.FINAL, GameStatus.POSTPONED):
                self.finished.add((match.polymarket.league, match.polymarket.event_slug))
                if (state.league.value, state.feed_id) in ungraded:
                    graded = self.diary.grade_game(state, now)
                    if graded:
                        log.info("graded %d alert(s) on %s", graded, match.polymarket.event_slug)

        live = [m for m in matches if m.espn.status in (GameStatus.LIVE, GameStatus.HALFTIME)]
        summary.live_games = len(live)
        for match in live:
            state = match.espn
            try:
                sides = self._winner_candidates(match, state)
                if sides:
                    summary.candidates += len(sides)
                    quotes = self._quotes(match.polymarket)
                    for side, fair in sides:
                        team = match.market_team_for(side)
                        quote = quotes.get(team.abbreviation)
                        if quote is None:
                            continue
                        self._deliver(
                            self._evaluate_winner(match, state, side, fair, quote, now),
                            now,
                            summary,
                        )
                decisions = self._clinched_decisions(match, state, now)
                if decisions:
                    summary.candidates += len(decisions)
                    best = pick_clinched(decisions)
                    if best is not None:
                        self._deliver(best, now, summary)
                    for decision in decisions:
                        if decision is not best and decision.near_miss is not None:
                            self._deliver(decision, now, summary)
                self._observe_winners(match, state, now, summary)
                self._price_ok(now)
            except RateLimited as exc:
                self._price_failed(now, f"rate limited: {exc}")
                summary.errors.append(str(exc))
                break
            except PolymarketError as exc:
                self._price_failed(now, str(exc))
                summary.errors.append(str(exc))
        if not summary.errors:
            try:
                self._period_pass(live, now, summary)
            except RateLimited as exc:
                self._price_failed(now, f"rate limited: {exc}")
                summary.errors.append(str(exc))
            except PolymarketError as exc:
                self._price_failed(now, str(exc))
                summary.errors.append(str(exc))

        self._followups(now)
        self._feed_messages(now)
        self.status.live_games = len(live)
        self.status.candidates = summary.candidates
        return summary

    def _followups(self, now: datetime) -> None:
        for due in self.diary.pending_followups(now):
            price: float | None = None
            try:
                quote = self.reader.quote(due.market_slug, due.side_label == "long", 0.0)
                price = quote.buy_price
            except PolymarketError as exc:
                log.info("follow-up read failed for alert %d: %s", due.alert_id, exc)
            self.diary.record_followup(due.alert_id, due.seconds, price, now)

    # -- the loop -----------------------------------------------------------

    def run_forever(self, once: bool = False) -> int:
        while not self.leagues:
            try:
                self.start()
            except Exception as exc:  # keep retrying; the web pages stay up meanwhile
                self.status.last_error = short_error(f"startup: {type(exc).__name__}: {exc}")
                if once:
                    log.error("cannot start: %s", exc)
                    return 1
                log.error("cannot start (%s), retrying in 60s", exc)
                self._sleep(60)
        if self.settings.SEND_TEST_MESSAGE_ON_START:
            log.info("SEND_TEST_MESSAGE_ON_START is set: sending the owner's test message")
            self.notifier.send_test_message()
        self._refresh_games(self._now(), force=True)
        while not self.stop_event.is_set():
            now = self._now()
            awake, note, change_at = self.window(now)
            self.status.awake = awake
            self.status.window_note = note
            if awake:
                games_today = sum(
                    1
                    for games in self.games.values()
                    for g in games
                    if g.start_time and abs((g.start_time - now).total_seconds()) < 12 * 3600
                )
                self._heartbeat(now, games_today)
                started = time.monotonic()
                try:
                    summary = self.scan_once(now)
                except Exception as exc:  # the loop must survive anything
                    log.exception("pass failed")
                    self.status.last_error = short_error(f"{type(exc).__name__}: {exc}")
                    summary = PassSummary(errors=[str(exc)])
                if once:
                    return 0 if not summary.errors else 1
                live = summary.live_games > 0
                cadence = self.settings.SCORE_POLL_SECONDS if live else PRE_KICKOFF_POLL_SECONDS
                elapsed = time.monotonic() - started
                self._sleep(max(0.5, cadence - elapsed))
            else:
                if once:
                    log.info("asleep: %s", note)
                    return 0
                self._refresh_games(now, force=self._stale_for(ASLEEP_REFRESH_SECONDS, now))
                wait = MAX_SLEEP_SECONDS
                if change_at is not None:
                    wait = min(wait, max(1.0, (change_at - now).total_seconds()))
                self._sleep(wait)
        return 0

    def _stale_for(self, seconds: float, now: datetime) -> bool:
        if self.games_refreshed_at is None:
            return True
        return (now - self.games_refreshed_at).total_seconds() >= seconds


def serve_web(settings: Settings, diary: Diary, status: RuntimeStatus) -> threading.Thread:
    """Start the status pages in a background thread."""
    import uvicorn

    from scanner.web import create_app

    config = uvicorn.Config(
        create_app(settings, diary, status), host="0.0.0.0", port=settings.PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="web", daemon=True)
    thread.start()
    return thread


def run(settings: Settings, once: bool = False) -> int:
    diary = Diary(settings.DATABASE_PATH)
    status = RuntimeStatus()
    if not once:
        serve_web(settings, diary, status)
        if settings.PREGAME_ENABLED:
            from scanner.pregame import PregameScanner

            PregameScanner(settings, diary, status).start_thread()
    scanner = Scanner(settings, diary=diary, status=status)
    try:
        return scanner.run_forever(once=once)
    except KeyboardInterrupt:
        log.info("stopping")
        return 0
