"""What the weather does to a football total.

Wind is the one condition that moves a total by more than noise, and the market
prices only part of it. Fit on 3,314 NFL games (2010-2026, nflverse's stadium
reports) against a weather-blind expected total from team ratings fit only on
each season's earlier games (scripts/fit_weather.py): each mph of sustained wind
costs 0.27 points, linear from calm, held out on 2020-2026 (the miss falls from
13.75 to 13.62); cold adds nothing once wind is in, and a dome or a closed roof
is calm. The ratings already carry the average conditions they were fit in, so
a game is adjusted by its effect less the sample mean of the effect: a dome
moves up about a point and a half, a six-mph day not at all, a twenty-mph gale
down three and a half. The closing total still misses 0.16 a mph, so the market
prices about forty percent of this.

ESPN reports a gust, not a sustained wind; sustained is taken as two thirds of
the gust, the usual ratio over land. College football takes the NFL's fit, the
one source with the wind on record. Nothing is adjusted when the venue is
indoors, or when there is no weather yet, and only football's totals move.
"""

from __future__ import annotations

WIND_PER_MPH = -0.266  # points of total per mph of sustained wind
BASELINE = -1.59  # the effect's mean over the games the ratings are fit on
GUST_TO_WIND = 2.0 / 3.0  # sustained wind as a share of the reported gust
SPORTS = ("nfl", "cfb")


def total_adjustment(sport: str, venue: dict | None, weather: dict | None) -> dict | None:
    """Points to add to a projected total for the conditions, with what they were.

    None when the sport is not football or there is nothing to go on. Indoors
    counts as calm, so a dome gets the baseline back.
    """
    if sport not in SPORTS:
        return None
    venue, weather = venue or {}, weather or {}
    indoors = venue.get("indoor") is True
    gust = weather.get("gust")
    if not indoors and not isinstance(gust, int | float):
        return None
    wind = 0.0 if indoors else max(float(gust), 0.0) * GUST_TO_WIND
    return {
        "indoors": indoors,
        "gust": None if indoors or gust is None else float(gust),
        "wind": round(wind, 1),
        "total": round(WIND_PER_MPH * wind - BASELINE, 2),
    }


def describe(adjustment: dict | None) -> str:
    """One phrase for a sheet: 'indoors: total +1.6' or 'wind 10 mph (gusts 15): total -1.1'."""
    if not adjustment:
        return ""
    total = f"total {adjustment['total']:+.1f}"
    if adjustment["indoors"]:
        return f"indoors: {total}"
    gust = f" (gusts {adjustment['gust']:.0f})" if adjustment.get("gust") is not None else ""
    return f"wind {adjustment['wind']:.0f} mph{gust}: {total}"
