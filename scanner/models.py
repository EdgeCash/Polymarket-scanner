"""Plain data types shared by every module.

Prices are decimal dollars per contract (0.93 means 93 cents). Times are
timezone-aware ``datetime`` values in UTC unless a field says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class League(StrEnum):
    NFL = "nfl"
    CFB = "cfb"


class GameStatus(StrEnum):
    """Where a game is. Anything the feed cannot describe cleanly is UNKNOWN."""

    PRE = "pre"
    LIVE = "live"
    HALFTIME = "halftime"
    FINAL = "final"
    DELAYED = "delayed"  # weather delay, suspension, or any stoppage
    POSTPONED = "postponed"  # postponed or cancelled before or during play
    UNKNOWN = "unknown"


class Side(StrEnum):
    HOME = "home"
    AWAY = "away"


class AlertType(StrEnum):
    WINNER = "winner"
    CLINCHED_OVER = "clinched_over"
    PERIOD = "period"  # a quarter or half market whose result is already known


REGULATION_PERIODS = 4


@dataclass(frozen=True, slots=True)
class Team:
    """One team as a feed names it."""

    name: str  # "Philadelphia Eagles"
    abbreviation: str  # "PHI"
    feed_id: str | None = None  # the feed's own id, kept for matching
    location: str | None = None  # ESPN's "Philadelphia", used for matching


@dataclass(frozen=True, slots=True)
class GameState:
    """The real situation of one game, as read from the score feed.

    ``seconds_left`` is seconds left in regulation (0 at the end of the 4th quarter).
    ``yards_to_endzone`` is for the team with the ball. Any field the feed did not
    give cleanly is ``None``; a game whose core fields are missing has
    ``status == UNKNOWN`` and ``unknown_reason`` set.
    """

    league: League
    feed_id: str
    home: Team
    away: Team
    kickoff: datetime | None
    status: GameStatus
    home_score: int | None
    away_score: int | None
    period: int | None
    clock_seconds: float | None  # seconds left in the current period
    seconds_left: float | None  # seconds left in regulation
    possession: Side | None
    down: int | None
    distance: int | None
    yards_to_endzone: int | None
    home_timeouts: int | None
    away_timeouts: int | None
    espn_home_win_probability: float | None  # ESPN's own number, 0..1, when present
    home_spread: float | None  # pre-game spread for the home team (negative = favourite)
    fetched_at: datetime
    unknown_reason: str | None = None
    status_detail: str = ""
    period_over: bool = False  # the feed says the current period has just ended
    home_linescores: tuple[int, ...] = ()  # points by period, when the feed gives them
    away_linescores: tuple[int, ...] = ()

    @property
    def is_overtime(self) -> bool:
        return self.period is not None and self.period > REGULATION_PERIODS

    @property
    def completed_periods(self) -> int:
        """How many periods are over for certain. 0 when the feed cannot say."""
        if self.period is None:
            return 0
        if self.status is GameStatus.FINAL:
            return max(self.period, REGULATION_PERIODS)
        if self.status is GameStatus.HALFTIME:
            return 2
        if self.status in (GameStatus.LIVE, GameStatus.DELAYED):
            return self.period if self.period_over else self.period - 1
        return 0

    def linescores_for(self, side: Side) -> tuple[int, ...]:
        return self.home_linescores if side is Side.HOME else self.away_linescores

    def points_so_far(self, side: Side, first: int, last: int) -> int | None:
        """Points scored in periods ``first``..``last`` so far. None without per-period scores."""
        scores = self.linescores_for(side)
        if not scores:
            return None
        return sum(scores[first - 1 : last])

    def points_in_span(self, side: Side, first: int, last: int) -> int | None:
        """Points in periods ``first``..``last``, only once the feed reports all of them."""
        scores = self.linescores_for(side)
        if len(scores) < last:
            return None
        return sum(scores[first - 1 : last])

    @property
    def total_points(self) -> int | None:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score + self.away_score

    def score_for(self, side: Side) -> int | None:
        return self.home_score if side is Side.HOME else self.away_score

    def team_for(self, side: Side) -> Team:
        return self.home if side is Side.HOME else self.away

    def situation_key(self) -> tuple:
        """Everything that must match on two polls in a row before an alert."""
        return (
            self.status,
            self.home_score,
            self.away_score,
            self.period,
            self.possession,
            self.down,
            self.distance,
            self.yards_to_endzone,
            self.home_timeouts,
            self.away_timeouts,
            self.period_over,
            self.home_linescores,
            self.away_linescores,
        )


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    quantity: float  # contracts for sale or bid at this price


@dataclass(frozen=True, slots=True)
class Book:
    """An order book for one Polymarket instrument, as fetched."""

    market_slug: str
    bids: tuple[BookLevel, ...]  # highest first
    offers: tuple[BookLevel, ...]  # lowest first
    state: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Quote:
    """What it costs to buy one side of a market right now."""

    market_slug: str
    side_label: str  # "long" or "short" (which half of the instrument)
    buy_price: float | None  # None means no price, no alert
    best_bid: float | None
    best_ask: float | None
    state: str | None  # MARKET_STATE_OPEN etc.
    tradable: bool
    theta: float
    fetched_at: datetime

    @property
    def is_open(self) -> bool:
        return self.state == "MARKET_STATE_OPEN" and self.tradable


@dataclass(frozen=True, slots=True)
class MarketTeam:
    """A team as Polymarket names it, with the side of the instrument it sits on."""

    team_id: int | None
    name: str
    abbreviation: str
    is_long: bool
    nickname: str = ""  # "Eagles"


@dataclass(frozen=True, slots=True)
class TotalMarket:
    market_slug: str
    line: float
    over_is_long: bool
    theta: float
    active: bool
    closed: bool
    over_tradable: bool


@dataclass(frozen=True, slots=True)
class PeriodMarket:
    """A quarter or half market: a total, a team total or a spread for that span only."""

    market_slug: str
    period: str  # "1q", "2q", "3q", "4q", "1h" or "2h"
    kind: str  # "total", "team_total" or "spread"
    line: float
    theta: float
    active: bool
    closed: bool
    long_tradable: bool
    short_tradable: bool
    over_is_long: bool = True  # totals and team totals: which side is the Over
    team_id: int | None = None  # team total: its team; spread: the long side's team
    other_team_id: int | None = None  # spread: the short side's team
    long_line: float | None = None  # spread: the long side's handicap, signed (+4.5)
    title: str = ""


@dataclass(frozen=True, slots=True)
class PolymarketGame:
    """One Polymarket event with its moneyline and total markets."""

    event_id: str
    event_slug: str
    league: League
    title: str
    start_time: datetime | None
    live: bool | None
    ended: bool | None
    teams: tuple[MarketTeam, ...]
    moneyline_slug: str | None
    moneyline_theta: float
    moneyline_active: bool
    moneyline_closed: bool
    totals: tuple[TotalMarket, ...]
    display_score: str | None  # Polymarket's own scoreboard, recorded, never trusted
    display_period: str | None
    display_elapsed: str | None
    period_markets: tuple[PeriodMarket, ...] = ()

    def team_on(self, is_long: bool) -> MarketTeam | None:
        for team in self.teams:
            if team.is_long == is_long:
                return team
        return None


@dataclass(frozen=True, slots=True)
class FairPrice:
    """The price the helper thinks is fair, and where it came from."""

    fair: float | None  # None means "send nothing", with reason
    model_price: float | None
    espn_price: float | None
    reason: str  # "min(model, espn)", "model-2c", "kneel", "disagree", "overtime", ...


@dataclass(frozen=True, slots=True)
class Availability:
    """Result of walking the book: dollars for sale while the edge still clears."""

    dollars: float
    contracts: float
    average_price: float | None
    best_price: float | None
    worst_price: float | None = None  # the dearest level still counted


@dataclass(slots=True)
class Alert:
    """One alert, exactly as sent, plus everything needed to grade it later."""

    alert_type: AlertType
    league: League
    created_at: datetime
    feed_id: str
    event_slug: str
    market_slug: str
    home: str
    away: str
    pick: str  # team abbreviation, or "OVER 47.5"
    side_label: str
    fair_price: float
    model_price: float | None
    espn_price: float | None
    buy_price: float
    fee: float
    edge: float
    dollars_available: float
    average_price: float | None
    worst_price: float | None = None  # dearest price included in dollars_available
    situation: dict = field(default_factory=dict)
    polymarket_score: str | None = None
    polymarket_score_differs: bool = False
    line: float | None = None
    combined_score: int | None = None
    pick_side: str | None = None  # "home" or "away" for winner alerts, for grading
    message: str = ""
    enabled: bool = False  # whether ALERTS_ENABLED was true when it fired


@dataclass(frozen=True, slots=True)
class NearMiss:
    created_at: datetime
    league: League
    feed_id: str
    event_slug: str
    pick: str
    reason: str
    fair_price: float | None
    buy_price: float | None
    edge: float | None


@dataclass(frozen=True, slots=True)
class Observation:
    """A winner evaluation inside the observation window: recorded, never sent.

    The window sits just outside the late-game filter, so the owner can see what
    the rules would have done with a little more time left before deciding whether
    to change the filter. Nothing here reaches the phone.
    """

    created_at: datetime
    league: League
    feed_id: str
    event_slug: str
    pick: str
    pick_side: str
    minutes_left: float
    home_score: int | None
    away_score: int | None
    fair_price: float | None
    model_price: float | None
    espn_price: float | None
    buy_price: float | None
    fee: float | None
    edge: float | None
    dollars_available: float | None
    would_alert: bool
    reason: str
