"""Polymarket US taker fee: ``theta x contracts x price x (1 - price)``.

From the fee schedule effective 1 October 2026. Theta comes from the market's
``feeCoefficient`` when present, else the published taker rate (0.0695).
Fees are rounded to the cent with banker's rounding, as the exchange does.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from scanner.config import DEFAULT_THETA


def fee_per_contract(price: float, theta: float = DEFAULT_THETA) -> float:
    """Exact (unrounded) taker fee for one contract bought at ``price``."""
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price {price} is not between 0 and 1")
    return theta * price * (1.0 - price)


def fee_for(contracts: float, price: float, theta: float = DEFAULT_THETA) -> float:
    """Fee in dollars for a fill of ``contracts`` at ``price``, rounded to the cent."""
    exact = Decimal(str(theta)) * Decimal(str(contracts)) * Decimal(str(price))
    exact *= Decimal(1) - Decimal(str(price))
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def edge(fair_price: float, buy_price: float, theta: float = DEFAULT_THETA) -> float:
    """Fair price minus buy price minus the per-contract fee."""
    return fair_price - buy_price - fee_per_contract(buy_price, theta)


def break_even_rate(buy_price: float, theta: float = DEFAULT_THETA) -> float:
    """The win rate at which buying at ``buy_price`` neither makes nor loses money."""
    return buy_price + fee_per_contract(buy_price, theta)


def risk_and_reward(
    buy_price: float, theta: float = DEFAULT_THETA, contracts: int = 100
) -> tuple[float, float]:
    """(dollars at risk, dollars won if the contract settles at $1) for ``contracts``."""
    risk = buy_price * contracts
    reward = (1.0 - buy_price) * contracts - fee_for(contracts, buy_price, theta)
    return risk, reward
