from __future__ import annotations

import pytest

from scanner.projection import fit, project
from scanner.weather import (
    BASELINE,
    RAIN_BASELINE,
    RAIN_POINTS,
    WIND_PER_MPH,
    describe,
    total_adjustment,
)
from tests.test_projection import season


def test_wind_lowers_a_football_total_and_a_dome_gets_the_baseline_back():
    calm = total_adjustment("nfl", {"indoor": False}, {"gust": 0, "temperature": 60})
    assert calm == {
        "indoors": False, "gust": 0.0, "wind": 0.0, "rain": None, "total": pytest.approx(-BASELINE)
    }  # fmt: skip
    windy = total_adjustment("nfl", {"indoor": False}, {"gust": 30, "temperature": 40})
    assert windy["wind"] == 20.0  # sustained is two thirds of the gust
    assert windy["total"] == pytest.approx(WIND_PER_MPH * 20 - BASELINE, abs=0.01)
    assert windy["total"] < -3.5
    # Six mph or so is the average day the ratings were fit in: no change to speak of.
    usual = total_adjustment("cfb", {}, {"gust": 9})
    assert abs(usual["total"]) < 0.2
    dome = total_adjustment("nfl", {"indoor": True}, {"gust": 15, "precipitation": 90})
    assert dome == {
        "indoors": True, "gust": None, "wind": 0.0, "rain": 0.0,
        "total": pytest.approx(-BASELINE - RAIN_BASELINE, abs=0.01),
    }  # fmt: skip
    # Nothing to go on, or not football: no adjustment.
    assert total_adjustment("nfl", {"indoor": False}, {"temperature": 50}) is None
    assert total_adjustment("nfl", None, None) is None
    assert total_adjustment("nba", {"indoor": True}, {"gust": 0}) is None
    assert total_adjustment("nhl", {}, {"gust": 40}) is None
    assert describe(windy) == "wind 20 mph (gusts 30): total -3.7"
    assert describe(dome) == "indoors: total +1.8"
    assert describe(None) == ""


def test_a_chance_of_rain_lowers_the_total_by_its_share_and_snow_does_not():
    wet = total_adjustment("nfl", {"indoor": False}, {"precipitation": 100, "temperature": 50})
    assert wet["rain"] == 1.0 and wet["wind"] is None and wet["gust"] is None
    assert wet["total"] == pytest.approx(RAIN_POINTS - RAIN_BASELINE, abs=0.01)
    likely = total_adjustment("cfb", {}, {"precipitation": 60, "gust": 15, "temperature": 55})
    assert likely["rain"] == 0.6 and likely["wind"] == 10.0
    assert likely["total"] == pytest.approx(
        WIND_PER_MPH * 10 - BASELINE + RAIN_POINTS * 0.6 - RAIN_BASELINE, abs=0.01
    )
    # A dry forecast gives the rain's baseline back: a fifth of a point.
    dry = total_adjustment("nfl", {"indoor": False}, {"precipitation": 0})
    assert dry["rain"] == 0.0 and dry["total"] == pytest.approx(-RAIN_BASELINE, abs=0.01)
    # At or below freezing the forecast is snow, which moves nothing.
    snow = total_adjustment("nfl", {"indoor": False}, {"precipitation": 90, "temperature": 28})
    assert snow["rain"] == 0.0 and snow["total"] == dry["total"]
    assert describe(likely) == f"wind 10 mph (gusts 15), rain 60%: total {likely['total']:+.1f}"
    assert describe(wet) == f"rain 100%: total {wet['total']:+.1f}"
    assert describe(dry) == f"dry: total {dry['total']:+.1f}"


def test_the_projection_moves_its_total_for_the_wind_and_keeps_its_margin():
    ratings = fit(season(), "nfl")
    plain = project(ratings, "A", "D", False, None)
    gale = project(
        ratings, "A", "D", False, None, {"venue": {"indoor": False}, "weather": {"gust": 30}}
    )
    adjust = gale["raw"]["weather"]["total"]
    assert adjust < -3.5
    assert gale["raw"]["total"] == pytest.approx(plain["raw"]["total"] + adjust)
    assert gale["raw"]["margin"] == pytest.approx(plain["raw"]["margin"])
    assert gale["raw"]["home"] == pytest.approx(plain["raw"]["home"] + adjust / 2)
    assert gale["raw"]["first_half"]["home"] == pytest.approx(
        gale["raw"]["home"] * ratings.first_half_share
    )
    assert "weather" not in plain["raw"]
    # The blend starts from the adjusted total; with no book it is the adjusted total.
    assert gale["blend"]["total"] == pytest.approx(gale["raw"]["total"])
    # Nothing known about the conditions: the projection is the plain one.
    assert project(ratings, "A", "D", False, None, {"venue": {}, "weather": None}) == plain
