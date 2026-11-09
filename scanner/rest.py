"""What rest does to a game: the back-to-back in the NBA and the NHL.

A side that played the day before gives up ground, and the schedule says so
before the market has to. Fit on history against a rest-blind expected margin
from team ratings fit only on each season's earlier games (scripts/fit_rest.py):
in hockey the rested side gains 0.45 goals of margin when the other is on a
back-to-back (4,947 games, 2021-22 to 2024-25, held out on 2024-25), in the NBA
2.5 points (4,661 games, the held-out margin miss 14.13 against 14.22); long
rest adds nothing and the total does not move in either. The two sides'
coefficients are not told apart by the data (NBA home -2.8, away +2.4), so one
serves both. A back-to-back is a game on the day after the side's last one, by the
local date (a 7 pm Eastern tip is the next day in UTC), counted from every
game on the season's schedule, played or still to come. Nothing moves when a
side's last game is unknown, or outside these two sports.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
# Points (goals) of margin the rested side gains when the other side is on a back-to-back.
BACK_TO_BACK_EDGE = {"nhl": 0.45, "nba": 2.5}
SPORTS = tuple(BACK_TO_BACK_EDGE)


def local_date(moment: datetime):
    return moment.astimezone(EASTERN).date()


def days_since_last(kickoff: datetime, others: list[datetime]) -> int | None:
    """Days between a side's kickoff and its latest other game before it, by local date;
    None when it has none on record."""
    day = local_date(kickoff)
    earlier = [local_date(m) for m in others if m < kickoff and local_date(m) < day]
    if not earlier:
        return None
    return (day - max(earlier)).days


def margin_adjustment(sport: str, home_rest: int | None, away_rest: int | None) -> dict | None:
    """Points to add to the projected home margin for the two sides' rest, with the rests.

    None outside the two sports or when neither side's rest is known; a side whose rest
    is unknown is taken as rested.
    """
    if sport not in SPORTS or (home_rest is None and away_rest is None):
        return None
    home_b2b, away_b2b = home_rest == 1, away_rest == 1
    edge = BACK_TO_BACK_EDGE[sport]
    return {
        "home": home_rest,
        "away": away_rest,
        "margin": round(edge * (int(away_b2b) - int(home_b2b)), 2),
    }


def describe(adjustment: dict | None, home: str, away: str, unit: str) -> str:
    """One phrase for a sheet: 'BOS on a back-to-back: margin -1.0 points'."""
    if not adjustment or adjustment["margin"] == 0:
        return ""
    tired = home if adjustment["margin"] < 0 else away
    both = adjustment["home"] == 1 and adjustment["away"] == 1
    if both:
        return ""
    return f"{tired} on a back-to-back: margin {adjustment['margin']:+.1f} {unit}"
