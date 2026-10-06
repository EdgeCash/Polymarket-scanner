from __future__ import annotations

from dataclasses import replace

import pytest

from scanner.diary import OUTCOME_LOSS, OUTCOME_NOT_GRADED, OUTCOME_TIE, OUTCOME_WIN, Diary
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
    }
    card = diary.scorecard()
    assert card["near_misses_by_type"] == split
    assert card["near_misses"]["stale score"] == 2
