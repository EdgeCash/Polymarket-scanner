"""Quarter and half markets whose result is already known from the score feed.

Polymarket US lists, for each football game, a total for every quarter and half,
team totals for each half and spreads for every quarter and half. Once ESPN's
per-period scores show a span finished, these markets are decided, yet they can
keep trading for a while. This module says which side of such a market is
decided, if any. It never guesses: no per-period scores, a span still running
(for anything but an Over that the points have already passed), or a push all
mean "not decided". The same function grades the alert once the game is final.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scanner.matching import Match
from scanner.models import GameState, PeriodMarket, Side

PERIOD_SPANS = {"1q": (1, 1), "2q": (2, 2), "3q": (3, 3), "4q": (4, 4), "1h": (1, 2), "2h": (3, 4)}
PERIOD_LABELS = {"1q": "1Q", "2q": "2Q", "3q": "3Q", "4q": "4Q", "1h": "1H", "2h": "2H"}
PERIOD_NAMES = {
    "1q": "1st quarter",
    "2q": "2nd quarter",
    "3q": "3rd quarter",
    "4q": "4th quarter",
    "1h": "1st half",
    "2h": "2nd half",
}
PICK_PREFIXES = tuple(f"{label} " for label in PERIOD_LABELS.values())


def is_period_pick(pick: str) -> bool:
    """Whether an alert or near-miss pick names a quarter or half market."""
    return pick.startswith(PICK_PREFIXES)


@dataclass(frozen=True, slots=True)
class PeriodSides:
    """Which ESPN side each Polymarket team of the market is, plus names for the pick."""

    home_abbr: str
    away_abbr: str
    team_side: Side | None = None  # team total: the team; spread: the long side's team
    other_side: Side | None = None  # spread: the short side's team

    def abbr(self, side: Side) -> str:
        return self.home_abbr if side is Side.HOME else self.away_abbr


@dataclass(frozen=True, slots=True)
class PeriodDecision:
    side_label: str  # "long" or "short": the side whose result is known
    pick: str  # "1H OVER 24.5", "1Q DAL -4.5", "1H TB UNDER 6.5"
    detail: str  # "1st half ended with 17 points"
    points: int | None  # the points the decision rests on (totals), else None
    by_period_end: bool  # True: the span ended; False: the points passed the line
    pick_side: str | None  # "home"/"away" for team totals and spreads
    margin: float = 0.0  # how far past the line the result is; the closest are read first


def last_period(market: PeriodMarket) -> int:
    return PERIOD_SPANS[market.period][1]


def period_sides(match: Match, market: PeriodMarket) -> PeriodSides:
    """Map the market's Polymarket team ids onto ESPN's home and away sides."""

    def side_of(team_id: int | None) -> Side | None:
        if team_id is None:
            return None
        if match.home_team.team_id == team_id:
            return Side.HOME
        if match.away_team.team_id == team_id:
            return Side.AWAY
        return None

    return PeriodSides(
        home_abbr=match.home_team.abbreviation,
        away_abbr=match.away_team.abbreviation,
        team_side=side_of(market.team_id),
        other_side=side_of(market.other_team_id),
    )


def decide(market: PeriodMarket, state: GameState, sides: PeriodSides) -> PeriodDecision | None:
    """The decided side of a quarter or half market, or None while it is still open."""
    span = PERIOD_SPANS.get(market.period)
    if span is None or market.kind not in ("total", "team_total", "spread"):
        return None
    first, last = span
    complete = state.completed_periods >= last
    label = PERIOD_LABELS[market.period]
    name = PERIOD_NAMES[market.period]
    line = market.line

    if market.kind == "spread":
        if (
            not complete
            or sides.team_side is None
            or sides.other_side is None
            or market.long_line is None
        ):
            return None
        long_points = state.points_in_span(sides.team_side, first, last)
        short_points = state.points_in_span(sides.other_side, first, last)
        if long_points is None or short_points is None:
            return None
        margin = long_points + market.long_line - short_points
        long_abbr, short_abbr = sides.abbr(sides.team_side), sides.abbr(sides.other_side)
        detail = f"{name} ended {long_abbr} {long_points}, {short_abbr} {short_points}"
        if margin > 1e-9:
            pick = f"{label} {long_abbr} {market.long_line:+g}"
            return PeriodDecision(
                "long", pick, detail, None, True, sides.team_side.value, abs(margin)
            )
        if margin < -1e-9:
            pick = f"{label} {short_abbr} {-market.long_line:+g}"
            return PeriodDecision(
                "short", pick, detail, None, True, sides.other_side.value, abs(margin)
            )
        return None  # a push

    if market.kind == "team_total":
        if sides.team_side is None:
            return None
        so_far = state.points_so_far(sides.team_side, first, last)
        in_span = state.points_in_span(sides.team_side, first, last)
        who = f"{sides.abbr(sides.team_side)} "
        pick_side: str | None = sides.team_side.value
    else:
        home_so_far = state.points_so_far(Side.HOME, first, last)
        away_so_far = state.points_so_far(Side.AWAY, first, last)
        so_far = None if home_so_far is None or away_so_far is None else home_so_far + away_so_far
        home_span = state.points_in_span(Side.HOME, first, last)
        away_span = state.points_in_span(Side.AWAY, first, last)
        in_span = None if home_span is None or away_span is None else home_span + away_span
        who = ""
        pick_side = None
    if so_far is None:
        return None
    over_label = "long" if market.over_is_long else "short"
    under_label = "short" if market.over_is_long else "long"
    if so_far > line + 1e-9:
        if complete and in_span is not None:
            detail = f"{name} ended with {who}{in_span} points"
        else:
            detail = f"{who}{so_far} points in the {name} so far"
        return PeriodDecision(
            over_label,
            f"{label} {who}OVER {line:g}",
            detail,
            so_far,
            complete,
            pick_side,
            so_far - line,
        )
    if complete and in_span is not None and in_span < line - 1e-9:
        detail = f"{name} ended with {who}{in_span} points"
        return PeriodDecision(
            under_label,
            f"{label} {who}UNDER {line:g}",
            detail,
            in_span,
            True,
            pick_side,
            line - in_span,
        )
    return None


def to_json(market: PeriodMarket, sides: PeriodSides) -> dict[str, Any]:
    """Everything needed to decide the market again when the game is final."""
    return {
        "market_slug": market.market_slug,
        "period": market.period,
        "kind": market.kind,
        "line": market.line,
        "over_is_long": market.over_is_long,
        "long_line": market.long_line,
        "home_abbr": sides.home_abbr,
        "away_abbr": sides.away_abbr,
        "team_side": sides.team_side.value if sides.team_side else None,
        "other_side": sides.other_side.value if sides.other_side else None,
    }


def from_json(data: Any) -> tuple[PeriodMarket, PeriodSides] | None:
    if not isinstance(data, dict):
        return None
    try:
        market = PeriodMarket(
            market_slug=str(data["market_slug"]),
            period=str(data["period"]),
            kind=str(data["kind"]),
            line=float(data["line"]),
            theta=0.0,
            active=True,
            closed=False,
            long_tradable=True,
            short_tradable=True,
            over_is_long=bool(data.get("over_is_long", True)),
            long_line=None if data.get("long_line") is None else float(data["long_line"]),
        )
        sides = PeriodSides(
            home_abbr=str(data.get("home_abbr") or ""),
            away_abbr=str(data.get("away_abbr") or ""),
            team_side=Side(data["team_side"]) if data.get("team_side") else None,
            other_side=Side(data["other_side"]) if data.get("other_side") else None,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return market, sides
