"""What the model would stake on Polymarket: recorded, graded, shown late, never placed.

A stake is a quarter-Kelly share of the bankroll on the blended projection's
probability for a side, against the last Polymarket price the pre-game scan saw
for it, after the taker fee, when the edge clears the floor; it is capped at a
share of the bankroll. Suggestions go to the diary as shadow rows from the first
day and are graded at the final like everything else. The sheet shows them only
once the model's own record in that sport has earned it: enough graded games,
with the closing line moving the model's way. Nothing here places anything.
"""

from __future__ import annotations

from scanner.config import DEFAULT_THETA, Settings
from scanner.fees import fee_per_contract
from scanner.projection import TUNING, win_probability

MARKETS = ("moneyline", "total", "spread")
OUTCOME_WIN, OUTCOME_LOSS, OUTCOME_PUSH = "win", "loss", "push"


def probabilities(projection: dict, sport: str, polymarket: dict) -> dict[str, dict[str, float]]:
    """The blend's probability for each side the pre-game scan has a price for."""
    blend = projection.get("blend") or {}
    league = projection.get("league") or {}
    tuning = TUNING.get(sport, TUNING["cfb"])
    sigma = league.get("sigma") or tuning.sigma
    sigma_total = league.get("sigma_total") or tuning.sigma_total
    out: dict[str, dict[str, float]] = {}
    p_home = blend.get("home_win")
    if p_home is not None and (
        polymarket.get("home") is not None or polymarket.get("away") is not None
    ):
        out["moneyline"] = {"home": p_home, "away": 1.0 - p_home}
    total = polymarket.get("total")
    if isinstance(total, list) and len(total) == 3 and blend.get("total") is not None:
        p_over = win_probability(blend["total"] - float(total[0]), sigma_total)
        out["total"] = {"over": p_over, "under": 1.0 - p_over}
    spread = polymarket.get("spread")
    if isinstance(spread, list) and len(spread) == 3 and blend.get("margin") is not None:
        p_cover = win_probability(blend["margin"] + float(spread[0]), sigma)
        out["spread"] = {"home": p_cover, "away": 1.0 - p_cover}
    return out


def _prices(polymarket: dict) -> dict[str, dict[str, float | None]]:
    total = polymarket.get("total") if isinstance(polymarket.get("total"), list) else [None] * 3
    spread = polymarket.get("spread") if isinstance(polymarket.get("spread"), list) else [None] * 3
    return {
        "moneyline": {"home": polymarket.get("home"), "away": polymarket.get("away")},
        "total": {"over": total[1], "under": total[2]},
        "spread": {"home": spread[1], "away": spread[2]},
    }


def _label(market: str, side: str, line: float | None, home: str, away: str) -> str:
    if market == "moneyline":
        return home if side == "home" else away
    if market == "total":
        return f"{side} {line:g}"
    team = home if side == "home" else away
    team_line = line if side == "home" else -line
    return f"{team} {team_line:+g}"


def suggest(
    projection: dict, polymarket: dict | None, sport: str, home: str, away: str, settings: Settings
) -> list[dict]:
    """The best side per market whose edge after the fee clears the floor, biggest first."""
    polymarket = polymarket or {}
    probs = probabilities(projection, sport, polymarket)
    prices = _prices(polymarket)
    lines = {
        "moneyline": None,
        "total": (polymarket.get("total") or [None])[0],
        "spread": (polymarket.get("spread") or [None])[0],
    }
    out = []
    for market in MARKETS:
        best = None
        for side, p in probs.get(market, {}).items():
            price = prices[market].get(side)
            if p is None or not isinstance(price, int | float) or not 0.0 < price < 1.0:
                continue
            fee = fee_per_contract(price, DEFAULT_THETA)
            cost = price + fee  # what one contract really costs
            edge = p - cost
            if edge < settings.STAKE_MIN_EDGE:
                continue
            kelly = edge / (1.0 - cost)  # the full-Kelly share of the bankroll
            share = min(kelly * settings.STAKE_KELLY_FRACTION, settings.STAKE_MAX_SHARE)
            stake = share * settings.BANKROLL
            line = None if lines[market] is None else float(lines[market])
            row = {
                "market": market,
                "side": side,
                "side_label": _label(market, side, line, home, away),
                "line": line,
                "buy_price": float(price),
                "fee": fee,
                "model_prob": p,
                "edge": edge,
                "kelly": kelly,
                "share": share,
                "stake": stake,
                "contracts": stake / cost if cost > 0 else 0.0,
            }
            if best is None or edge > best["edge"]:
                best = row
        if best is not None:
            out.append(best)
    return sorted(out, key=lambda r: -r["edge"])


def gate(record: dict | None, settings: Settings) -> dict:
    """Whether the sheet may show stakes for a sport, from the model's graded record."""
    record = record or {}
    graded = int(record.get("graded") or 0)
    clv = (record.get("clv") or {}).get("margin")
    needed = settings.STAKE_GATE_GAMES
    is_open = graded >= needed and (needed == 0 or (clv is not None and clv > 0))
    return {"open": bool(is_open), "graded": graded, "needed": needed, "clv": clv}


def settle(
    market: str, side: str, line: float | None, home_score: int, away_score: int
) -> tuple[str, float | None]:
    """(outcome, settlement per contract) for a side at the final score."""
    margin = home_score - away_score
    total = home_score + away_score
    if market == "moneyline":
        if margin == 0:
            return OUTCOME_PUSH, 0.5  # a tie pays half, as the alert grading does
        won = (margin > 0) == (side == "home")
    elif market == "total" and line is not None:
        if total == line:
            return OUTCOME_PUSH, None
        won = (total > line) == (side == "over")
    elif market == "spread" and line is not None:
        if margin + line == 0:
            return OUTCOME_PUSH, None
        won = (margin + line > 0) == (side == "home")
    else:
        return "not_graded", None
    return (OUTCOME_WIN, 1.0) if won else (OUTCOME_LOSS, 0.0)


def profit(row: dict, settlement: float | None) -> float:
    """Dollars won or lost on a suggestion; a void (None settlement) costs nothing."""
    if settlement is None:
        return 0.0
    contracts = row["contracts"]
    return contracts * (settlement - row["buy_price"]) - contracts * row["fee"]
