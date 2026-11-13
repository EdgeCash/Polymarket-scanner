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
