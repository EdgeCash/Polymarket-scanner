"""Feature construction shared by the training script and the live model.

Everything is expressed from the point of view of one team ("us"). Flipping
sides negates every feature, which is what makes the model symmetric. Each
feature is built so that a non-negative weight gives the right direction:

* bigger lead -> higher,
* less time for a leader -> higher (time enters only through the margin),
* having the ball -> higher, with field position, down and distance scaled
  into [0, 1] and multiplied by the possession sign,
* more timeouts than the opponent -> higher,
* being favoured before the game, and playing at home -> higher.
"""

from __future__ import annotations

import math

FEATURE_NAMES = (
    "margin",
    "margin_per_sqrt_time",
    "one_score_step",
    "two_score_step",
    "three_score_step",
    "one_score_step_x_time",
    "two_score_step_x_time",
    "possession",
    "possession_x_field",
    "possession_x_down",
    "possession_x_distance",
    "timeout_edge",
    "pregame_advantage",
    "home_field",
)

MAX_MARGIN = 35.0
QUARTER_SECONDS = 900.0


def features(
    *,
    diff: float,
    seconds_left: float,
    has_ball: int,
    down: int,
    distance: int,
    yards_to_endzone: int,
    own_timeouts: int,
    opp_timeouts: int,
    advantage_points: float,
    is_home: int,
) -> list[float]:
    """Feature vector for "us".

    ``diff`` is our score minus theirs. ``has_ball`` is +1 (we have it), -1 (they
    do). ``down``, ``distance`` and ``yards_to_endzone`` describe whoever has the
    ball. ``advantage_points`` is how many points we were favoured by before the
    game (negative if we were the underdog). ``is_home`` is +1 or -1.
    """
    margin = max(-MAX_MARGIN, min(MAX_MARGIN, float(diff)))
    secs = max(0.0, min(QUARTER_SECONDS, float(seconds_left)))
    sign = 1.0 if margin > 0 else (-1.0 if margin < 0 else 0.0)
    size = abs(margin)
    time_gone = 1.0 - secs / QUARTER_SECONDS
    one = sign * (1.0 if size >= 4 else 0.0)
    two = sign * (1.0 if size >= 9 else 0.0)
    three = sign * (1.0 if size >= 17 else 0.0)
    ball = float(has_ball)
    field = (100.0 - max(1, min(99, int(yards_to_endzone)))) / 100.0
    down_value = (4.0 - max(1, min(4, int(down)))) / 3.0
    distance_value = max(0.0, 1.0 - max(1, int(distance)) / 20.0)
    timeout_edge = (max(0, min(3, int(own_timeouts))) - max(0, min(3, int(opp_timeouts)))) / 3.0
    return [
        margin / 10.0,
        margin / math.sqrt(secs + 1.0),
        one,
        two,
        three,
        one * time_gone,
        two * time_gone,
        ball,
        ball * field,
        ball * down_value,
        ball * distance_value,
        timeout_edge,
        max(-30.0, min(30.0, float(advantage_points))) / 10.0,
        float(is_home),
    ]
