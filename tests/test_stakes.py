from __future__ import annotations

import pytest

from scanner.config import load_settings
from scanner.fees import fee_per_contract
from scanner.projection import win_probability
from scanner.stakes import gate, probabilities, profit, settle, suggest

SETTINGS = load_settings(
    BANKROLL=1000,
    STAKE_MIN_EDGE=0.03,
    STAKE_KELLY_FRACTION=0.25,
    STAKE_MAX_SHARE=0.05,
    STAKE_GATE_GAMES=50,
)
PROJ = {
    "blend": {"margin": 4.0, "total": 48.0, "home_win": 0.62},
    "league": {"sigma": 13.5, "sigma_total": 10.5},
}


def test_probabilities_cover_each_market_the_scan_priced():
    pm = {"home": 0.5, "away": 0.52, "total": [45.5, 0.5, 0.52], "spread": [-2.5, 0.5, 0.52]}
    probs = probabilities(PROJ, "nfl", pm)
    assert probs["moneyline"] == {"home": 0.62, "away": pytest.approx(0.38)}
    over = win_probability(48.0 - 45.5, 10.5)
    assert probs["total"] == {"over": pytest.approx(over), "under": pytest.approx(1 - over)}
    cover = win_probability(4.0 - 2.5, 13.5)
    assert probs["spread"] == {"home": pytest.approx(cover), "away": pytest.approx(1 - cover)}
    assert 0.59 < over < 0.60 and 0.54 < cover < 0.55
    assert probabilities(PROJ, "nfl", {}) == {}
    assert set(probabilities(PROJ, "nfl", {"home": 0.5})) == {"moneyline"}
    # Without the sigmas in the projection, the sport's own are used.
    bare = probabilities({"blend": PROJ["blend"]}, "nfl", pm)
    assert bare["spread"]["home"] == pytest.approx(cover)
    assert probabilities({"blend": {}}, "nfl", pm) == {}


def test_suggest_takes_the_best_side_per_market_at_a_quarter_kelly():
    pm = {"home": 0.5, "away": 0.52, "total": [45.5, 0.5, 0.52], "spread": [-2.5, 0.45, 0.57]}
    out = suggest(PROJ, pm, "nfl", "CLE", "PIT", SETTINGS)
    assert [r["market"] for r in out] == ["moneyline", "spread", "total"]
    assert [r["edge"] for r in out] == sorted((r["edge"] for r in out), reverse=True)
    ml = out[0]
    fee = fee_per_contract(0.5)
    assert ml["side"] == "home" and ml["side_label"] == "CLE" and ml["line"] is None
    assert ml["buy_price"] == 0.5 and ml["fee"] == pytest.approx(fee)
    assert ml["edge"] == pytest.approx(0.62 - 0.5 - fee)
    assert ml["kelly"] == pytest.approx(ml["edge"] / (1 - 0.5 - fee))
    assert ml["share"] == 0.05  # a quarter of the Kelly share would be more: capped
    assert ml["stake"] == 50.0 and ml["contracts"] == pytest.approx(50 / (0.5 + fee))
    spread = out[1]
    assert spread["side_label"] == "CLE -2.5" and spread["line"] == -2.5
    assert spread["share"] == pytest.approx(spread["kelly"] * 0.25) and spread["share"] < 0.05
    assert spread["stake"] == pytest.approx(spread["share"] * 1000)
    total = out[2]
    assert total["side"] == "over" and total["side_label"] == "over 45.5"
    # The away side of the spread, priced at 57c, had no edge; PIT +2.5 would be its label.
    flipped = suggest(
        {**PROJ, "blend": {"margin": -6.0, "total": 48.0, "home_win": 0.3}},
        pm,
        "nfl",
        "CLE",
        "PIT",
        SETTINGS,
    )
    assert {(r["market"], r["side_label"]) for r in flipped} == {
        ("moneyline", "PIT"),
        ("spread", "PIT +2.5"),
        ("total", "over 45.5"),
    }
    strict = suggest(PROJ, pm, "nfl", "CLE", "PIT", load_settings(STAKE_MIN_EDGE=0.09))
    assert [r["market"] for r in strict] == ["moneyline"]
    assert suggest(PROJ, None, "nfl", "CLE", "PIT", SETTINGS) == []
    assert suggest(PROJ, {"home": 0.0, "away": 1.0}, "nfl", "CLE", "PIT", SETTINGS) == []


def test_gate_opens_on_graded_games_and_a_line_that_moves_the_models_way():
    assert gate({"graded": 49, "clv": {"margin": 0.5}}, SETTINGS)["open"] is False
    assert gate({"graded": 50, "clv": {"margin": 0.5}}, SETTINGS) == {
        "open": True,
        "graded": 50,
        "needed": 50,
        "clv": 0.5,
    }
    assert gate({"graded": 50, "clv": {"margin": -0.1}}, SETTINGS)["open"] is False
    assert gate({"graded": 50, "clv": {"margin": None}}, SETTINGS)["open"] is False
    assert gate(None, SETTINGS) == {"open": False, "graded": 0, "needed": 50, "clv": None}
    assert gate({"graded": 0}, load_settings(STAKE_GATE_GAMES=0))["open"] is True


def test_settle_and_profit_follow_the_final_score():
    assert settle("moneyline", "home", None, 27, 24) == ("win", 1.0)
    assert settle("moneyline", "away", None, 27, 24) == ("loss", 0.0)
    assert settle("moneyline", "home", None, 20, 20) == ("push", 0.5)
    assert settle("total", "over", 45.5, 27, 24) == ("win", 1.0)
    assert settle("total", "under", 45.5, 27, 24) == ("loss", 0.0)
    assert settle("total", "over", 51.0, 27, 24) == ("push", None)
    assert settle("spread", "home", -2.5, 27, 24) == ("win", 1.0)
    assert settle("spread", "away", -2.5, 27, 24) == ("loss", 0.0)
    assert settle("spread", "away", -3.5, 27, 24) == ("win", 1.0)
    assert settle("spread", "home", -3.0, 27, 24) == ("push", None)
    assert settle("bogus", "home", None, 27, 24) == ("not_graded", None)
    assert settle("total", "over", None, 27, 24) == ("not_graded", None)
    row = {"contracts": 100.0, "buy_price": 0.5, "fee": 0.017375}
    assert profit(row, 1.0) == pytest.approx(50 - 1.7375)
    assert profit(row, 0.0) == pytest.approx(-50 - 1.7375)
    assert profit(row, 0.5) == pytest.approx(-1.7375)
    assert profit(row, None) == 0.0
