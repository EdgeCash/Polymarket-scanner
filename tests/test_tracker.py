from __future__ import annotations

from dataclasses import replace

from scanner.config import PRICE_STALE_SECONDS, SCORE_STALE_SECONDS
from scanner.models import GameStatus, League
from scanner.scores import parse_scoreboard
from scanner.tracker import GameTracker, is_fresh
from tests.conftest import at, load_fixture


def live_state(seconds=0.0):
    raw = load_fixture("espn_states.json")
    states = {s.feed_id: s for s in parse_scoreboard(raw, League.NFL, at(seconds))}
    return states["401873001"]


def test_score_freshness_window_is_fifteen_seconds():
    assert is_fresh(at(0), at(14.9), SCORE_STALE_SECONDS)
    assert is_fresh(at(0), at(15.0), SCORE_STALE_SECONDS)
    assert not is_fresh(at(0), at(15.1), SCORE_STALE_SECONDS)


def test_price_freshness_window_is_five_seconds():
    assert is_fresh(at(0), at(4.9), PRICE_STALE_SECONDS)
    assert not is_fresh(at(0), at(5.1), PRICE_STALE_SECONDS)


def test_a_read_stamped_moments_after_the_pass_started_is_fresh():
    # A pass notes "now", then reads the feed; the read is stamped a little later.
    assert is_fresh(at(0.8), at(0), SCORE_STALE_SECONDS)
    assert is_fresh(at(2.9), at(0), PRICE_STALE_SECONDS)


def test_a_read_stamped_well_into_the_future_is_not_fresh():
    assert not is_fresh(at(10), at(0), SCORE_STALE_SECONDS)
    assert not is_fresh(at(3.1), at(0), SCORE_STALE_SECONDS)


def test_first_poll_is_never_confirmed():
    tracker = GameTracker()
    first = live_state(0)
    tracker.update(first)
    assert tracker.confirmed(first) is False


def test_two_identical_polls_in_a_row_confirm():
    tracker = GameTracker()
    first = live_state(0)
    second = live_state(5)
    tracker.update(first)
    tracker.update(second)
    assert tracker.confirmed(second) is True


def test_a_changed_situation_is_not_confirmed_until_seen_twice():
    tracker = GameTracker()
    tracker.update(live_state(0))
    tracker.update(live_state(5))
    changed = replace(live_state(10), down=3)
    tracker.update(changed)
    assert tracker.confirmed(changed) is False
    again = replace(live_state(15), down=3)
    tracker.update(again)
    assert tracker.confirmed(again) is True


def test_score_flicker_is_not_confirmed():
    """A score that changes and changes back needs two matching polls again."""
    tracker = GameTracker()
    tracker.update(live_state(0))
    tracker.update(live_state(5))
    glitch = replace(live_state(10), away_score=31)
    tracker.update(glitch)
    back = live_state(15)
    tracker.update(back)
    assert tracker.confirmed(back) is False


def test_unknown_states_never_confirm():
    tracker = GameTracker()
    unknown = replace(live_state(0), status=GameStatus.UNKNOWN, unknown_reason="x")
    tracker.update(unknown)
    tracker.update(replace(unknown, fetched_at=at(5)))
    assert tracker.confirmed(replace(unknown, fetched_at=at(5))) is False


def test_seconds_since_score_change_tracks_the_last_change():
    tracker = GameTracker()
    tracker.update(live_state(0))
    assert tracker.seconds_since_score_change(live_state(0), at(12)) == 12
    tracker.update(live_state(5))
    assert tracker.seconds_since_score_change(live_state(5), at(30)) == 30
    scored = replace(live_state(40), away_score=31)
    tracker.update(scored)
    assert tracker.seconds_since_score_change(scored, at(41)) == 1
    tracker.update(replace(live_state(45), away_score=31))
    assert tracker.seconds_since_score_change(scored, at(100)) == 60


def test_unseen_game_has_no_score_age():
    tracker = GameTracker()
    assert tracker.seconds_since_score_change(live_state(0), at(0)) is None


def test_forget_finished_drops_games_not_in_keep_set():
    tracker = GameTracker()
    state = live_state(0)
    tracker.update(state)
    tracker.forget_finished(set())
    assert tracker.latest(GameTracker.key(state)) is None
    assert tracker.seconds_since_score_change(state, at(1)) is None
