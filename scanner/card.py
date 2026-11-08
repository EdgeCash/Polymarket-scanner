"""The daily card: the model's few highest-conviction sides, chosen by rule, locked, graded.

Once a day, at a fixed time, every projected game in the next day is ranked by
its edge: the model's chance that the side comes in, from the same score grid
the simulation pages draw (key numbers, overtime and all, and given the line
does not push, since a push is a refund), less the chance the book's price
needs to break even (52.4% at -110). That puts every market and every sport on
one scale, and it knows that half a point across football's 3 is worth more
than half a point across 11. The edge is weighted by how often that sport's
closing line has moved the model's way so far. One side per game, the stronger
of its spread (moneyline in hockey) and its total; the top few make the card.
The lines are the book's at that moment; nothing is chosen by hand and nothing
is changed afterwards. Each pick is settled at the final and the whole record,
rank by rank, is public on the card page.

Conviction is not accuracy. The biggest edge over the book is as likely a
missing quarterback as an edge, which is why the ranking also leans on the
closing line and why the record by rank is kept: it is the only thing that can
say whether the top of the card deserves more trust than the bottom. Nothing
here is sent or placed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scanner.books import BookLine
from scanner.config import (
    CARD_LATE_MINUTES,
    CARD_TRUST_MIN_CLOSED,
    CARD_WINDOW_HOURS,
    SIM_DRAWS,
    Settings,
)
from scanner.grid import cover, grid, over, without_push
from scanner.projection import tuning_for, win_probability
from scanner.simulate import pick_chance, seed_for, simulate
from scanner.stakes import OUTCOME_PUSH, settle

MARKET_SPREAD, MARKET_TOTAL, MARKET_MONEYLINE = "spread", "total", "moneyline"
STANDARD_ODDS = -110  # the usual price on a spread or total when the book quotes none
OUTCOME_NOT_GRADED = "not_graded"


def payout(odds: int | None) -> float:
    """Units won per unit risked at American odds (-110 pays 0.909, +150 pays 1.5)."""
    if odds is None or odds == 0:
        odds = STANDARD_ODDS
    return odds / 100.0 if odds > 0 else 100.0 / abs(odds)


def break_even(odds: int | None) -> float:
    """The chance a price needs to come in to break even, the book's margin left in."""
    return 1.0 / (1.0 + payout(odds))


# -- timing ------------------------------------------------------------------------------------


def lock_moment(day: date, settings: Settings) -> datetime:
    """When the card for ``day`` locks: CARD_LOCK_TIME in CARD_TZ, as an aware moment."""
    return datetime.combine(day, settings.card_lock_time, tzinfo=ZoneInfo(settings.CARD_TZ))


def card_date_for(now: datetime, settings: Settings) -> date:
    """The day whose card is due: today in CARD_TZ once its lock time has passed, else yesterday."""
    local = now.astimezone(ZoneInfo(settings.CARD_TZ))
    if local.time() >= settings.card_lock_time:
        return local.date()
    return local.date() - timedelta(days=1)


def window_for(day: date, now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    """The kickoffs a day's card may hold: from its lock (or now, if later) for a day."""
    lock = lock_moment(day, settings)
    return max(lock, now), lock + timedelta(hours=CARD_WINDOW_HOURS)


# -- ranking -----------------------------------------------------------------------------------


def trust_for(record: dict | None) -> dict:
    """How much a sport's ranking is weighted by its closing-line record so far.

    Until the sport has CARD_TRUST_MIN_CLOSED closed lines every sport weighs the
    same. After that the weight is a half plus the share of lines that moved the
    model's way: 1.0 when the market agrees with the model half the time, more
    when it agrees more often, less when it does not.
    """
    clv = (record or {}).get("clv") or {}
    closed = int(clv.get("closed") or 0)
    share = clv.get("positive_share")
    if closed < CARD_TRUST_MIN_CLOSED or share is None:
        return {"trust": 1.0, "closed": closed, "positive_share": share, "weighted": False}
    return {"trust": 0.5 + share, "closed": closed, "positive_share": share, "weighted": True}


@dataclass(frozen=True, slots=True)
class Candidate:
    sport: str
    game_id: str
    market: str
    side: str
    line: float | None
    odds: int | None
    model_value: float
    book_value: float
    gap: float  # signed: model minus book
    z: float  # the gap in that sport's spread of results
    chance: float  # the model's chance the side comes in, given the line does not push
    edge: float  # that chance less the chance the price needs to break even


def side_chance(
    sport: str,
    raw: dict,
    sigma: float,
    sigma_total: float,
    market: str,
    side: str,
    line: float | None,
) -> float:
    """The model's chance a side comes in, from the score grid, given no push.

    The plain bell curve stands in when the grid cannot be laid out (a projection
    without a total, say).
    """
    margin, total = raw.get("margin"), raw.get("total")
    g = grid(sport, margin, total, sigma, sigma_total) if None not in (margin, total) else None
    if market == MARKET_MONEYLINE:
        p_home = g.home_win if g is not None else win_probability(margin, sigma)
        decided = sum(p for m, p in g.margin.items() if m != 0) if g is not None else 1.0
        p_home = p_home / decided if decided > 0 else p_home
        return p_home if side == "home" else 1.0 - p_home
    if market == MARKET_SPREAD:
        split = without_push(cover(g, line)) if g is not None else None
        if split is None:
            p_home = win_probability(margin + line, sigma)
            split = (p_home, 1.0 - p_home)
        return split[0] if side == "home" else split[1]
    split = without_push(over(g, line)) if g is not None else None
    if split is None:
        p_over = win_probability(total - line, sigma_total)
        split = (p_over, 1.0 - p_over)
    return split[0] if side == "over" else split[1]


def _candidate(sport, game_id, market, side, line, odds, model, book, gap, z, chance_of):
    chance = chance_of(market, side, line)
    return Candidate(
        sport, game_id, market, side, line, odds, model, book, gap, z,
        chance, chance - break_even(odds),
    )  # fmt: skip


def _label(market: str, side: str, line: float | None, home: str, away: str) -> str:
    if market == MARKET_MONEYLINE:
        return f"{home if side == 'home' else away} to win"
    if market == MARKET_TOTAL:
        return f"{side.capitalize()} {line:g}"
    team = home if side == "home" else away
    handicap = line if side == "home" else -line
    return f"{team} {handicap:+g}"


def candidates_for(sport: str, projection: dict, book: BookLine | None) -> list[Candidate]:
    """A game's possible picks: its margin side and its total side, where the book has a line."""
    raw, book_parts = projection.get("raw") or {}, projection.get("book") or {}
    if projection.get("thin") or not book_parts:
        return []
    tuning = tuning_for(sport)
    league = projection.get("league") or {}
    sigma = float(league.get("sigma") or tuning.sigma)
    sigma_total = float(league.get("sigma_total") or tuning.sigma_total)
    game_id = str(projection.get("game_id") or "")

    def chance_of(market: str, side: str, line: float | None) -> float:
        return side_chance(sport, raw, sigma, sigma_total, market, side, line)

    out = []
    margin, book_margin = raw.get("margin"), book_parts.get("margin")
    if margin is not None and book_margin is not None and margin != book_margin:
        gap = margin - book_margin
        side = "home" if gap > 0 else "away"
        if tuning.margin_from_moneyline:
            odds = None
            if book is not None:
                odds = book.home_ml if side == "home" else book.away_ml
            if odds is not None:
                out.append(
                    _candidate(
                        sport,
                        game_id,
                        MARKET_MONEYLINE,
                        side,
                        None,
                        odds,
                        margin,
                        book_margin,
                        gap,
                        abs(gap) / sigma,
                        chance_of,
                    )  # fmt: skip
                )
        else:
            odds = None
            if book is not None:
                odds = book.home_spread_odds if side == "home" else book.away_spread_odds
            out.append(
                _candidate(
                    sport,
                    game_id,
                    MARKET_SPREAD,
                    side,
                    -book_margin,
                    odds,
                    margin,
                    book_margin,
                    gap,
                    abs(gap) / sigma,
                    chance_of,
                )  # fmt: skip
            )
    total, book_total = raw.get("total"), book_parts.get("total")
    if total is not None and book_total is not None and total != book_total:
        gap = total - book_total
        side = "over" if gap > 0 else "under"
        odds = None
        if book is not None:
            odds = book.over_odds if side == "over" else book.under_odds
        out.append(
            _candidate(
                sport,
                game_id,
                MARKET_TOTAL,
                side,
                book_total,
                odds,
                total,
                book_total,
                gap,
                abs(gap) / sigma_total,
                chance_of,
            )  # fmt: skip
        )
    return out


def choose(
    games: list[dict], trust_by_sport: dict[str, dict], picks: int, draws: int = SIM_DRAWS
) -> list[dict]:
    """Rank every game's strongest side by conviction and keep the top ``picks``.

    A side's conviction is its edge times its sport's trust. A side with no edge
    (the price asks for more than the model gives) keeps its edge unweighted, so
    it ranks below every side with one and a thin day still fills its card.

    Each entry of ``games`` is a dict with ``sport``, ``game_id``, ``kickoff``
    (aware), ``home`` and ``away`` (the slate's team dicts), ``book`` (a BookLine
    or None) and ``projection`` (the stored projection). One side per game.
    """
    ranked = []
    for game in games:
        sport, projection = game["sport"], game["projection"]
        options = candidates_for(sport, projection, game.get("book"))
        if not options:
            continue
        best = max(options, key=lambda c: c.edge)
        trust = trust_by_sport.get(sport) or trust_for(None)
        conviction = best.edge * trust["trust"] if best.edge > 0 else best.edge
        ranked.append((conviction, game["kickoff"], best, trust, game))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    out = []
    for rank, (conviction, kickoff, best, trust, game) in enumerate(ranked[:picks], start=1):
        home, away = game["home"], game["away"]
        projection = game["projection"]
        stamp = str(game.get("updated_at") or kickoff.isoformat())
        seed = seed_for(best.sport, best.game_id, stamp)
        sim = simulate(projection, best.sport, draws=draws, seed=seed)
        chance = pick_chance(sim, best.market, best.side, best.line) if sim else None
        out.append(
            {
                "rank": rank,
                "sport": best.sport,
                "game_id": best.game_id,
                "kickoff": kickoff,
                "home": home["abbreviation"],
                "away": away["abbreviation"],
                "market": best.market,
                "side": best.side,
                "side_label": _label(
                    best.market, best.side, best.line, home["abbreviation"], away["abbreviation"]
                ),
                "line": best.line,
                "odds": best.odds if best.odds is not None else STANDARD_ODDS,
                "price": payout(best.odds),
                "model_value": best.model_value,
                "book_value": best.book_value,
                "gap": best.gap,
                "z": best.z,
                "trust": trust["trust"],
                "conviction": conviction,
                "detail": {
                    "home_team": home,
                    "away_team": away,
                    "neutral": bool(projection.get("neutral")),
                    "trust": trust,
                    "projection": {
                        k: projection.get(k)
                        for k in (
                            "model",
                            "games_used",
                            "thin",
                            "ratings",
                            "league",
                            "raw",
                            "blend",
                            "book",
                            "gap",
                        )
                    },
                    "sim": sim,
                    "chance": chance,
                    # What the ranking used: the chance given no push and its edge.
                    "chance_no_push": best.chance,
                    "break_even": break_even(best.odds),
                    "edge": best.edge,
                },
            }
        )
    return out


def is_late(locked_at: datetime, day: date, settings: Settings) -> bool:
    return locked_at > lock_moment(day, settings) + timedelta(minutes=CARD_LATE_MINUTES)


def games_in_window(diary, settings: Settings, start: datetime, end: datetime) -> list[dict]:
    """Every open projection whose game starts in the window, with its slate and book line."""
    games = []
    for sport in settings.gamelog_sports:
        for game_id, row in diary.open_projections(sport).items():
            kickoff = datetime.fromisoformat(row["kickoff"])
            if not start <= kickoff < end:
                continue
            upcoming = diary.football_upcoming_game(sport, game_id)
            if upcoming is None:
                continue
            slate = upcoming["slate"]
            games.append(
                {
                    "sport": sport,
                    "game_id": game_id,
                    "kickoff": kickoff,
                    "home": slate["home"],
                    "away": slate["away"],
                    "book": BookLine.from_dict(slate.get("book")) if slate.get("book") else None,
                    "projection": {**row["projection"], "game_id": game_id},
                    "updated_at": row.get("updated_at"),
                }
            )
    return games


def build_if_due(diary, settings: Settings, now: datetime) -> dict | None:
    """Lock the day's card if its time has come and it has not been locked yet.

    A day with nothing eligible is left unlocked, so a later pass can still lock
    it should games appear; the lock time on the card says when it happened and a
    card locked well after its time is marked late.
    """
    if not settings.CARD_ENABLED or not settings.PROJECTION_ENABLED:
        return None
    day = card_date_for(now, settings)
    key = day.isoformat()
    if diary.card_exists(key):
        return None
    start, end = window_for(day, now, settings)
    trust = {
        sport: trust_for(diary.projection_summary(sport=sport)) for sport in settings.gamelog_sports
    }
    picks = choose(games_in_window(diary, settings, start, end), trust, settings.CARD_PICKS)
    if not picks:
        return None
    late = is_late(now, day, settings)
    diary.record_card(key, now, late, None, picks)
    return {"card_date": key, "picks": len(picks), "late": late, "locked_at": now.isoformat()}


# -- grading -----------------------------------------------------------------------------------


def settle_pick(row: dict, home_score: int, away_score: int) -> tuple[str, float | None]:
    """(outcome, units won or lost at one unit risked) for a pick at the final score."""
    outcome, _ = settle(row["market"], row["side"], row["line"], home_score, away_score)
    if outcome == OUTCOME_NOT_GRADED:
        return outcome, None
    if outcome == OUTCOME_PUSH:
        return outcome, 0.0
    return outcome, float(row["price"]) if outcome == "win" else -1.0


def closing_for(row: dict, close: BookLine | None) -> tuple[float | None, float | None]:
    """(the closing number for the pick's market, how far it moved toward the pick).

    A positive move means the market came the model's way after the card locked:
    the spread or moneyline margin toward the side taken, the total toward the
    over or the under.
    """
    from scanner.projection import _book_parts

    parts = _book_parts(close, row["sport"])
    if not parts:
        return None, None
    if row["market"] == MARKET_TOTAL:
        closing = parts.get("total")
        if closing is None:
            return None, None
        moved = closing - row["book_value"]
        return closing, moved if row["side"] == "over" else -moved
    closing = parts.get("margin")
    if closing is None:
        return None, None
    moved = closing - row["book_value"]
    return closing, moved if row["side"] == "home" else -moved


def _tally(rows: list[dict]) -> dict:
    graded = [r for r in rows if r.get("outcome") in ("win", "loss", "push")]
    wins = sum(1 for r in graded if r["outcome"] == "win")
    losses = sum(1 for r in graded if r["outcome"] == "loss")
    pushes = sum(1 for r in graded if r["outcome"] == "push")
    units = sum(r.get("units") or 0.0 for r in graded)
    clv = [r["clv"] for r in graded if r.get("clv") is not None]
    return {
        "picks": len(rows),
        "graded": len(graded),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": wins / (wins + losses) if wins + losses else None,
        "units": units,
        "roi": units / len(graded) if graded else None,
        "clv": sum(clv) / len(clv) if clv else None,
        "clv_positive_share": (sum(1 for v in clv if v > 0) / len(clv)) if clv else None,
    }


def summarise(rows: list[dict], picks: int) -> dict:
    """The card's record: overall, by rank, by sport and by market."""
    ranks = sorted({r["rank"] for r in rows} | set(range(1, picks + 1)))
    return {
        **_tally(rows),
        "open": sum(1 for r in rows if r.get("outcome") is None),
        "not_graded": sum(1 for r in rows if r.get("outcome") == OUTCOME_NOT_GRADED),
        "by_rank": {str(n): _tally([r for r in rows if r["rank"] == n]) for n in ranks},
        "by_sport": {
            s: _tally([r for r in rows if r["sport"] == s])
            for s in sorted({r["sport"] for r in rows})
        },
        "by_market": {
            m: _tally([r for r in rows if r["market"] == m])
            for m in (MARKET_SPREAD, MARKET_MONEYLINE, MARKET_TOTAL)
            if any(r["market"] == m for r in rows)
        },
    }
