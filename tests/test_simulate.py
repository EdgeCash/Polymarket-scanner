from __future__ import annotations

import pytest

from scanner.projection import win_probability
from scanner.simulate import (
    chances,
    ladder,
    pick_chance,
    seed_for,
    simulate,
    svg_heatmap,
    svg_histogram,
)

NFL = {
    "raw": {"margin": 4.0, "total": 44.0, "home_win": win_probability(4.0, 13.5)},
    "league": {"sigma": 13.5, "sigma_total": 10.5},
}


def test_simulated_games_follow_the_projection_and_pin_to_their_seed():
    sim = simulate(NFL, "nfl", draws=20_000, seed=7)
    assert sim["draws"] == 20_000 and sim["seed"] == 7 and sim["source"] == "raw"
    assert sim["margin"]["mean"] == pytest.approx(4.0, abs=0.4)
    assert sim["total"]["mean"] == pytest.approx(44.0, abs=0.4)
    assert sim["margin"]["sd"] == pytest.approx(13.5, abs=0.5)
    assert sim["home_win"] == pytest.approx(NFL["raw"]["home_win"], abs=0.015)
    assert 0.01 < sim["overtime"] < 0.05  # level after regulation, settled in overtime
    assert "0" not in sim["margin"]["hist"]  # so no game ends level
    assert sum(sim["margin"]["hist"].values()) == 20_000
    assert len(sim["scores"]) == 10 and all(n > 0 for _, _, n in sim["scores"])
    assert "cells" not in sim
    again = simulate(NFL, "nfl", draws=20_000, seed=7)
    assert again == sim
    assert simulate(NFL, "nfl", draws=20_000, seed=8) != sim
    with_cells = simulate(NFL, "nfl", draws=2_000, seed=7, with_cells=True)
    assert sum(with_cells["cells"].values()) == 2_000
    assert simulate({"raw": {}}, "nfl") is None
    assert simulate(NFL, "nfl", draws=0) is None
    # The blend can be drawn instead of the model.
    blend = simulate({**NFL, "blend": {"margin": -6.0, "total": 40.0}}, "nfl", 5_000, 1, "blend")
    assert blend["margin"]["mean"] < 0


def test_hockey_scores_are_whole_goals_and_basketball_overtime_adds_points():
    nhl = simulate(
        {"raw": {"margin": 0.3, "total": 6.1, "home_win": 0.55}, "league": {}},
        "nhl",
        draws=10_000,
        seed=3,
        with_cells=True,
    )
    assert all(int(h) >= 0 and int(a) >= 0 for h, a in (k.split(",") for k in nhl["cells"]))
    assert 0.1 < nhl["overtime"] < 0.3  # level after sixty minutes, far more often than football
    assert nhl["home_win"] == pytest.approx(0.55, abs=0.03)
    nba = simulate(
        {"raw": {"margin": -3.0, "total": 228.0, "home_win": 0.4}, "league": {}},
        "nba",
        draws=10_000,
        seed=4,
    )
    # The projection is fit to final scores, overtime included, so the picture keeps its
    # mean total; the overtime games' extra points show up in the shape instead.
    assert nba["total"]["mean"] == pytest.approx(228.0, abs=1e-4)
    assert 0.01 < nba["overtime"] < 0.05
    assert nba["home_win"] == pytest.approx(0.4, abs=0.03)


def test_chances_and_pick_chance_read_the_histograms():
    hist = {"-3": 10, "-1": 20, "2": 30, "3": 25, "7": 15}
    assert chances(hist, 2.5) == {"above": 0.4, "push": 0.0, "below": 0.6}
    assert chances(hist, 3) == {"above": 0.15, "push": 0.25, "below": 0.6}
    assert chances({}, 1) == {"above": 0.0, "push": 0.0, "below": 0.0}
    sim = {"home_win": 0.61, "margin": {"hist": hist}, "total": {"hist": {"40": 1, "44": 3}}}
    assert pick_chance(sim, "moneyline", "home", None) == 0.61
    assert pick_chance(sim, "moneyline", "away", None) == pytest.approx(0.39)
    # Home -2.5 covers when the margin is above 2.5.
    assert pick_chance(sim, "spread", "home", -2.5) == 0.4
    assert pick_chance(sim, "spread", "away", -2.5) == 0.6
    assert pick_chance(sim, "total", "over", 42.5) == 0.75
    assert pick_chance(sim, "total", "under", 42.5) == 0.25
    assert pick_chance(sim, "spread", "home", None) is None
    assert pick_chance(sim, "team_total", "home", 20) is None
    rungs = ladder(hist, 2.5, 1.0, rungs=1)
    assert [r["line"] for r in rungs] == [1.5, 2.5, 3.5]
    assert rungs[0]["above"] == 0.7 and rungs[2]["below"] == 0.85


def test_pictures_mark_the_book_and_the_model_and_shade_the_pick():
    sim = simulate(NFL, "nfl", draws=5_000, seed=11, with_cells=True)
    svg = svg_histogram(
        sim["margin"]["hist"],
        line=2.5,
        mean=4.0,
        side_above=True,
        line_label="book CLE -2.5",
        mean_label="model CLE -4.0",
        axis_label="Margin (CLE, points)",
    )
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "<script" not in svg
    assert "book CLE -2.5" in svg and "model CLE -4.0" in svg and "stroke-dasharray" in svg
    assert "fill='#1a7f37'" in svg and "fill='#8884'" in svg  # the pick's side and the rest
    assert ">+6<" in svg and ">0<" in svg  # signed margin ticks, zero unsigned
    plain = svg_histogram(
        sim["total"]["hist"],
        line=None,
        mean=None,
        side_above=None,
        line_label="",
        mean_label="",
        axis_label="Total points",
    )
    assert "fill='#1a7f37'" not in plain and "stroke-dasharray" not in plain
    assert svg_histogram({}, line=None, mean=None, side_above=None, line_label="",
                         mean_label="", axis_label="x") == ""  # fmt: skip
    heat = svg_heatmap(sim["cells"], "CLE", "PIT")
    assert heat.startswith("<svg") and "CLE score" in heat and "PIT score" in heat
    assert "<rect" in heat and "stroke-dasharray" in heat  # cells and the tie line
    assert svg_heatmap({}, "A", "B") == ""


def test_seed_is_stable_across_processes():
    assert seed_for("nfl", "401", "2026-10-11T14:00:00+00:00") == 2384493067
    assert seed_for("nfl", "401", "x") != seed_for("nfl", "402", "x")
