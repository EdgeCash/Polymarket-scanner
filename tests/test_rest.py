from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scanner.projection import fit, project
from scanner.rest import BACK_TO_BACK_EDGE, days_since_last, describe, margin_adjustment
from tests.test_projection import season


def test_rest_is_counted_by_the_eastern_date_from_the_sides_other_games():
    # A 7 pm Eastern tip on the 10th is 00:00 UTC on the 11th; it is still the 10th here.
    tip = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    last_night = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)  # 7:30 pm Eastern on the 9th
    assert days_since_last(tip, [last_night]) == 1  # a back-to-back
    two_before = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
    assert days_since_last(tip, [two_before]) == 2
    # Only earlier games count; a game the same local day does not make a back-to-back.
    later = tip + timedelta(days=2)
    same_day_earlier = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)  # 1 pm Eastern on the 10th
    assert days_since_last(tip, [later, same_day_earlier]) is None
    assert days_since_last(tip, []) is None


def test_the_rested_side_gains_the_sports_edge():
    nhl = margin_adjustment("nhl", 1, 3)  # home on a back-to-back
    assert nhl == {"home": 1, "away": 3, "margin": pytest.approx(-BACK_TO_BACK_EDGE["nhl"])}
    nba = margin_adjustment("nba", 2, 1)  # away on a back-to-back
    assert nba["margin"] == pytest.approx(BACK_TO_BACK_EDGE["nba"])
    assert margin_adjustment("nba", 1, 1)["margin"] == 0  # both tired: a wash
    assert margin_adjustment("nba", None, 1)["margin"] == pytest.approx(BACK_TO_BACK_EDGE["nba"])
    assert margin_adjustment("nba", None, None) is None
    assert margin_adjustment("nfl", 1, 7) is None
    assert (
        describe(nhl, "BOS", "TOR", "goals")
        == f"BOS on a back-to-back: margin {-BACK_TO_BACK_EDGE['nhl']:+.1f} goals"
    )
    assert (
        describe(nba, "LAL", "DEN", "points")
        == f"DEN on a back-to-back: margin {BACK_TO_BACK_EDGE['nba']:+.1f} points"
    )
    assert describe(margin_adjustment("nba", 1, 1), "A", "B", "points") == ""
    assert describe(None, "A", "B", "points") == ""


def test_the_projection_moves_its_margin_for_a_back_to_back_and_keeps_its_total():
    ratings = fit(season(), "nba")
    plain = project(ratings, "A", "D", False, None)
    tired_home = project(ratings, "A", "D", False, None, {"rest": {"home": 1, "away": 2}})
    edge = BACK_TO_BACK_EDGE["nba"]
    assert tired_home["raw"]["rest"] == {"home": 1, "away": 2, "margin": pytest.approx(-edge)}
    assert tired_home["raw"]["margin"] == pytest.approx(plain["raw"]["margin"] - edge)
    assert tired_home["raw"]["total"] == pytest.approx(plain["raw"]["total"])
    assert tired_home["raw"]["home_win"] < plain["raw"]["home_win"]
    assert "rest" not in plain["raw"]
    # Football ignores rest; a side with no games on record is taken as rested.
    nfl = fit(season(), "nfl")
    assert project(nfl, "A", "D", False, None, {"rest": {"home": 1, "away": 7}}) == project(
        nfl, "A", "D", False, None
    )
