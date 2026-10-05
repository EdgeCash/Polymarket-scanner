"""The alert rules. Pure decisions: nothing here reads a feed or sends anything.

Winner alerts follow the seven numbered rules and the always-on checks from
the brief. Clinched-over alerts follow their shorter list. Every evaluation
returns a :class:`Decision` saying whether to alert, and if not, why; a game
that passed rules 1 and 2 but failed later is a near miss for the diary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scanner.config import (
    CLINCHED_FAIR_PRICE,
    PRICE_STALE_SECONDS,
    SCORE_STALE_SECONDS,
    Settings,
)
from scanner.fees import edge as edge_after_fee
from scanner.fees import fee_per_contract
from scanner.matching import Match
from scanner.models import (
    Alert,
    AlertType,
    Availability,
    Book,
    BookLevel,
    FairPrice,
    GameState,
    GameStatus,
    NearMiss,
    Quote,
    Side,
    TotalMarket,
)
from scanner.polymarket import buy_levels
from scanner.tracker import GameTracker, is_fresh

log = logging.getLogger(__name__)

REPEAT_EDGE_GROWTH = 0.02  # a repeat within the gap needs the edge to have grown this much


@dataclass(frozen=True, slots=True)
class Decision:
    alert: Alert | None
    near_miss: NearMiss | None
    reason: str

    @property
    def fired(self) -> bool:
        return self.alert is not None


class AlertHistory:
    """What has already been alerted today, for the repeat gap and the daily cap."""

    def __init__(self, tz: str) -> None:
        self._zone = ZoneInfo(tz)
        self._last: dict[tuple[str, str, str], tuple[datetime, float]] = {}
        self._sent_on: list[date] = []
        self._paused_on: date | None = None

    def local_date(self, now: datetime) -> date:
        return now.astimezone(self._zone).date()

    def count_today(self, now: datetime) -> int:
        today = self.local_date(now)
        return sum(1 for d in self._sent_on if d == today)

    def record(self, alert: Alert) -> None:
        key = (alert.league.value, alert.feed_id, alert.pick)
        self._last[key] = (alert.created_at, alert.edge)
        self._sent_on.append(self.local_date(alert.created_at))

    def last(self, league: str, feed_id: str, pick: str) -> tuple[datetime, float] | None:
        return self._last.get((league, feed_id, pick))

    def paused_message_due(self, now: datetime, cap: int) -> bool:
        """True once per local day, the first time the cap is reached."""
        today = self.local_date(now)
        if self.count_today(now) >= cap and self._paused_on != today:
            self._paused_on = today
            return True
        return False


def availability_at_edge(
    levels: tuple[BookLevel, ...], fair: float, theta: float, min_edge: float
) -> Availability:
    """Rule 4: dollars for sale, cheapest first, while the edge still clears ``min_edge``."""
    dollars = 0.0
    contracts = 0.0
    best: float | None = None
    for level in levels:
        if edge_after_fee(fair, level.price, theta) < min_edge - 1e-12:
            break
        if best is None:
            best = level.price
        dollars += level.price * level.quantity
        contracts += level.quantity
    average = dollars / contracts if contracts > 0 else None
    return Availability(
        dollars=dollars, contracts=contracts, average_price=average, best_price=best
    )


def _situation(state: GameState) -> dict:
    return {
        "status": state.status.value,
        "home_score": state.home_score,
        "away_score": state.away_score,
        "period": state.period,
        "clock_seconds": state.clock_seconds,
        "seconds_left": state.seconds_left,
        "possession": state.possession.value if state.possession else None,
        "down": state.down,
        "distance": state.distance,
        "yards_to_endzone": state.yards_to_endzone,
        "home_timeouts": state.home_timeouts,
        "away_timeouts": state.away_timeouts,
        "espn_home_win_probability": state.espn_home_win_probability,
        "home_spread": state.home_spread,
        "score_fetched_at": state.fetched_at.isoformat(),
    }


def polymarket_score_differs(display_score: str | None, state: GameState) -> bool:
    """Whether Polymarket's displayed score shows different numbers from ESPN's."""
    if not display_score or state.home_score is None or state.away_score is None:
        return False
    parts = [p.strip() for p in display_score.replace(":", "-").split("-")]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False
    shown = sorted(int(p) for p in parts)
    return shown != sorted([state.home_score, state.away_score])


def _near_miss(
    state: GameState,
    match: Match,
    pick: str,
    reason: str,
    fair: float | None,
    buy: float | None,
    edge_value: float | None,
    now: datetime,
) -> Decision:
    miss = NearMiss(
        created_at=now,
        league=state.league,
        feed_id=state.feed_id,
        event_slug=match.polymarket.event_slug,
        pick=pick,
        reason=reason,
        fair_price=fair,
        buy_price=buy,
        edge=edge_value,
    )
    return Decision(None, miss, reason)


def _common_checks(
    *,
    state: GameState,
    quote: Quote,
    tracker: GameTracker,
    now: datetime,
    market_open: bool,
) -> str | None:
    """The always-on checks shared by both alert types. Returns a reason or None."""
    if not is_fresh(state.fetched_at, now, SCORE_STALE_SECONDS):
        return "stale score"
    if not tracker.confirmed(state):
        return "not confirmed on two polls"
    if quote.buy_price is None:
        return "no price"
    if not is_fresh(quote.fetched_at, now, PRICE_STALE_SECONDS):
        return "stale price"
    if not market_open or not quote.is_open:
        return "market not open"
    return None


def _repeat_blocked(
    history: AlertHistory,
    league: str,
    feed_id: str,
    pick: str,
    edge_value: float,
    now: datetime,
    settings: Settings,
) -> bool:
    last = history.last(league, feed_id, pick)
    if last is None:
        return False
    last_at, last_edge = last
    within_gap = (now - last_at).total_seconds() < settings.REPEAT_ALERT_MINUTES * 60
    return within_gap and edge_value < last_edge + REPEAT_EDGE_GROWTH - 1e-12


def evaluate_winner(
    *,
    match: Match,
    state: GameState,
    side: Side,
    fair: FairPrice,
    quote: Quote,
    book: Book | None,
    settings: Settings,
    tracker: GameTracker,
    history: AlertHistory,
    now: datetime,
) -> Decision:
    """Apply the winner rules for one team of one game."""
    team = match.market_team_for(side)
    pick = team.abbreviation
    game = match.polymarket

    # Rule 1: late in the 4th quarter of a live game. Not a near miss if it fails.
    if state.status is not GameStatus.LIVE:
        return Decision(None, None, f"game is {state.status.value}")
    if state.is_overtime:
        return Decision(None, None, "overtime")
    if state.period != 4 or state.seconds_left is None:
        return Decision(None, None, "rule 1: not in the 4th quarter")
    if state.seconds_left > settings.MAX_MINUTES_LEFT * 60 + 1e-9:
        return Decision(None, None, "rule 1: too much time left")

    # Rule 2: fair price high enough. A disagreement with the model above the
    # bar is recorded as a near miss, since it is what the thresholds should see.
    if fair.fair is None:
        if fair.model_price is not None and fair.model_price >= settings.MIN_FAIR:
            return _near_miss(
                state,
                match,
                pick,
                f"no fair price: {fair.reason}",
                None,
                quote.buy_price,
                None,
                now,
            )
        return Decision(None, None, f"no fair price: {fair.reason}")
    if fair.fair < settings.MIN_FAIR:
        return Decision(None, None, "rule 2: fair price too low")

    # From here on every failure is a near miss.
    market_open = bool(game.moneyline_slug) and game.moneyline_active and not game.moneyline_closed
    reason = _common_checks(
        state=state, quote=quote, tracker=tracker, now=now, market_open=market_open
    )
    if reason:
        return _near_miss(state, match, pick, reason, fair.fair, quote.buy_price, None, now)
    buy = quote.buy_price
    assert buy is not None
    theta = quote.theta
    edge_value = edge_after_fee(fair.fair, buy, theta)

    # Rule 3: edge after the fee.
    if edge_value < settings.MIN_EDGE - 1e-12:
        return _near_miss(
            state, match, pick, "rule 3: edge too small", fair.fair, buy, edge_value, now
        )

    # Rule 4: dollars for sale at prices that still clear rule 3.
    if book is None:
        return _near_miss(state, match, pick, "rule 4: no book", fair.fair, buy, edge_value, now)
    if not is_fresh(book.fetched_at, now, PRICE_STALE_SECONDS):
        return _near_miss(state, match, pick, "rule 4: stale book", fair.fair, buy, edge_value, now)
    avail = availability_at_edge(
        buy_levels(book, team.is_long), fair.fair, theta, settings.MIN_EDGE
    )
    if avail.dollars < settings.MIN_DOLLARS_AVAILABLE - 1e-9:
        return _near_miss(
            state, match, pick, "rule 4: not enough for sale", fair.fair, buy, edge_value, now
        )

    # Rule 5: the score has stood for a while.
    age = tracker.seconds_since_score_change(state, now)
    if age is None or age < settings.SCORE_COOLDOWN_SECONDS:
        return _near_miss(
            state, match, pick, "rule 5: score changed recently", fair.fair, buy, edge_value, now
        )

    # Rule 6: no repeat for the same team inside the gap unless the edge grew.
    if _repeat_blocked(history, state.league.value, state.feed_id, pick, edge_value, now, settings):
        return _near_miss(
            state, match, pick, "rule 6: repeat too soon", fair.fair, buy, edge_value, now
        )

    # Rule 7: the daily cap.
    if history.count_today(now) >= settings.MAX_ALERTS_PER_DAY:
        return _near_miss(state, match, pick, "rule 7: daily cap", fair.fair, buy, edge_value, now)

    alert = Alert(
        alert_type=AlertType.WINNER,
        league=state.league,
        created_at=now,
        feed_id=state.feed_id,
        event_slug=game.event_slug,
        market_slug=game.moneyline_slug or "",
        home=match.home_team.abbreviation,
        away=match.away_team.abbreviation,
        pick=pick,
        side_label=quote.side_label,
        fair_price=fair.fair,
        model_price=fair.model_price,
        espn_price=fair.espn_price,
        buy_price=buy,
        fee=fee_per_contract(buy, theta),
        edge=edge_value,
        dollars_available=avail.dollars,
        average_price=avail.average_price,
        situation={**_situation(state), "fair_reason": fair.reason, "score_age_seconds": age},
        polymarket_score=game.display_score,
        polymarket_score_differs=polymarket_score_differs(game.display_score, state),
        enabled=settings.ALERTS_ENABLED,
    )
    return Decision(alert, None, "alert")


def evaluate_clinched(
    *,
    match: Match,
    state: GameState,
    total: TotalMarket,
    quote: Quote,
    book: Book | None,
    settings: Settings,
    tracker: GameTracker,
    history: AlertHistory,
    now: datetime,
) -> Decision:
    """Apply the clinched-over rules for one total line of one game."""
    pick = f"OVER {total.line:g}"
    game = match.polymarket
    if not settings.CLINCHED_OVERS_ENABLED:
        return Decision(None, None, "clinched overs disabled")
    expected_side = "long" if total.over_is_long else "short"
    if quote.side_label != expected_side or quote.market_slug != total.market_slug:
        # The Under can never be decided before the end; refuse anything but the Over.
        return Decision(None, None, "quote is not the Over side of this total")
    if state.status not in (GameStatus.LIVE, GameStatus.HALFTIME):
        return Decision(None, None, f"game is {state.status.value}")
    combined = state.total_points
    if combined is None:
        return Decision(None, None, "score unknown")
    if combined <= total.line:
        return Decision(None, None, "not clinched")

    # Clinched. From here on every failure is a near miss.
    age = tracker.seconds_since_score_change(state, now)
    if age is None or age < settings.CLINCH_COOLDOWN_SECONDS:
        return _near_miss(
            state,
            match,
            pick,
            "clinch cooldown: score changed recently",
            CLINCHED_FAIR_PRICE,
            quote.buy_price,
            None,
            now,
        )
    market_open = total.active and not total.closed and total.over_tradable
    reason = _common_checks(
        state=state, quote=quote, tracker=tracker, now=now, market_open=market_open
    )
    if reason:
        return _near_miss(
            state, match, pick, reason, CLINCHED_FAIR_PRICE, quote.buy_price, None, now
        )
    buy = quote.buy_price
    assert buy is not None
    theta = quote.theta
    edge_value = edge_after_fee(CLINCHED_FAIR_PRICE, buy, theta)
    if edge_value < settings.MIN_EDGE_CLINCHED - 1e-12:
        return _near_miss(
            state, match, pick, "edge too small", CLINCHED_FAIR_PRICE, buy, edge_value, now
        )
    if book is None:
        return _near_miss(
            state, match, pick, "rule 4: no book", CLINCHED_FAIR_PRICE, buy, edge_value, now
        )
    if not is_fresh(book.fetched_at, now, PRICE_STALE_SECONDS):
        return _near_miss(
            state, match, pick, "rule 4: stale book", CLINCHED_FAIR_PRICE, buy, edge_value, now
        )
    avail = availability_at_edge(
        buy_levels(book, total.over_is_long), CLINCHED_FAIR_PRICE, theta, settings.MIN_EDGE_CLINCHED
    )
    if avail.dollars < settings.MIN_DOLLARS_AVAILABLE - 1e-9:
        return _near_miss(
            state,
            match,
            pick,
            "rule 4: not enough for sale",
            CLINCHED_FAIR_PRICE,
            buy,
            edge_value,
            now,
        )
    if _repeat_blocked(history, state.league.value, state.feed_id, pick, edge_value, now, settings):
        return _near_miss(
            state, match, pick, "rule 6: repeat too soon", CLINCHED_FAIR_PRICE, buy, edge_value, now
        )
    if history.count_today(now) >= settings.MAX_ALERTS_PER_DAY:
        return _near_miss(
            state, match, pick, "rule 7: daily cap", CLINCHED_FAIR_PRICE, buy, edge_value, now
        )

    alert = Alert(
        alert_type=AlertType.CLINCHED_OVER,
        league=state.league,
        created_at=now,
        feed_id=state.feed_id,
        event_slug=game.event_slug,
        market_slug=total.market_slug,
        home=match.home_team.abbreviation,
        away=match.away_team.abbreviation,
        pick=pick,
        side_label=quote.side_label,
        fair_price=CLINCHED_FAIR_PRICE,
        model_price=None,
        espn_price=None,
        buy_price=buy,
        fee=fee_per_contract(buy, theta),
        edge=edge_value,
        dollars_available=avail.dollars,
        average_price=avail.average_price,
        situation={**_situation(state), "score_age_seconds": age},
        polymarket_score=game.display_score,
        polymarket_score_differs=polymarket_score_differs(game.display_score, state),
        line=total.line,
        combined_score=combined,
        enabled=settings.ALERTS_ENABLED,
    )
    return Decision(alert, None, "alert")


def pick_clinched(decisions: list[Decision]) -> Decision | None:
    """When several lines of one game are clinched and cheap, keep the deepest one."""
    fired = [d for d in decisions if d.alert is not None]
    if not fired:
        return None
    return max(fired, key=lambda d: d.alert.dollars_available)  # type: ignore[union-attr]
