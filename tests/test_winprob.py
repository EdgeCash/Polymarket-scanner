from __future__ import annotations

import itertools
import json
import time
from dataclasses import replace

import pytest

from scanner.config import KNEEL_FAIR_PRICE, MODEL_MAX_PRICE, load_settings
from scanner.models import GameStatus, League, Side
from scanner.scores import parse_scoreboard
from scanner.winprob import (
    MODEL_PATH,
    WinProbModel,
    can_kneel_out,
    fair_price,
    model_price,
)
from tests.conftest import at, load_fixture


@pytest.fixture(scope="module")
def model():
    return WinProbModel.load()


def live_state():
    raw = load_fixture("espn_states.json")
    states = {s.feed_id: s for s in parse_scoreboard(raw, League.NFL, at(0))}
    return states["401873001"]  # PHI 24 at JAX 14, Q4 3:52, PHI ball 2nd & 7 at JAX 35


# -- the saved model file -----------------------------------------------------


def test_model_file_loads_in_under_two_seconds():
    start = time.perf_counter()
    WinProbModel.load()
    assert time.perf_counter() - start < 2.0


def test_model_file_records_held_out_calibration_within_one_point():
    data = json.loads(MODEL_PATH.read_text())
    assert set(data["holdout_seasons"]).isdisjoint(data["train_seasons"])
    assert set(data["holdout_seasons"]).isdisjoint(data["calibration_seasons"])
    assert set(data["calibration_seasons"]).isdisjoint(data["train_seasons"])
    bucket = next(b for b in data["holdout"]["buckets"] if b["threshold"] == 0.95)
    assert bucket["n"] > 1000
    assert abs(bucket["actual"] - bucket["predicted"]) <= 0.01


def test_model_weights_are_non_negative_and_calibration_is_monotone(model):
    assert all(w >= 0 for w in model.weights)
    assert list(model.calibration_y) == sorted(model.calibration_y)


def test_model_rejects_a_negative_weight(tmp_path):
    data = json.loads(MODEL_PATH.read_text())
    data["weights"][0] = -0.1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        WinProbModel.load(path)


# -- monotonic guarantees -----------------------------------------------------

GRID_SECS = [0, 15, 45, 90, 180, 300, 480, 900]
GRID_DIFF = list(range(0, 36))
GRID_BALL = [1, -1]
GRID_FIELD = [(1, 10, 80), (2, 7, 35), (3, 12, 50), (4, 1, 2), (1, 10, 5)]
GRID_TO = [(3, 3), (0, 3), (3, 0), (1, 2)]


def price(model, diff, secs, ball, field, timeouts, adv=0.0, home=1):
    down, distance, ytg = field
    own, opp = timeouts
    return model.probability(
        diff=diff,
        seconds_left=secs,
        has_ball=ball,
        down=down,
        distance=distance,
        yards_to_endzone=ytg,
        own_timeouts=own,
        opp_timeouts=opp,
        advantage_points=adv,
        is_home=home,
    )


def test_a_bigger_lead_never_lowers_the_leaders_price(model):
    for secs, ball, field, to in itertools.product(GRID_SECS, GRID_BALL, GRID_FIELD, GRID_TO):
        previous = None
        for diff in GRID_DIFF:
            p = price(model, diff, secs, ball, field, to)
            if previous is not None:
                assert p >= previous - 1e-12, (diff, secs, ball, field, to)
            previous = p


def test_less_time_left_never_lowers_the_leaders_price(model):
    for diff, ball, field, to in itertools.product(range(1, 36), GRID_BALL, GRID_FIELD, GRID_TO):
        previous = None
        for secs in reversed(GRID_SECS):  # 900 down to 0
            p = price(model, diff, secs, ball, field, to)
            if previous is not None:
                assert p >= previous - 1e-12, (diff, secs, ball, field, to)
            previous = p


def test_having_the_ball_never_lowers_a_leading_teams_price(model):
    for diff, secs, to in itertools.product(range(1, 36), GRID_SECS, GRID_TO):
        with_ball = min(price(model, diff, secs, 1, field, to) for field in GRID_FIELD)
        without = max(price(model, diff, secs, -1, field, to) for field in GRID_FIELD)
        assert with_ball >= without - 1e-12, (diff, secs, to)


def test_model_is_symmetric_between_the_two_teams(model):
    a = price(model, 7, 120, 1, (1, 10, 50), (2, 1), adv=3.0, home=1)
    b = price(model, -7, 120, -1, (1, 10, 50), (1, 2), adv=-3.0, home=-1)
    assert a == pytest.approx(1 - b, abs=0.02)  # calibration is not exactly symmetric


def test_model_never_exceeds_the_cap_and_makes_sense(model):
    assert price(model, 35, 0, 1, (1, 10, 50), (3, 0)) == MODEL_MAX_PRICE
    tied = price(model, 0, 300, 1, (1, 10, 50), (3, 3))
    assert 0.4 < tied < 0.7
    assert price(model, -14, 60, 1, (1, 10, 80), (0, 3)) < 0.05
    assert price(model, 10, 232, 1, (2, 7, 35), (3, 2)) > 0.93


# -- state to price -----------------------------------------------------------


def test_model_price_for_the_live_fixture_both_sides(model):
    state = live_state()
    phi = model_price(state, Side.AWAY, model)
    jax = model_price(state, Side.HOME, model)
    assert phi is not None and jax is not None
    assert phi > 0.93
    assert jax < 0.07


def test_model_price_is_none_outside_the_fourth_quarter_or_with_missing_fields(model):
    state = live_state()
    assert model_price(replace(state, period=3, seconds_left=1000), Side.AWAY, model) is None
    assert model_price(replace(state, period=5, seconds_left=0), Side.AWAY, model) is None
    assert model_price(replace(state, status=GameStatus.HALFTIME), Side.AWAY, model) is None
    assert model_price(replace(state, possession=None), Side.AWAY, model) is None
    assert model_price(replace(state, yards_to_endzone=None), Side.AWAY, model) is None
    assert model_price(replace(state, home_timeouts=None), Side.AWAY, model) is None


def test_missing_spread_treats_the_teams_as_even(model):
    state = live_state()
    even = replace(state, home_spread=None)
    assert model_price(even, Side.AWAY, model) == pytest.approx(
        model_price(replace(state, home_spread=0.0), Side.AWAY, model)
    )


# -- kneel rule ---------------------------------------------------------------


def kneel_state(**changes):
    base = live_state()  # PHI (away) leads 24-14 with the ball, 2nd down, 3:52 left
    return replace(base, **changes)


def test_kneel_rule_first_down_no_opponent_timeouts_under_two_minutes():
    s = kneel_state(down=1, seconds_left=110.0, clock_seconds=110.0, home_timeouts=0)
    assert can_kneel_out(s, Side.AWAY) is True


def test_kneel_rule_each_opponent_timeout_removes_a_kneel():
    s = kneel_state(down=1, seconds_left=110.0, clock_seconds=110.0, home_timeouts=1)
    assert can_kneel_out(s, Side.AWAY) is False
    s = kneel_state(down=1, seconds_left=75.0, clock_seconds=75.0, home_timeouts=1)
    assert can_kneel_out(s, Side.AWAY) is True


def test_kneel_rule_counts_the_nfl_two_minute_warning_as_a_stop():
    s = kneel_state(down=1, seconds_left=115.0, clock_seconds=115.0, home_timeouts=0)
    assert can_kneel_out(s, Side.AWAY) is True
    s = kneel_state(down=1, seconds_left=121.0, clock_seconds=121.0, home_timeouts=0)
    assert can_kneel_out(s, Side.AWAY) is False
    college = replace(s, league=League.CFB)
    assert can_kneel_out(college, Side.AWAY) is False  # 121 > 120 burnable
    college = kneel_state(
        league=League.CFB, down=1, seconds_left=119.0, clock_seconds=119.0, home_timeouts=0
    )
    assert can_kneel_out(college, Side.AWAY) is True


def test_kneel_rule_never_for_fourth_down_trailing_team_or_team_without_ball():
    assert (
        can_kneel_out(kneel_state(down=4, seconds_left=10.0, home_timeouts=0), Side.AWAY) is False
    )
    assert (
        can_kneel_out(kneel_state(down=1, seconds_left=10.0, home_timeouts=0), Side.HOME) is False
    )
    trailing = kneel_state(down=1, seconds_left=10.0, home_timeouts=0, away_score=10)
    assert can_kneel_out(trailing, Side.AWAY) is False
    tied = kneel_state(down=1, seconds_left=10.0, home_timeouts=0, away_score=14)
    assert can_kneel_out(tied, Side.AWAY) is False
    assert can_kneel_out(kneel_state(down=None, seconds_left=10.0), Side.AWAY) is False


# -- fair price combination ---------------------------------------------------


def test_fair_price_is_the_lower_of_model_and_espn(model):
    settings = load_settings()
    state = live_state()  # ESPN says PHI 0.98
    ours = model_price(state, Side.AWAY, model)
    fp = fair_price(state, Side.AWAY, settings, model)
    assert fp.model_price == pytest.approx(ours)
    assert fp.espn_price == pytest.approx(0.98)
    assert fp.fair == pytest.approx(min(ours, 0.98))
    assert "min with espn" in fp.reason


def test_fair_price_without_espn_is_model_minus_two_cents(model):
    settings = load_settings()
    state = replace(live_state(), espn_home_win_probability=None)
    fp = fair_price(state, Side.AWAY, settings, model)
    assert fp.espn_price is None
    assert fp.fair == pytest.approx(fp.model_price - 0.02)
    assert "espn missing" in fp.reason


def test_fair_price_is_none_when_model_and_espn_disagree_by_more_than_five_cents(model):
    settings = load_settings()
    state = replace(live_state(), espn_home_win_probability=0.15)  # PHI 0.85 vs model ~0.95+
    fp = fair_price(state, Side.AWAY, settings, model)
    assert fp.fair is None
    assert fp.reason.startswith("disagree")
    assert fp.model_price is not None and fp.espn_price == pytest.approx(0.85)


def test_fair_price_is_none_in_overtime_or_when_not_live(model):
    settings = load_settings()
    ot = replace(live_state(), period=5, seconds_left=0.0)
    assert fair_price(ot, Side.AWAY, settings, model).fair is None
    assert "overtime" in fair_price(ot, Side.AWAY, settings, model).reason
    half = replace(live_state(), status=GameStatus.HALFTIME)
    assert fair_price(half, Side.AWAY, settings, model).fair is None
    delayed = replace(live_state(), status=GameStatus.DELAYED)
    assert fair_price(delayed, Side.AWAY, settings, model).fair is None
    third = replace(live_state(), period=3, seconds_left=1100.0)
    assert fair_price(third, Side.AWAY, settings, model).reason == "not in the 4th quarter"
    missing = replace(live_state(), down=None)
    assert fair_price(missing, Side.AWAY, settings, model).reason == "situation incomplete"


def test_college_games_get_the_extra_margin(model):
    settings = load_settings()
    nfl = replace(live_state(), espn_home_win_probability=None)
    cfb = replace(nfl, league=League.CFB)
    assert fair_price(cfb, Side.AWAY, settings, model).fair == pytest.approx(
        fair_price(nfl, Side.AWAY, settings, model).fair - settings.CFB_EXTRA_MARGIN
    )


def test_kneel_situation_is_priced_at_the_kneel_price(model):
    settings = load_settings()
    s = replace(live_state(), down=1, seconds_left=100.0, clock_seconds=100.0, home_timeouts=0)
    s = replace(s, espn_home_win_probability=0.005)
    fp = fair_price(s, Side.AWAY, settings, model)
    assert fp.model_price == pytest.approx(KNEEL_FAIR_PRICE)
    assert fp.fair == pytest.approx(min(KNEEL_FAIR_PRICE, 0.995))
    assert "kneel" in fp.reason
