from __future__ import annotations

from dataclasses import replace

import pytest

from scanner.diary import (
    FOOTBALL_GAME_VERSION,
    OUTCOME_LOSS,
    OUTCOME_NOT_GRADED,
    OUTCOME_TIE,
    OUTCOME_WIN,
    Diary,
)
from scanner.models import Alert, AlertType, GameStatus, League, NearMiss
from scanner.rules import AlertHistory
from scanner.scores import parse_scoreboard
from tests.conftest import at, load_fixture


def live_state(**changes):
    raw = load_fixture("espn_states.json")
    states = {s.feed_id: s for s in parse_scoreboard(raw, League.NFL, at(0))}
    return replace(states["401873001"], **changes)  # PHI 24 at JAX 14


def winner_alert(**changes) -> Alert:
    base = Alert(
        alert_type=AlertType.WINNER,
        league=League.NFL,
        created_at=at(0),
        feed_id="401873001",
        event_slug="nfl-phi-jax-2026-10-11",
        market_slug="aec-nfl-phi-jax-2026-10-11",
        home="JAX",
        away="PHI",
        pick="PHI",
        side_label="long",
        fair_price=0.98,
        model_price=0.985,
        espn_price=0.98,
        buy_price=0.93,
        fee=0.0045245,
        edge=0.0455,
        dollars_available=180.0,
        average_price=0.93,
        situation={"seconds_left": 232.0},
        polymarket_score="24-14",
        pick_side="away",
        message="NFL - PHI at JAX ...",
        enabled=False,
    )
    return replace(base, **changes)


def over_alert(**changes) -> Alert:
    return replace(
        winner_alert(),
        alert_type=AlertType.CLINCHED_OVER,
        market_slug="tsc-nfl-phi-jax-2026-10-11-total-47pt5",
        pick="OVER 47.5",
        fair_price=0.995,
        model_price=None,
        espn_price=None,
        buy_price=0.96,
        fee=0.0026688,
        edge=0.0323,
        line=47.5,
        combined_score=52,
        pick_side=None,
        **changes,
    )


def test_alerts_near_misses_followups_and_outcomes_survive_a_restart(tmp_path):
    path = str(tmp_path / "diary.db")
    diary = Diary(path)
    alert_id = diary.record_alert(winner_alert(), sent=False)
    diary.record_near_miss(
        NearMiss(
            at(1),
            League.NFL,
            "401873001",
            "nfl-phi-jax-2026-10-11",
            "PHI",
            "rule 4: not enough for sale",
            0.98,
            0.93,
            0.04,
        )
    )
    diary.record_followup(alert_id, 30, 0.935, at(30))
    diary.grade_game(live_state(status=GameStatus.FINAL, home_score=14, away_score=27), at(3600))
    diary.close()

    reopened = Diary(path)
    row = reopened.alert(alert_id)
    assert row["pick"] == "PHI" and row["message"].startswith("NFL - PHI at JAX")
    assert row["followup_30"] == 0.935 and row["followup_30_at"] is not None
    assert row["outcome"] == OUTCOME_WIN and row["settlement"] == 1.0
    assert row["result_per_contract"] == pytest.approx(1.0 - 0.93 - 0.0045245)
    assert row["final_home"] == 14 and row["final_away"] == 27
    assert reopened.near_misses_by_reason() == {"rule 4: not enough for sale": 1}
    reopened.close()


def test_grading_a_win_a_loss_a_tie_and_a_suspended_game():
    diary = Diary(":memory:")
    win_id = diary.record_alert(winner_alert(), sent=True)
    loss_id = diary.record_alert(
        winner_alert(pick="JAX", pick_side="home", buy_price=0.07, fee=0.0045), sent=True
    )
    diary.grade_game(live_state(status=GameStatus.FINAL, home_score=14, away_score=27), at(100))
    assert diary.alert(win_id)["outcome"] == OUTCOME_WIN
    assert diary.alert(loss_id)["outcome"] == OUTCOME_LOSS
    assert diary.alert(loss_id)["result_per_contract"] == pytest.approx(0.0 - 0.07 - 0.0045)

    tie_id = diary.record_alert(winner_alert(feed_id="tie-game"), sent=True)
    diary.grade_game(
        live_state(feed_id="tie-game", status=GameStatus.FINAL, home_score=20, away_score=20),
        at(100),
    )
    assert diary.alert(tie_id)["outcome"] == OUTCOME_TIE
    assert diary.alert(tie_id)["settlement"] == 0.5
    assert diary.alert(tie_id)["result_per_contract"] == pytest.approx(0.5 - 0.93 - 0.0045245)

    suspended_id = diary.record_alert(winner_alert(feed_id="washed-out"), sent=True)
    diary.grade_game(
        live_state(feed_id="washed-out", status=GameStatus.POSTPONED, home_score=10, away_score=3),
        at(100),
    )
    row = diary.alert(suspended_id)
    assert row["outcome"] == OUTCOME_NOT_GRADED and row["settlement"] is None
    assert row["result_per_contract"] is None

    # A game that is still live grades nothing.
    live_id = diary.record_alert(winner_alert(feed_id="still-on"), sent=True)
    assert diary.grade_game(live_state(feed_id="still-on"), at(100)) == 0
    assert diary.alert(live_id)["outcome"] is None
    assert diary.ungraded_games() == {("nfl", "still-on")}


def test_grading_a_clinched_over_uses_the_final_combined_score():
    diary = Diary(":memory:")
    over_id = diary.record_alert(over_alert(), sent=True)
    diary.grade_game(live_state(status=GameStatus.FINAL, home_score=21, away_score=31), at(100))
    row = diary.alert(over_id)
    assert row["outcome"] == OUTCOME_WIN
    assert row["line"] == 47.5 and row["combined_score"] == 52
    assert row["result_per_contract"] == pytest.approx(1.0 - 0.96 - 0.0026688)
    reversed_id = diary.record_alert(over_alert(feed_id="reversed"), sent=True)
    diary.grade_game(
        live_state(feed_id="reversed", status=GameStatus.FINAL, home_score=21, away_score=24),
        at(100),
    )
    assert diary.alert(reversed_id)["outcome"] == OUTCOME_LOSS


def test_college_tie_is_not_graded():
    diary = Diary(":memory:")
    alert_id = diary.record_alert(winner_alert(league=League.CFB), sent=True)
    diary.grade_game(
        live_state(league=League.CFB, status=GameStatus.FINAL, home_score=20, away_score=20),
        at(100),
    )
    assert diary.alert(alert_id)["outcome"] == OUTCOME_NOT_GRADED


def test_pending_followups_come_due_at_30_and_120_seconds():
    diary = Diary(":memory:")
    alert_id = diary.record_alert(winner_alert(), sent=True)
    assert diary.pending_followups(at(29)) == []
    due = diary.pending_followups(at(31))
    assert [(d.alert_id, d.seconds, d.market_slug, d.side_label) for d in due] == [
        (alert_id, 30, "aec-nfl-phi-jax-2026-10-11", "long")
    ]
    diary.record_followup(alert_id, 30, None, at(31))  # a failed read is stored as None
    assert diary.pending_followups(at(60)) == []
    due = diary.pending_followups(at(121))
    assert [(d.alert_id, d.seconds) for d in due] == [(alert_id, 120)]
    diary.record_followup(alert_id, 120, 0.95, at(121))
    assert diary.pending_followups(at(500)) == []
    row = diary.alert(alert_id)
    assert row["followup_30"] is None and row["followup_30_at"] is not None
    assert row["followup_120"] == 0.95


def test_scorecard_numbers_are_split_by_type_and_league():
    diary = Diary(":memory:")
    a1 = diary.record_alert(winner_alert(), sent=True)  # NFL win, buy 0.93
    a2 = diary.record_alert(
        winner_alert(feed_id="g2", league=League.CFB, buy_price=0.95, fee=0.0033), sent=False
    )  # CFB loss
    a3 = diary.record_alert(over_alert(feed_id="g3"), sent=True)  # over, win
    diary.record_alert(winner_alert(feed_id="g4"), sent=True)  # never graded
    diary.record_followup(a1, 30, 0.93, at(30))  # still there
    diary.record_followup(a2, 30, 0.97, at(30))  # moved away
    diary.record_followup(a3, 30, None, at(30))  # read failed: counts as checked, not available
    diary.grade_game(live_state(status=GameStatus.FINAL, home_score=14, away_score=27), at(100))
    diary.grade_game(
        live_state(
            feed_id="g2", league=League.CFB, status=GameStatus.FINAL, home_score=30, away_score=27
        ),
        at(100),
    )
    diary.grade_game(
        live_state(feed_id="g3", status=GameStatus.FINAL, home_score=21, away_score=31), at(100)
    )
    diary.record_near_miss(
        NearMiss(at(1), League.NFL, "x", "slug", "PHI", "rule 3: edge too small", 0.95, 0.93, 0.01)
    )
    diary.record_near_miss(
        NearMiss(at(2), League.NFL, "x", "slug", "PHI", "rule 3: edge too small", 0.95, 0.93, 0.02)
    )
    diary.record_near_miss(
        NearMiss(at(3), League.CFB, "y", "slug", "ALA", "rule 6: repeat too soon", 0.95, 0.93, 0.05)
    )

    card = diary.scorecard()
    assert card["alerts_total"] == 4 and card["alerts_sent"] == 3
    w = card["by_type"]["winner"]
    assert w["alerts"] == 3 and w["by_league"] == {"nfl": 2, "cfb": 1}
    assert w["graded"] == 2 and w["wins"] == 1 and w["losses"] == 1 and w["ties"] == 0
    assert w["win_rate_needed"] == pytest.approx((0.9345245 * 2 + 0.9533) / 3)
    assert w["actual_win_rate"] == 0.5
    assert w["profit_per_100"] == pytest.approx(
        100 * (1 - 0.93 - 0.0045245) + 100 * (0 - 0.95 - 0.0033)
    )
    assert w["followups_checked"] == 2 and w["still_available_at_30s"] == 0.5
    o = card["by_type"]["clinched_over"]
    assert o["alerts"] == 1 and o["wins"] == 1 and o["losses"] == 0
    assert o["profit_per_100"] == pytest.approx(100 * (1 - 0.96 - 0.0026688))
    assert o["followups_checked"] == 1 and o["still_available_at_30s"] == 0.0
    assert card["near_misses"] == {"rule 3: edge too small": 2, "rule 6: repeat too soon": 1}


def test_empty_scorecard_has_no_division_by_zero():
    card = Diary(":memory:").scorecard()
    w = card["by_type"]["winner"]
    assert w["alerts"] == 0 and w["win_rate_needed"] is None and w["actual_win_rate"] is None
    assert w["profit_per_100"] == 0.0 and w["still_available_at_30s"] is None


def test_history_is_restored_after_a_restart():
    diary = Diary(":memory:")
    diary.record_alert(winner_alert(created_at=at(0)), sent=True)
    diary.record_alert(over_alert(created_at=at(10)), sent=True)
    history = AlertHistory("America/Chicago")
    assert diary.restore_history(history, at(600)) == 2
    assert history.count_today(at(600)) == 2
    assert history.last("nfl", "401873001", "PHI")[1] == pytest.approx(0.0455)


def test_events_are_logged_newest_first():
    diary = Diary(":memory:")
    diary.log_event("heartbeat", "Scanner is up, watching 3 games", at(0))
    diary.log_event("feed_failure", "Score feed is failing", at(5))
    kinds = [e["kind"] for e in diary.events()]
    assert kinds == ["feed_failure", "heartbeat"]


def test_near_misses_are_also_split_by_alert_type():
    diary = Diary(":memory:")
    diary.record_near_miss(
        NearMiss(at(1), League.NFL, "g", "s", "PHI", "stale score", 0.98, 0.93, None)
    )
    diary.record_near_miss(
        NearMiss(at(2), League.NFL, "g", "s", "PHI", "rule 3: edge too small", 0.98, 0.99, 0.004)
    )
    diary.record_near_miss(
        NearMiss(at(3), League.NFL, "g", "s", "OVER 47.5", "stale score", 0.995, 0.99, None)
    )
    diary.record_near_miss(
        NearMiss(at(4), League.NFL, "g", "s", "OVER 41.5", "edge too small", 0.995, 0.99, 0.004)
    )
    split = diary.near_misses_by_type()
    assert split == {
        "winner": {"stale score": 1, "rule 3: edge too small": 1},
        "clinched_over": {"stale score": 1, "edge too small": 1},
        "period": {},
    }
    card = diary.scorecard()
    assert card["near_misses_by_type"] == split
    assert card["near_misses"]["stale score"] == 2


# -- period markets and the observation window --------------------------------


def final_state_with_lines(home_lines, away_lines, **changes):
    state = live_state(
        status=GameStatus.FINAL,
        period=4,
        clock_seconds=0.0,
        seconds_left=0.0,
        home_score=sum(home_lines),
        away_score=sum(away_lines),
        home_linescores=tuple(home_lines),
        away_linescores=tuple(away_lines),
    )
    return replace(state, **changes)


def period_alert(**changes) -> Alert:
    from scanner.models import PeriodMarket
    from scanner.periods import PeriodSides, to_json

    market = PeriodMarket(
        "tsc-nfl-phi-jax-2026-10-11-1h-24pt5", "1h", "total", 24.5, 0.0695, True, False, True, True
    )
    sides = PeriodSides(home_abbr="JAX", away_abbr="PHI")
    base = replace(
        winner_alert(),
        alert_type=AlertType.PERIOD,
        market_slug=market.market_slug,
        pick="1H OVER 24.5",
        side_label="long",
        fair_price=0.995,
        model_price=None,
        espn_price=None,
        buy_price=0.96,
        fee=0.0026688,
        edge=0.0323,
        line=24.5,
        combined_score=27,
        pick_side=None,
        situation={
            "period_market": to_json(market, sides),
            "period_detail": "1st half ended with 27 points",
        },
    )
    return replace(base, **changes)


def test_period_alerts_are_graded_from_the_final_per_period_scores():
    diary = Diary(":memory:")
    over_id = diary.record_alert(period_alert(), sent=False)
    under_id = diary.record_alert(
        period_alert(pick="1H UNDER 24.5", side_label="short"), sent=False
    )
    # JAX 3+7, PHI 14+3 in the first half: 27 points, the Over won and the Under lost.
    graded = diary.grade_game(final_state_with_lines([3, 7, 7, 0], [14, 3, 0, 14]), at(100))
    assert graded == 2
    assert diary.alert(over_id)["outcome"] == OUTCOME_WIN
    assert diary.alert(over_id)["result_per_contract"] == pytest.approx(1 - 0.96 - 0.0026688)
    assert diary.alert(under_id)["outcome"] == OUTCOME_LOSS
    card = diary.scorecard()
    assert card["by_type"]["period"]["alerts"] == 2
    assert card["by_type"]["period"]["wins"] == 1 and card["by_type"]["period"]["losses"] == 1


def test_period_alert_without_per_period_scores_at_the_final_is_not_graded():
    diary = Diary(":memory:")
    alert_id = diary.record_alert(period_alert(), sent=False)
    diary.grade_game(
        final_state_with_lines(
            [3, 7, 7, 0], [14, 3, 0, 14], home_linescores=(), away_linescores=()
        ),
        at(100),
    )
    assert diary.alert(alert_id)["outcome"] == OUTCOME_NOT_GRADED
    diary = Diary(":memory:")
    alert_id = diary.record_alert(period_alert(situation={}), sent=False)
    diary.grade_game(final_state_with_lines([3, 7, 7, 0], [14, 3, 0, 14]), at(100))
    assert diary.alert(alert_id)["outcome"] == OUTCOME_NOT_GRADED


def test_near_misses_on_period_picks_land_in_their_own_bucket():
    diary = Diary(":memory:")
    diary.record_near_miss(
        NearMiss(at(1), League.NFL, "g", "s", "1Q PHI +2.5", "market not open", 0.995, None, None)
    )
    diary.record_near_miss(
        NearMiss(at(2), League.NFL, "g", "s", "1H OVER 24.5", "edge too small", 0.995, 0.99, 0.004)
    )
    assert diary.near_misses_by_type()["period"] == {"market not open": 1, "edge too small": 1}
    assert diary.near_misses_by_type()["winner"] == {}


def test_restore_history_keeps_period_alerts_out_of_the_winner_cap():
    diary = Diary(":memory:")
    diary.record_alert(winner_alert(), sent=True)
    diary.record_alert(period_alert(), sent=False)
    history, period_history = AlertHistory("America/Chicago"), AlertHistory("America/Chicago")
    assert diary.restore_history(history, at(60), period_history) == 2
    assert history.count_today(at(60)) == 1 and period_history.count_today(at(60)) == 1
    only = AlertHistory("America/Chicago")
    assert diary.restore_history(only, at(60)) == 1


def observation(**changes):
    from scanner.models import Observation

    base = Observation(
        created_at=at(0),
        league=League.NFL,
        feed_id="401873001",
        event_slug="nfl-phi-jax-2026-10-11",
        pick="PHI",
        pick_side="away",
        minutes_left=10.0,
        home_score=14,
        away_score=24,
        fair_price=0.96,
        model_price=0.97,
        espn_price=0.96,
        buy_price=0.90,
        fee=0.0056,
        edge=0.0544,
        dollars_available=180.0,
        would_alert=True,
        reason="alert",
    )
    return replace(base, **changes)


def test_observations_are_recorded_graded_and_counted_once_per_game_and_pick():
    diary = Diary(":memory:")
    diary.record_observation(
        observation(
            would_alert=False,
            reason="rule 5: score changed recently",
            edge=None,
            dollars_available=None,
        )
    )
    diary.record_observation(observation(created_at=at(30)))
    diary.record_observation(observation(created_at=at(60), buy_price=0.91))
    diary.record_observation(
        observation(feed_id="other", event_slug="nfl-x-y", pick="JAX", pick_side="home")
    )
    assert diary.ungraded_games() == {("nfl", "401873001"), ("nfl", "other")}
    summary = diary.observation_summary()
    assert summary["rows"] == 4 and summary["games"] == 2
    assert summary["would_alert_rows"] == 3 and summary["picks"] == 2
    assert summary["graded"] == 0 and summary["reasons"] == {"rule 5: score changed recently": 1}
    assert summary["win_rate_needed"] == pytest.approx(0.90 + 0.0056)  # the first check per pick
    # PHI won the first game; the other game's home pick lost.
    assert (
        diary.grade_game(live_state(status=GameStatus.FINAL, home_score=21, away_score=31), at(500))
        == 3
    )
    assert (
        diary.grade_game(
            live_state(feed_id="other", status=GameStatus.FINAL, home_score=10, away_score=20),
            at(500),
        )
        == 1
    )
    summary = diary.observation_summary()
    assert summary["graded"] == 2 and summary["wins"] == 1 and summary["losses"] == 1
    assert summary["profit_per_100"] == pytest.approx(
        100 * (1 - 0.90 - 0.0056) + 100 * (0 - 0.90 - 0.0056)
    )
    rows = diary.observations_since(at(0))
    assert [r["outcome"] for r in rows] == [OUTCOME_WIN, OUTCOME_WIN, OUTCOME_WIN, OUTCOME_LOSS]
    assert diary.scorecard()["observations"]["picks"] == 2


OLD_FOOTBALL_GAMES = """
CREATE TABLE football_games (
    sport TEXT NOT NULL, game_id TEXT NOT NULL, season INTEGER, week INTEGER,
    date TEXT NOT NULL, neutral INTEGER NOT NULL DEFAULT 0,
    home_id TEXT NOT NULL, home_abbr TEXT NOT NULL, home_name TEXT NOT NULL,
    away_id TEXT NOT NULL, away_abbr TEXT NOT NULL, away_name TEXT NOT NULL,
    home_score INTEGER NOT NULL, away_score INTEGER NOT NULL,
    home_lines TEXT NOT NULL, away_lines TEXT NOT NULL,
    home_stats TEXT NOT NULL, away_stats TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (sport, game_id)
);
"""


def test_an_older_diary_gains_the_football_columns_and_its_rows_read_as_stale(tmp_path):
    import sqlite3

    from scanner.gamelog import parse_game_summary

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_FOOTBALL_GAMES)
    conn.execute(
        "INSERT INTO football_games VALUES ('nfl', 'old-1', 2026, 3, '2026-09-27T17:00:00+00:00',"
        " 0, '5', 'CLE', 'Cleveland Browns', '23', 'PIT', 'Pittsburgh Steelers', 27, 24,"
        " '[0, 21, 0, 6]', '[7, 3, 7, 7]', '{\"points\": 27}', '{\"points\": 24}',"
        " '2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    diary = Diary(path)
    assert diary.football_games_stale("nfl") == ["old-1"]
    old = diary.football_games("nfl")[0]
    assert old.game_id == "old-1" and old.book is None and old.home_stats == {"points": 27}
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    diary.store_football_game(record, at(0))
    assert diary.football_games_stale("nfl") == ["old-1"]  # the new row is current
    fresh = [g for g in diary.football_games("nfl") if g.game_id == record.game_id][0]
    assert fresh.book is not None and fresh.book.total == 38.5
    diary.mark_football_game_version("nfl", "old-1", FOOTBALL_GAME_VERSION)
    assert diary.football_games_stale("nfl") == [] and diary.football_games_stale("cfb") == []
    diary.close()
    Diary(path).close()  # opening a migrated diary again changes nothing


def test_projections_update_until_graded_from_the_log_and_feed_the_scorecard():
    from datetime import UTC, datetime, timedelta

    from scanner.gamelog import parse_game_summary

    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    kickoff = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)
    first = {
        "model": "v1",
        "raw": {"margin": 4.0, "total": 44.0, "home_win": 0.6},
        "blend": {"margin": 3.0, "total": 43.0, "home_win": 0.58},
        "book": {"margin": 2.0, "total": 42.0, "home_win": 0.55},
    }
    later = {**first, "raw": {"margin": 5.0, "total": 45.0, "home_win": 0.62}}
    t0 = kickoff - timedelta(hours=30)
    diary.upsert_projection("nfl", record.game_id, kickoff, "CLE", "PIT", first, t0)
    diary.upsert_projection(
        "nfl", record.game_id, kickoff, "CLE", "PIT", later, t0 + timedelta(hours=1)
    )
    row = diary.projection("nfl", record.game_id)
    assert row["projection"] == later and row["first_projection"] == first
    assert (
        row["made_at"] == t0.isoformat()
        and row["updated_at"] == (t0 + timedelta(hours=1)).isoformat()
    )
    assert row["outcome"] is None and row["grades"] is None and row["model"] == "v1"
    assert list(diary.open_projections("nfl")) == [record.game_id]
    assert diary.open_projections("cfb") == {}

    # Not graded within the grace period, nor before the game's record is in the log.
    assert diary.grade_projections("nfl", kickoff + timedelta(hours=1)) == 0
    assert diary.grade_projections("nfl", kickoff + timedelta(hours=6)) == 0
    diary.store_football_game(record, kickoff + timedelta(hours=4))
    assert diary.grade_projections("nfl", kickoff + timedelta(hours=6)) == 1
    row = diary.projection("nfl", record.game_id)
    assert row["outcome"] == "graded" and row["grades"]["final"] == {"home": 27, "away": 24}
    assert row["grades"]["ats"] == {"side": "home", "gap": 3.0, "result": "win"}  # 3 beat the 2
    assert row["grades"]["clv"]["margin"] == pytest.approx(-2.5 - 2.0)  # closed CLE +2.5
    assert diary.open_projections("nfl") == {}
    diary.upsert_projection("nfl", record.game_id, kickoff, "CLE", "PIT", first, kickoff)
    assert diary.projection("nfl", record.game_id)["projection"] == later  # graded rows are fixed

    # A game that never lands in the log is given up on after ten days.
    ghost_kickoff = kickoff - timedelta(days=12)
    diary.upsert_projection("nfl", "ghost", ghost_kickoff, "AAA", "BBB", first, ghost_kickoff)
    assert diary.grade_projections("nfl", kickoff) == 0
    assert diary.projection("nfl", "ghost")["outcome"] == OUTCOME_NOT_GRADED

    summary = diary.projection_summary()
    assert summary["graded"] == 1 and summary["open"] == 0 and summary["by_sport"] == {"nfl": 1}
    assert summary["margin_error"]["raw"] == 2.0 and summary["ats"]["3"]["wins"] == 1
    recent = summary["recent"][0]
    assert (recent["home"], recent["away"], recent["raw_margin"]) == ("CLE", "PIT", 5.0)
    assert recent["final"] == {"home": 27, "away": 24} and recent["ou"]["side"] == "over"
    assert diary.projection_summary(since=kickoff + timedelta(days=1))["graded"] == 0
    assert diary.scorecard()["projections"]["graded"] == 1


def stake_row(**changes):
    base = {
        "market": "moneyline",
        "side": "home",
        "side_label": "CLE",
        "line": None,
        "buy_price": 0.5,
        "fee": 0.017375,
        "model_prob": 0.62,
        "edge": 0.102625,
        "kelly": 0.2126,
        "share": 0.05,
        "stake": 50.0,
        "contracts": 96.64,
    }
    base.update(changes)
    return base


def test_stakes_follow_the_suggestions_until_kickoff_then_lock_and_grade():
    from datetime import UTC, datetime, timedelta

    from scanner.gamelog import parse_game_summary

    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    gid = record.game_id
    kickoff = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)
    t0 = kickoff - timedelta(hours=30)
    total = stake_row(
        market="total",
        side="over",
        side_label="over 45.5",
        line=45.5,
        edge=0.07,
        stake=39.7,
        contracts=76.7,
    )
    seen = (t0 - timedelta(minutes=5)).isoformat()
    assert (
        diary.sync_stakes(
            "nfl", gid, kickoff, "CLE", "PIT", [stake_row(), total], False, 1000, seen, t0
        )
        == 2
    )
    rows = diary.stakes_for("nfl", gid)
    assert [r["market"] for r in rows] == ["moneyline", "total"]  # biggest edge first
    assert rows[0]["gate_open"] == 0 and rows[0]["bankroll"] == 1000 and rows[0]["price_at"] == seen
    assert rows[0]["created_at"] == t0.isoformat() and rows[0]["outcome"] is None

    # Next pass: the total's edge is gone and the moneyline side has flipped.
    flipped = stake_row(side="away", side_label="PIT", buy_price=0.48, model_prob=0.56)
    later = t0 + timedelta(hours=1)
    assert (
        diary.sync_stakes("nfl", gid, kickoff, "CLE", "PIT", [flipped], True, 1000, seen, later)
        == 1
    )
    rows = diary.stakes_for("nfl", gid)
    assert len(rows) == 1 and rows[0]["side"] == "away" and rows[0]["gate_open"] == 1
    assert rows[0]["created_at"] == t0.isoformat() and rows[0]["updated_at"] == later.isoformat()
    # From kickoff on, nothing changes.
    assert diary.sync_stakes("nfl", gid, kickoff, "CLE", "PIT", [], True, 1000, seen, kickoff) == 0
    assert len(diary.stakes_for("nfl", gid)) == 1

    assert diary.grade_stakes("nfl", kickoff + timedelta(hours=1)) == 0  # grace period
    assert diary.grade_stakes("nfl", kickoff + timedelta(hours=6)) == 0  # game not in the log
    diary.store_football_game(record, kickoff + timedelta(hours=4))  # CLE 27, PIT 24
    assert diary.grade_stakes("nfl", kickoff + timedelta(hours=6)) == 1
    row = diary.stakes_for("nfl", gid)[0]
    assert row["outcome"] == "loss" and row["settlement"] == 0.0
    assert row["profit"] == pytest.approx(96.64 * (0 - 0.48) - 96.64 * 0.017375)
    assert (row["final_home"], row["final_away"]) == (27, 24)
    # A graded row is fixed: a new pass cannot rewrite it.
    diary.sync_stakes("nfl", gid, kickoff, "CLE", "PIT", [stake_row()], True, 1000, seen, t0)
    assert diary.stakes_for("nfl", gid)[0]["side"] == "away"

    # A game that never lands is given up on after ten days.
    ghost = kickoff - timedelta(days=12)
    diary.sync_stakes(
        "nfl",
        "ghost",
        ghost,
        "AAA",
        "BBB",
        [stake_row()],
        False,
        1000,
        None,
        ghost - timedelta(days=1),
    )
    assert diary.grade_stakes("nfl", kickoff) == 0
    assert diary.stakes_for("nfl", "ghost")[0]["outcome"] == OUTCOME_NOT_GRADED

    summary = diary.stake_summary()
    assert summary["suggested"] == 2 and summary["open"] == 0 and summary["shown"] == 1
    assert summary["graded"] == 1 and summary["not_graded"] == 1 and summary["losses"] == 1
    assert summary["staked"] == 50.0 and summary["profit"] == pytest.approx(row["profit"])
    assert summary["roi"] == pytest.approx(row["profit"] / 50.0)
    assert summary["by_market"]["moneyline"] == {
        "suggested": 2,
        "wins": 0,
        "losses": 1,
        "pushes": 0,
        "profit": pytest.approx(row["profit"]),
    }
    assert summary["recent"][0]["side_label"] == "PIT" and summary["recent"][0]["gate_open"]
    assert diary.stake_summary(since=kickoff + timedelta(days=1))["suggested"] == 0
    assert diary.scorecard()["stakes"]["graded"] == 1
