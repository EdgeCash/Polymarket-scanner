"""What the weather does to a football total.

Wind and rain are the conditions that move a total by more than noise, and the
market prices only part of each. Wind first. Fit on 3,314 NFL games (2010-2026, nflverse's stadium
reports) against a weather-blind expected total from team ratings fit only on
each season's earlier games (scripts/fit_weather.py): each mph of sustained wind
costs 0.27 points, linear from calm, held out on 2020-2026 (the miss falls from
13.75 to 13.62); cold adds nothing once wind is in, and a dome or a closed roof
is calm. The ratings already carry the average conditions they were fit in, so
a game is adjusted by its effect less the sample mean of the effect: a dome
moves up about a point and a half, a six-mph day not at all, a twenty-mph gale
down three and a half. The closing total still misses 0.16 a mph, so the market
prices about forty percent of this.

Rain is the other. Fit on 3,357 games with a stadium weather note (nflfastR's
play-by-play, 2010-2026; scripts/fit_nfl_situations.py), a game played in rain
runs 4.1 points under the same wind-adjusted expectation (182 games; held out on
2020-2026 the miss falls from 13.71 to 13.66), the same with the wind fit beside
it, steady in every stretch since 2015, and the closing total still misses 3.7
of it. Snow shows nothing that holds (40 games, the other way), so a forecast
at or below freezing is not counted as rain. Before the game there is only a
forecast, so the effect is weighted by ESPN's chance of precipitation, and like
the wind it is taken less its mean over the games the ratings were fit on (one
in eighteen was wet), which is a fifth of a point.

ESPN reports a gust, not a sustained wind; sustained is taken as two thirds of
the gust, the usual ratio over land. College football takes the NFL's fit, the
one source with the conditions on record. Nothing is adjusted when the venue is
outdoors with neither a gust nor a chance of rain on record, and only football's
totals move.
"""

from __future__ import annotations

WIND_PER_MPH = -0.266  # points of total per mph of sustained wind
BASELINE = -1.59  # the effect's mean over the games the ratings are fit on
GUST_TO_WIND = 2.0 / 3.0  # sustained wind as a share of the reported gust
RAIN_POINTS = -4.1  # points of total when the game is played in rain
RAIN_SHARE = 0.054  # the share of fitted games played in rain: the ratings carry that much
RAIN_BASELINE = RAIN_POINTS * RAIN_SHARE
FREEZING = 32.0  # at or below this the forecast is snow, which moves nothing
SPORTS = ("nfl", "cfb")


def total_adjustment(sport: str, venue: dict | None, weather: dict | None) -> dict | None:
    """Points to add to a projected total for the conditions, with what they were.

    None when the sport is not football or there is nothing to go on. Indoors
    counts as calm and dry, so a dome gets both baselines back. Outdoors the wind
    counts when a gust is reported and the rain when a chance of it is.
    """
    if sport not in SPORTS:
        return None
    venue, weather = venue or {}, weather or {}
    indoors = venue.get("indoor") is True
    gust = weather.get("gust")
    gust = float(gust) if isinstance(gust, int | float) else None
    chance = weather.get("precipitation")
    chance = min(max(float(chance) / 100.0, 0.0), 1.0) if isinstance(chance, int | float) else None
    temperature = weather.get("temperature")
    if chance is not None and isinstance(temperature, int | float) and temperature <= FREEZING:
        chance = 0.0  # snow
    if indoors:
        gust, wind, chance = None, 0.0, 0.0
    elif gust is None and chance is None:
        return None
    else:
        wind = None if gust is None else max(gust, 0.0) * GUST_TO_WIND
    total = 0.0
    if wind is not None:
        total += WIND_PER_MPH * wind - BASELINE
    if chance is not None:
        total += RAIN_POINTS * chance - RAIN_BASELINE
    return {
        "indoors": indoors,
        "gust": gust,
        "wind": None if wind is None else round(wind, 1),
        "rain": None if chance is None else round(chance, 2),
        "total": round(total, 2),
    }


def describe(adjustment: dict | None) -> str:
    """One phrase for a sheet: 'indoors: total +1.8' or 'wind 10 mph (gusts 15), rain 70%: ...'."""
    if not adjustment:
        return ""
    total = f"total {adjustment['total']:+.1f}"
    if adjustment["indoors"]:
        return f"indoors: {total}"
    parts = []
    if adjustment.get("wind") is not None:
        gust = f" (gusts {adjustment['gust']:.0f})" if adjustment.get("gust") is not None else ""
        parts.append(f"wind {adjustment['wind']:.0f} mph{gust}")
    if adjustment.get("rain"):
        parts.append(f"rain {adjustment['rain'] * 100:.0f}%")
    return f"{', '.join(parts) or 'dry'}: {total}"
