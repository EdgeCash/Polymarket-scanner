from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scanner.books import BookLine
from scanner.config import load_settings
from scanner.diary import Diary
from scanner.gamelog import parse_game_summary
from scanner.grid import grid
from scanner.monthly import check, month_bounds, report, summary
from scanner.projection import grade
from tests.conftest import load_fixture

CLOSE = BookLine("DraftKings", -150, 130, None, 44.5, -110, -110, -3.0, -110, -110)


def _row(margin, total, home_win, final_home, final_away, close=CLOSE, sigma=13.5, blend=None):
    projection = {
        "raw": {"margin": margin, "total": total, "home_win": home_win},
        "blend": blend or {"margin": margin, "total": total, "home_win": home_win},
        "book": {"margin": -close.home_spread, "total": close.total, "home_win": 0.58},
        "league": {"sigma": sigma, "sigma_total": sigma},
    }
    grades = grade(projection, final_home, final_away, close)
    return {"projection": projection, "grades": grades, "close": close.as_dict()}


def test_month_bounds_name_a_calendar_month_in_the_local_zone():
    now = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
    start, end, label = month_bounds(None, now, "America/New_York")
    assert label == "2026-10"
    assert start == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # midnight Eastern, daylight time
    assert end == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    start, end, label = month_bounds("2026-01", now, "UTC")
    assert (start.month, end.month, end.year, label) == (1, 2, 2026, "2026-01")
    assert month_bounds("2025-12", now, "UTC")[1] == datetime(2026, 1, 1, tzinfo=UTC)
    # Early January, the last full month is December of the year before.
    assert month_bounds(None, datetime(2026, 1, 2, tzinfo=UTC), "UTC")[2] == "2025-12"


def test_the_check_scores_the_month_against_the_finals_and_the_close():
    rows = [
        _row(6.0, 48.0, 0.67, 27, 24),  # home by 3: model's spread side (home -3) pushes
        _row(6.0, 48.0, 0.67, 31, 17),  # home by 14: wins everywhere
        _row(-1.0, 40.0, 0.47, 20, 23),  # away by 3: away side at +3 pushes, under wins
        _row(2.0, 50.0, 0.55, 17, 20),  # model liked home, away won
    ]
    out = check(rows, "nfl")
    assert out["games"] == 4
    # Brier: the model's win chances against the outcomes; the close's from its fair price.
    fair_home = CLOSE.fair_moneyline()[0]
    expected_close = sum((fair_home - o) ** 2 for o in (1, 1, 0, 0)) / 4
    assert out["brier"]["close"] == pytest.approx(expected_close)
    assert out["brier"]["model"] == pytest.approx(
        ((0.67 - 1) ** 2 + (0.67 - 1) ** 2 + (0.47 - 0) ** 2 + (0.55 - 0) ** 2) / 4
    )
    # Misses: the model's margin against the final, and the close's (home -3 means +3).
    assert out["miss"]["margin"]["model"] == pytest.approx((3 + 8 + 2 + 5) / 4)
    assert out["miss"]["margin"]["close"] == pytest.approx((0 + 11 + 6 + 6) / 4)
    assert out["miss"]["total"]["close"] == pytest.approx((6.5 + 3.5 + 1.5 + 7.5) / 4)
    # Calibration by the favourite's chance: 0.67 twice (both won); 0.53 for the away side
    # in game 3 (won) and 0.55 for the home side in game 4 (lost).
    bins = {b["bin"]: b for b in out["calibration"]}
    assert bins["60-70%"]["games"] == 2 and bins["60-70%"]["won"] == 1.0
    assert bins["50-60%"]["games"] == 2 and bins["50-60%"]["won"] == 0.5
    assert bins["50-60%"]["chance"] == pytest.approx((0.53 + 0.55) / 2)
    # Markets against the close: the side the model rates above the close's fair price,
    # settled at the final. The close has the home side at 58%, so the model's 55% in
    # game 4 is an away side, and the away side won: four for four.
    markets = out["markets"]
    assert markets["moneyline"] == {"wins": 4, "losses": 0, "pushes": 0, "rate": 1.0}
    # Spread at the close (home -3): the model takes home only when its margin beats 3,
    # so games 1 and 2 (home by 3: a push; home by 14: a win) and the away side in games
    # 3 and 4 (away by 3 covers +3 both times).
    assert markets["spread"] == {"wins": 3, "losses": 0, "pushes": 1, "rate": 1.0}
    # Total at 44.5: over, over, under, over -> win, win, win, loss.
    assert markets["total"] == {"wins": 3, "losses": 1, "pushes": 0, "rate": 0.75}
    assert "puck_line" not in markets
    # Every final sat inside the model's middle 90% here.
    assert out["outside_middle_90"] == {"margin": 0.0, "total": 0.0, "games": 4}
    # The close was the projection's book line, so the line never moved.
    assert out["clv"]["closed"] == 4 and out["clv"]["positive_share"] == 0.0


def test_a_wild_final_lands_outside_the_middle_90_and_hockey_has_a_puck_line():
    wild = _row(0.0, 44.0, 0.5, 45, 3)  # home by 42 on a pick-em
    out = check([wild], "nfl")
    assert out["outside_middle_90"]["margin"] == 1.0
    g = grid("nfl", 0.0, 44.0, 13.5, 13.5)
    assert sum(p for m, p in g.margin.items() if m >= 42) < 0.05
    puck = BookLine("DraftKings", -140, 120, None, 6.5, -110, -110, -1.5, 150, -180)
    row = _row(0.6, 6.2, 0.6, 4, 1, close=puck, sigma=2.5)
    out = check([row], "nhl")
    assert "spread" in out["markets"] and out["markets"]["spread"]["wins"] == 0
    # The model's chance of covering -1.5 against the close's fair price picks a side.
    assert out["markets"]["puck_line"]["wins"] + out["markets"]["puck_line"]["losses"] == 1
    assert out["games"] == 1 and out["brier"]["close"] is not None


def test_rows_without_a_close_still_count_toward_the_model():
    row = _row(4.0, 44.0, 0.6, 24, 20)
    row["close"] = None
    row["grades"]["clv"] = None
    out = check([row], "nfl")
    assert out["games"] == 1 and out["brier"]["model"] is not None
    assert out["brier"]["close"] is None and out["miss"]["margin"]["close"] is None
    assert out["markets"]["moneyline"] == {"wins": 0, "losses": 0, "pushes": 0, "rate": None}
    assert out["clv"]["closed"] == 0 and out["clv"]["positive_share"] is None


def test_summary_reads_the_month_from_the_diary_and_the_report_prints_it():
    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    kickoff = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
    projection = {
        "model": "v1",
        "raw": {"margin": 4.0, "total": 44.0, "home_win": 0.6},
        "blend": {"margin": 3.0, "total": 43.0, "home_win": 0.58},
        "book": {"margin": 2.0, "total": 42.0, "home_win": 0.55},
        "league": {"sigma": 13.5, "sigma_total": 13.5},
    }
    diary.upsert_projection("nfl", record.game_id, kickoff, "CLE", "PIT", projection, kickoff)
    diary.store_football_game(record, kickoff + timedelta(hours=4))
    assert diary.grade_projections("nfl", kickoff + timedelta(hours=6)) == 1
    start, end, label = month_bounds("2026-10", kickoff, "UTC")
    rows = diary.graded_projections_between(start, end)
    assert len(rows) == 1 and rows[0]["grades"]["final"] == {"home": 27, "away": 24}
    assert rows[0]["close"] is None or isinstance(rows[0]["close"], dict)
    month = summary(diary, start, end, label)
    assert month["month"] == "2026-10" and month["games"] == 1 and "nfl" in month["sports"]
    text = report(month, "UTC")
    assert text.startswith("Monthly check, 2026-10: 1 graded projections")
    assert "NFL: 1 games" in text and "Brier, model / blend / close: 0.160" in text
    assert "Outside the model's middle 90% (10% expected)" in text
    assert "Against the close:" in text and "Closing line moved the model's way" in text
    # A month with nothing in it says so.
    empty = summary(diary, *month_bounds("2026-09", kickoff, "UTC"))
    assert report(empty, "UTC").endswith("Nothing graded this month.")
    settings = load_settings()
    assert settings.TZ  # the report takes the sheet's zone, as the CLI passes it
