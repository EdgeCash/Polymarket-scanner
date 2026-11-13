"""The gap report (scripts/grade_gaps.py) on the lines fixtures: who is right when the raw
model sits far from the book, and what the grid gave against what came in.

The numbers are the report card in docs/sim_report.md; a tolerance holds each so a
change to the ratings, the blend or the grid that moves one shows up here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gaps():
    spec = importlib.util.spec_from_file_location("grade_gaps", ROOT / "scripts" / "grade_gaps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Expectations rounded to 2 points and 4 so nearby games share a grid: the report's
    # own quarter-point rounding takes a minute and a half on the NFL alone and moves
    # nothing here by more than half a point of chance.
    module.ROUNDING = (2.0, 4.0)
    return module


@pytest.fixture(scope="module")
def nfl(gaps):
    return gaps.grade(gaps.load(gaps.lines_path("nfl"), sport="nfl"), "nfl")


@pytest.fixture(scope="module")
def cbb(gaps):
    return gaps.grade(gaps.load(gaps.lines_path("cbb"), sport="cbb"), "cbb")


def test_big_college_underdogs_are_no_better_than_a_coin_toss_against_the_close(cbb):
    # 5,110 games of 2022-23 with a closing spread (ESPN keeps no lines for the two
    # seasons after). Spreads of 24 or more: the dog covers half the time, and the raw
    # model's side, which is nearly always the dog, no better; with under eight games
    # a side it covers under half, and the raw model misses those margins by 16.8 to
    # the close's 12.5. That is the card's big-line gate and why it ranks on the blend.
    assert cbb["games"] > 5000 and cbb["big_line"] == 24.0
    big = cbb["big_spreads"]
    assert big["all"]["games"] > 80 and big["all"]["cover"] == pytest.approx(0.49, abs=0.05)
    few = big["under_8_games"]
    assert few["games"] > 35 and few["model_side"]["cover"] < 0.52
    assert few["rmse_model"] > few["rmse_close"] + 3 and few["rmse_blend"] < few["rmse_close"] + 1
    # Up to ten points from the close the model's side is a coin toss while the grid
    # gave it 57-72%; from ten to fifteen it covered 62% of 181 games, one season's
    # worth, still far under the grid's 84%. The blend's chances sit nearer the truth.
    for band, cover, gave in (
        ("0-5", 0.507, 0.565),
        ("5-10", 0.521, 0.716),
        ("10-15", 0.624, 0.838),
    ):
        part = cbb["by_gap"][band]
        assert part["model_side"]["cover"] == pytest.approx(cover, abs=0.03)
        assert part["grid_gave_model_side"] == pytest.approx(gave, abs=0.03)
        assert part["grid_gave_blend_side"] < part["grid_gave_model_side"] - 0.015
    model, blend = cbb["calibration"]["model"]["70-80"], cbb["calibration"]["blend"]["70-80"]
    assert model["came_in"] == pytest.approx(0.53, abs=0.05) and model["games"] > 400
    assert blend["came_in"] > model["came_in"] and blend["games"] < model["games"] / 2


@pytest.fixture(scope="module")
def nba(gaps):
    return gaps.grade(gaps.load(gaps.lines_path("nba"), sport="nba"), "nba")


@pytest.fixture(scope="module")
def cfb(gaps):
    return gaps.grade(gaps.load(gaps.lines_path("cfb"), sport="cfb"), "cfb")


def test_the_nba_and_college_football_say_the_same(nba, cfb):
    # Up to ten points from the close the raw model's side is a coin toss in both,
    # while the grid gave it 54-76%; the close misses the margin by less than the
    # raw model, the blend between.
    for result, bands in ((nba, ("0-3", "3-6", "6-9")), (cfb, ("0-5", "5-10", "10-15"))):
        assert result["games"] > 2000
        for band in bands:
            part = result["by_gap"][band]
            assert 0.45 < part["model_side"]["cover"] < 0.55
            assert part["grid_gave_model_side"] > part["model_side"]["cover"] + 0.04
        rmse = result["rmse"]
        assert rmse["close"] < rmse["blend"] < rmse["model"]
    # Football's big spreads with few games a side: the dog covered under 40% of 39
    # games, the raw model missing those margins by five points more than the close.
    few = cfb["big_spreads"]["under_8_games"]
    assert few["games"] > 30 and few["cover"] < 0.45
    assert few["rmse_model"] > few["rmse_close"] + 4
    assert cfb["big_line"] == 32.5 and nba["big_spreads"]["all"]["games"] == 0


def test_the_nfl_model_s_side_against_the_close_is_a_coin_toss_whatever_the_gap(nfl):
    # 3,495 games, 2010 on, ratings on points fit on each season's earlier games: the raw
    # model's side covers the close about half the time in every band, under the 52.4% a
    # -110 price needs, while the grid was giving it far more. The close is the better
    # number, which is why the card ranks on the blend and leans on the closing line.
    assert nfl["games"] > 3000 and nfl["seasons"][0] == 2010
    for band, cover in (("0-3", 0.508), ("3-6", 0.484), ("6-9", 0.50)):
        part = nfl["by_gap"][band]
        assert part["model_side"]["cover"] == pytest.approx(cover, abs=0.03)
        assert part["grid_gave_model_side"] > part["model_side"]["cover"] + 0.03
    # A grid chance of 60-70% for the model's side came in half the time; the blend's
    # 60-70% a little more often, and far fewer sides reach it.
    model, blend = nfl["calibration"]["model"]["60-70"], nfl["calibration"]["blend"]["60-70"]
    assert model["came_in"] == pytest.approx(0.50, abs=0.04) and model["games"] > 800
    assert blend["games"] < model["games"] / 2
    # The close misses the margin by less than the raw model, the blend between them.
    rmse = nfl["rmse"]
    assert rmse["close"] < rmse["blend"] < rmse["model"]
    assert rmse["close"] == pytest.approx(13.0, abs=0.3)
