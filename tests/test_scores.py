from __future__ import annotations

import pytest

from scanner.models import GameStatus, League, Side
from scanner.scores import ScoreFeedError, parse_event, parse_scoreboard
from tests.conftest import at, load_fixture


def states_by_id():
    raw = load_fixture("espn_states.json")
    return {s.feed_id: s for s in parse_scoreboard(raw, League.NFL, at(0))}


def test_real_scoreboards_parse_without_unknowns():
    nfl = parse_scoreboard(load_fixture("espn_nfl_scoreboard.json"), League.NFL, at(0))
    cfb = parse_scoreboard(load_fixture("espn_cfb_scoreboard.json"), League.CFB, at(0))
    assert len(nfl) == 16 and len(cfb) == 58
    assert all(s.status is not GameStatus.UNKNOWN for s in nfl + cfb)
    assert {s.status for s in nfl} == {GameStatus.FINAL, GameStatus.PRE}
    assert all(s.status is GameStatus.PRE for s in cfb)
    assert all(s.kickoff is not None for s in nfl + cfb)
    assert all(s.fetched_at == at(0) for s in nfl + cfb)


def test_pre_game_state():
    s = states_by_id()["401873014"]
    assert s.status is GameStatus.PRE
    assert s.seconds_left == 3600
    assert s.home.abbreviation == "JAX" and s.away.abbreviation == "PHI"
    assert s.home.location == "Jacksonville"
    assert s.possession is None
    assert s.home_spread == -3.5  # "JAX -3.5", JAX is home


def test_live_fourth_quarter_state_with_full_situation():
    s = states_by_id()["401873001"]
    assert s.status is GameStatus.LIVE
    assert s.period == 4
    assert s.clock_seconds == 232.0
    assert s.seconds_left == 232.0
    assert (s.away_score, s.home_score) == (24, 14)
    assert s.possession is Side.AWAY  # PHI has the ball
    assert s.down == 2 and s.distance == 7
    assert s.yards_to_endzone == 35  # at the JAX 35, driving toward JAX's goal
    assert s.home_timeouts == 2 and s.away_timeouts == 3
    assert s.espn_home_win_probability == pytest.approx(0.02)
    assert s.home_spread == 3.5  # "PHI -3.5", PHI is away
    assert not s.is_overtime
    assert s.total_points == 38


def test_home_team_at_its_own_35_has_65_yards_to_go():
    s = states_by_id()["401873016"]
    assert s.possession is Side.HOME
    assert s.yards_to_endzone == 65


def test_halftime_state():
    s = states_by_id()["401873002"]
    assert s.status is GameStatus.HALFTIME
    assert s.period == 2 and s.seconds_left == 1800


def test_end_of_period_counts_as_live_with_no_down():
    s = states_by_id()["401873003"]
    assert s.status is GameStatus.LIVE
    assert s.seconds_left == 900
    assert s.down is None  # down 0 is "no down"
    assert s.possession is Side.AWAY


def test_final_in_overtime():
    s = states_by_id()["401873004"]
    assert s.status is GameStatus.FINAL
    assert s.is_overtime and s.period == 5
    assert s.seconds_left == 0


def test_live_overtime_has_zero_regulation_seconds_left():
    s = states_by_id()["401873005"]
    assert s.status is GameStatus.LIVE and s.is_overtime
    assert s.seconds_left == 0
    assert s.espn_home_win_probability == pytest.approx(0.55)


def test_weather_delay_and_postponement():
    states = states_by_id()
    assert states["401873006"].status is GameStatus.DELAYED
    assert states["401873007"].status is GameStatus.POSTPONED


@pytest.mark.parametrize(
    "feed_id, reason",
    [
        ("401873008", "score malformed"),
        ("401873009", "clock malformed"),
        ("401873010", "live with period 0"),
        ("401873013", "status 'STATUS_SOMETHING_NEW' unknown"),
        ("401873017", "home/away missing"),
    ],
)
def test_malformed_fields_produce_unknown_not_a_guess(feed_id, reason):
    s = states_by_id()[feed_id]
    assert s.status is GameStatus.UNKNOWN
    assert s.unknown_reason == reason
    assert s.home_score is None and s.seconds_left is None


def test_missing_situation_leaves_situation_fields_none_but_scores_valid():
    s = states_by_id()["401873011"]
    assert s.status is GameStatus.LIVE
    assert s.home_score == 14 and s.away_score == 24
    assert s.possession is None and s.down is None and s.yards_to_endzone is None
    assert s.home_timeouts is None and s.espn_home_win_probability is None


def test_contradictory_yard_line_is_dropped_rather_than_guessed():
    s = states_by_id()["401873012"]
    assert s.status is GameStatus.LIVE
    assert s.possession is Side.AWAY
    assert s.yards_to_endzone is None


def test_possession_id_matching_neither_team_is_none():
    s = states_by_id()["401873015"]
    assert s.possession is None
    assert s.yards_to_endzone is None


def test_spread_that_disagrees_with_its_text_is_dropped():
    s = states_by_id()["401873018"]
    assert s.home_spread is None


def test_scoreboard_without_events_raises():
    with pytest.raises(ScoreFeedError):
        parse_scoreboard({"foo": "bar"}, League.NFL, at(0))
    with pytest.raises(ScoreFeedError):
        parse_scoreboard("not json", League.NFL, at(0))


def test_event_without_id_is_skipped_and_garbage_entries_ignored():
    raw = {"events": [{"no": "id"}, "garbage", 42]}
    assert parse_scoreboard(raw, League.NFL, at(0)) == []
    assert parse_event({"no": "id"}, League.NFL, at(0)) is None


def test_situation_key_changes_when_anything_that_matters_changes():
    base = states_by_id()["401873001"]
    other = states_by_id()["401873016"]
    assert base.situation_key() != other.situation_key()
    assert base.situation_key() == base.situation_key()
