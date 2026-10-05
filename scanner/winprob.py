"""Win probability: our model, ESPN's number, and the fair price that combines them.

The model is a JSON file trained by ``scripts/train_winprob.py``. Loading it is
a file read and no more; nothing here trains or needs numpy.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from dataclasses import dataclass
from math import exp
from pathlib import Path

from scanner.config import (
    ESPN_MISSING_MARGIN,
    KNEEL_FAIR_PRICE,
    KNEEL_SECONDS_PER_DOWN,
    MODEL_ESPN_MAX_DISAGREEMENT,
    MODEL_MAX_PRICE,
    NFL_TWO_MINUTE_WARNING,
    Settings,
)
from scanner.models import FairPrice, GameState, GameStatus, League, Side
from scanner.winprob_features import FEATURE_NAMES, features

log = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).with_name("winprob_model.json")


@dataclass(frozen=True, slots=True)
class WinProbModel:
    weights: tuple[float, ...]
    calibration_x: tuple[float, ...]
    calibration_y: tuple[float, ...]
    metadata: dict

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> WinProbModel:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        names = tuple(data["feature_names"])
        if names != FEATURE_NAMES:
            raise ValueError(f"model features {names} do not match code {FEATURE_NAMES}")
        weights = tuple(float(w) for w in data["weights"])
        if len(weights) != len(FEATURE_NAMES):
            raise ValueError("model has the wrong number of weights")
        if any(w < 0 for w in weights):
            raise ValueError("model has a negative weight; monotonic guarantees would break")
        cal = data["calibration"]
        xs = tuple(float(v) for v in cal["x"])
        ys = tuple(float(v) for v in cal["y"])
        if len(xs) != len(ys) or len(xs) < 2:
            raise ValueError("calibration map malformed")
        if any(b < a for a, b in zip(xs, xs[1:], strict=False)):
            raise ValueError("calibration x knots are not sorted")
        if any(b < a for a, b in zip(ys, ys[1:], strict=False)):
            raise ValueError("calibration map is not non-decreasing")
        metadata = {k: v for k, v in data.items() if k not in ("weights", "calibration")}
        return cls(weights, xs, ys, metadata)

    def raw_probability(self, feature_vector: list[float]) -> float:
        z = sum(w * f for w, f in zip(self.weights, feature_vector, strict=True))
        z = max(-40.0, min(40.0, z))
        return 1.0 / (1.0 + exp(-z))

    def calibrate(self, p: float) -> float:
        xs, ys = self.calibration_x, self.calibration_y
        if p <= xs[0]:
            return ys[0]
        if p >= xs[-1]:
            return ys[-1]
        i = bisect_right(xs, p)
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        if x1 == x0:
            return y1
        return y0 + (y1 - y0) * (p - x0) / (x1 - x0)

    def probability(self, **state: float) -> float:
        """Calibrated win probability for "us", capped at MODEL_MAX_PRICE."""
        p = self.calibrate(self.raw_probability(features(**state)))
        return max(0.0, min(MODEL_MAX_PRICE, p))


_default_model: WinProbModel | None = None


def default_model() -> WinProbModel:
    global _default_model
    if _default_model is None:
        _default_model = WinProbModel.load()
    return _default_model


def situation_complete(state: GameState) -> bool:
    return (
        state.home_score is not None
        and state.away_score is not None
        and state.seconds_left is not None
        and state.possession is not None
        and state.down is not None
        and state.distance is not None
        and state.yards_to_endzone is not None
        and state.home_timeouts is not None
        and state.away_timeouts is not None
    )


def model_price(state: GameState, side: Side, model: WinProbModel | None = None) -> float | None:
    """Our model's price for ``side`` to win, or None when it cannot be computed.

    Only priced in the 4th quarter of a live game with a complete situation.
    """
    if state.status is not GameStatus.LIVE or state.period != 4 or state.is_overtime:
        return None
    if not situation_complete(state):
        return None
    model = model or default_model()
    other = Side.AWAY if side is Side.HOME else Side.HOME
    diff = state.score_for(side) - state.score_for(other)
    home_adv = -state.home_spread if state.home_spread is not None else 0.0
    return model.probability(
        diff=diff,
        seconds_left=state.seconds_left,
        has_ball=1 if state.possession is side else -1,
        down=state.down,
        distance=state.distance,
        yards_to_endzone=state.yards_to_endzone,
        own_timeouts=state.home_timeouts if side is Side.HOME else state.away_timeouts,
        opp_timeouts=state.away_timeouts if side is Side.HOME else state.home_timeouts,
        advantage_points=home_adv if side is Side.HOME else -home_adv,
        is_home=1 if side is Side.HOME else -1,
    )


def can_kneel_out(state: GameState, side: Side) -> bool:
    """The leading team has the ball and can end the game in victory formation.

    Each kneel before 4th down burns a 40 second play clock unless the defence
    stops the clock with a timeout. In the NFL the two-minute warning stops the
    clock once more when the ball is snapped above 2:00. Conservative on purpose:
    a 4th-down kneel does not count, and the time per kneel is 40 seconds, not 42.
    """
    if state.status is not GameStatus.LIVE or state.period != 4 or state.is_overtime:
        return False
    if not situation_complete(state) or state.possession is not side:
        return False
    other = Side.AWAY if side is Side.HOME else Side.HOME
    if state.score_for(side) <= state.score_for(other):
        return False
    if state.down is None or state.down >= 4:
        return False
    stops = state.away_timeouts if side is Side.HOME else state.home_timeouts
    if state.league is League.NFL and state.seconds_left > NFL_TWO_MINUTE_WARNING:
        stops += 1
    kneels = 4 - state.down
    burnable = KNEEL_SECONDS_PER_DOWN * max(0, kneels - stops)
    return state.seconds_left <= burnable


def espn_price(state: GameState, side: Side) -> float | None:
    if state.espn_home_win_probability is None:
        return None
    p = state.espn_home_win_probability
    return p if side is Side.HOME else 1.0 - p


def fair_price(
    state: GameState,
    side: Side,
    settings: Settings,
    model: WinProbModel | None = None,
) -> FairPrice:
    """The lower of our model and ESPN, with every rule from the brief applied."""
    if state.status is not GameStatus.LIVE:
        return FairPrice(None, None, None, f"game is {state.status.value}, not live")
    if state.is_overtime:
        return FairPrice(None, None, None, "overtime is not priced")
    ours = model_price(state, side, model)
    if ours is None:
        if state.period != 4:
            return FairPrice(None, None, None, "not in the 4th quarter")
        return FairPrice(None, None, None, "situation incomplete")
    reason_bits = ["model"]
    if can_kneel_out(state, side):
        ours = max(ours, KNEEL_FAIR_PRICE)
        reason_bits.append("kneel")
    if state.league is League.CFB:
        ours = ours - settings.CFB_EXTRA_MARGIN
        reason_bits.append(f"cfb-{settings.CFB_EXTRA_MARGIN * 100:.0f}c")
    ours = max(0.0, min(1.0, ours))
    theirs = espn_price(state, side)
    if theirs is None:
        fair = max(0.0, ours - ESPN_MISSING_MARGIN)
        reason_bits.append("espn missing, -2c")
        return FairPrice(fair, ours, None, " ".join(reason_bits))
    if abs(ours - theirs) > MODEL_ESPN_MAX_DISAGREEMENT + 1e-9:
        log.info(
            "%s %s: model %.3f and ESPN %.3f disagree by more than 5c, no price",
            state.feed_id,
            side.value,
            ours,
            theirs,
        )
        return FairPrice(None, ours, theirs, f"disagree: model {ours:.3f} vs espn {theirs:.3f}")
    reason_bits.append("min with espn")
    return FairPrice(min(ours, theirs), ours, theirs, " ".join(reason_bits))
