"""State that lives across polls: freshness, two-polls-in-a-row, score timing.

The feed modules are stateless. This tracker remembers the previous read of
each game so the rules can insist that a situation was seen twice before it
counts, and can measure how long the score has stood.
"""

from __future__ import annotations

from datetime import datetime

from scanner.config import FRESHNESS_SKEW_SECONDS
from scanner.models import GameState, GameStatus, League

GameKey = tuple[League, str]


def is_fresh(fetched_at: datetime, now: datetime, max_age_seconds: float) -> bool:
    """True when a read is recent enough to act on.

    A pass notes ``now`` when it starts and then reads the feeds, so a read is
    normally stamped a fraction of a second after ``now``. That is fresh. A stamp
    far in the future means a clock problem and is not.
    """
    age = (now - fetched_at).total_seconds()
    return -FRESHNESS_SKEW_SECONDS <= age <= max_age_seconds


class GameTracker:
    """Remembers the last two reads of every game and when its score last changed."""

    def __init__(self) -> None:
        self._latest: dict[GameKey, GameState] = {}
        self._previous: dict[GameKey, GameState] = {}
        self._score_changed_at: dict[GameKey, datetime] = {}
        self._score: dict[GameKey, tuple[int | None, int | None]] = {}

    @staticmethod
    def key(state: GameState) -> GameKey:
        return (state.league, state.feed_id)

    def update(self, state: GameState) -> None:
        key = self.key(state)
        if key in self._latest:
            self._previous[key] = self._latest[key]
        self._latest[key] = state
        score = (state.home_score, state.away_score)
        if key not in self._score or self._score[key] != score:
            self._score[key] = score
            self._score_changed_at[key] = state.fetched_at

    def update_all(self, states: list[GameState]) -> None:
        for state in states:
            self.update(state)

    def latest(self, key: GameKey) -> GameState | None:
        return self._latest.get(key)

    def previous(self, key: GameKey) -> GameState | None:
        return self._previous.get(key)

    def confirmed(self, state: GameState) -> bool:
        """True when the previous poll showed exactly the same situation."""
        if state.status is GameStatus.UNKNOWN:
            return False
        previous = self._previous.get(self.key(state))
        if previous is None or previous.status is GameStatus.UNKNOWN:
            return False
        if previous.fetched_at >= state.fetched_at:
            return False
        return previous.situation_key() == state.situation_key()

    def seconds_since_score_change(self, state: GameState, now: datetime) -> float | None:
        """How long the current score has stood, as of ``now``. None if never seen."""
        changed_at = self._score_changed_at.get(self.key(state))
        if changed_at is None:
            return None
        return max(0.0, (now - changed_at).total_seconds())

    def forget_finished(self, keep: set[GameKey]) -> None:
        """Drop games no longer on the scoreboard so memory stays small."""
        for key in list(self._latest):
            if key not in keep:
                self._latest.pop(key, None)
                self._previous.pop(key, None)
                self._score_changed_at.pop(key, None)
                self._score.pop(key, None)
