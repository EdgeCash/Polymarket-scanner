"""The history fetch's book lines: parsed from ESPN's summary, laid beside the ratings."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.conftest import load_fixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def history():
    spec = importlib.util.spec_from_file_location(
        "fetch_history", ROOT / "scripts" / "fetch_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_summary_s_pickcenter_gives_the_home_spread_and_total_open_and_close(history):
    lines = history.parse_lines(load_fixture("espn_cbb_summary_final.json"))
    assert lines == {
        "spread_open": -42.5,
        "spread_close": -44.5,
        "total_open": 150.5,
        "total_close": 157.5,
    }
    assert history.parse_lines({}) == dict.fromkeys(lines)
    # Only the headline numbers, as older seasons' summaries keep them.
    bare = {"pickcenter": [{"spread": 3.0, "overUnder": 141.0}]}
    assert history.parse_lines(bare) == {
        "spread_open": None,
        "spread_close": 3.0,
        "total_open": None,
        "total_close": 141.0,
    }
    assert history._line_value("o157.5") == 157.5 and history._line_value("u140") == 140.0
    assert history._line_value("EVEN") == 0.0 and history._line_value("+3") == 3.0
    assert history._line_value("OFF") is None and history._line_value(None) is None


def _row(game_id: str, day: str, home: str, away: str, hs: int, aws: int) -> dict:
    return {
        "id": game_id,
        "date": day,
        "start": f"{day}T00:00Z",
        "season": 2025,
        "neutral": False,
        "home": {"id": home, "score": hs, "lines": [hs // 2, hs - hs // 2]},
        "away": {"id": away, "score": aws, "lines": [aws // 2, aws - aws // 2]},
    }


def test_the_lines_table_holds_each_rated_game_with_its_close_and_games_so_far(history, tmp_path):
    # Two sides meet four times: the fourth game is the first either side is rated for.
    rows = [
        _row("1", "2024-11-05", "A", "B", 80, 60),
        _row("2", "2024-11-08", "B", "A", 70, 75),
        _row("3", "2024-11-11", "A", "B", 90, 70),
        _row("4", "2024-11-14", "B", "A", 65, 85),
    ]
    with open(tmp_path / "cbb.jsonl", "w") as out:
        out.writelines(json.dumps(r) + "\n" for r in rows)
    lines = [
        {"id": "1", "spread_close": -10.5, "total_close": 140.0},
        {"id": "4", "spread_open": 14.0, "spread_close": 15.5, "total_open": 139.5},
    ]
    lines[1]["total_close"] = 141.5
    with open(tmp_path / "cbb_lines.jsonl", "w") as out:
        out.writelines(json.dumps(r) + "\n" for r in lines)
    table = history.build_lines(tmp_path)
    assert len(table) == 1
    game = table.iloc[0]
    assert (game.season, game.date, game.home_games, game.away_games) == (2025, "2024-11-14", 3, 3)
    # Margins are the home side's, positive when favoured: B is the home side, a 15.5 dog.
    assert game.book_margin == -15.5 and game.book_margin_open == -14.0
    assert game.book_total == 141.5 and game.book_total_open == 139.5
    assert game.exp_margin < 0 and (game.home_final, game.away_final, game.ot) == (65, 85, 0)
    # Rated games without a close are left out; a game with a close but no rating too.
    rated = history.rate_before(history.load_games(tmp_path, "cbb"), "cbb")
    assert list(rated["id"]) == ["4"] and list(rated.home_games) == [3]
