from __future__ import annotations

import pytest

from scanner.fees import break_even_rate, edge, fee_for, fee_per_contract, risk_and_reward


@pytest.mark.parametrize(
    "price, published",
    [(0.10, 0.63), (0.50, 1.74), (0.93, 0.45), (0.99, 0.07), (0.01, 0.07), (0.65, 1.58)],
)
def test_fee_matches_the_published_table_for_100_contracts(price, published):
    assert fee_for(100, price) == published


def test_fee_examples_from_the_schedule():
    assert fee_for(1000, 0.10) == 6.26
    assert fee_for(1000, 0.65) == 15.81
    assert fee_for(1000, 0.50) == 17.38


def test_fee_uses_bankers_rounding():
    # 0.0695 * 100 * 0.5 * 0.5 = 1.7375 -> 1.74 (round half to even on the cent above)
    assert fee_for(100, 0.5) == 1.74
    assert fee_for(0, 0.5) == 0.0


def test_fee_is_symmetric_around_fifty_cents():
    assert fee_per_contract(0.93) == pytest.approx(fee_per_contract(0.07))


def test_theta_from_the_market_overrides_the_default():
    assert fee_per_contract(0.5, theta=0.10) == pytest.approx(0.025)


def test_edge_example_from_the_brief():
    # fair 99.5c, buy 93c, fee 0.45c -> edge about 6c
    e = edge(0.995, 0.93)
    assert e == pytest.approx(0.995 - 0.93 - 0.0045245, abs=1e-6)
    assert round(e, 3) == 0.06


def test_break_even_and_per_100_numbers_from_the_message_examples():
    assert round(break_even_rate(0.93) * 100, 1) == 93.5
    assert round(break_even_rate(0.96) * 100, 1) == 96.3
    risk, reward = risk_and_reward(0.93)
    assert risk == pytest.approx(93.0)
    assert reward == pytest.approx(6.55)
    risk, reward = risk_and_reward(0.96)
    assert risk == pytest.approx(96.0)
    assert reward == pytest.approx(3.73)


def test_price_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        fee_per_contract(1.5)
