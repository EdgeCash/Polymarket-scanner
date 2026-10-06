from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from scanner.config import load_settings
from scanner.diary import Diary
from scanner.gamelog import (
    METRICS,
    FootballLog,
    GameLogError,
    GameRecord,
    TeamRef,
    aggregate,
    build_sheet,
    first_half,
    last_n,
    league_ranks,
    parse_game_summary,
    parse_upcoming_summary,
    parse_week_scoreboard,
    rank_values,
    sheet_from_diary,
    streak,
    team_games,
)
from tests.conftest import load_fixture

NOW = datetime.fromisoformat("2026-10-06T12:00:00+00:00")


# -- parsing ----------------------------------------------------------------------


def test_weekly_scoreboard_parses_finished_games_with_lines_and_records():
    season, week, games = parse_week_scoreboard(load_fixture("espn_nfl_week4.json"), "nfl")
    assert (season, week, len(games)) == (2026, 4, 3)
    game = games[0]
    assert game.game_id == "401872964" and game.completed and game.status == "STATUS_FINAL"
    assert (game.home.abbreviation, game.away.abbreviation) == ("CLE", "PIT")
    assert game.home.name == "Cleveland Browns" and game.home.team_id == "5"
    assert (game.home_score, game.away_score) == (27, 24)
    assert game.home_lines == (0, 21, 0, 6) and game.away_lines == (7, 3, 0, 14)
    assert game.records["home"] == {"total": "3-1", "home": "2-0", "road": "1-1"}
    assert game.venue["name"] == "Huntington Bank Field" and game.venue["indoor"] is False
    assert game.neutral is False and game.book is None and game.ranks["home"] is None
    assert game.date.isoformat() == "2026-10-02T00:15:00+00:00"
    assert game.as_dict()["home"]["abbreviation"] == "CLE"
    cfb_season, cfb_week, cfb_games = parse_week_scoreboard(
        load_fixture("espn_cfb_week4.json"), "cfb"
    )
    assert (cfb_season, cfb_week, len(cfb_games)) == (2026, 4, 3)


def test_nfl_summary_becomes_a_game_record_with_both_box_scores():
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    assert record is not None and record.game_id == "401872964"
    assert (record.season, record.week) == (2026, 4)
    assert (record.home.abbreviation, record.away.abbreviation) == ("CLE", "PIT")
    assert (record.home_score, record.away_score) == (27, 24)
    assert record.home_lines == (0, 21, 0, 6)
    home, away = record.home_stats, record.away_stats
    assert home["points"] == 27 and home["first_half_points"] == 21
    assert away["points"] == 24 and away["first_half_points"] == 10
    assert home["pass_td"] + home["rush_td"] >= 3 and away["pass_td"] + away["rush_td"] >= 3
    assert home["third_att"] > 0 and home["pass_att"] > 0 and home["rush_att"] > 0
    assert home["plays"] is not None and home["sacks_taken"] is not None
    assert home["possession_seconds"] is not None and 0 < home["possession_seconds"] < 3600
    assert home["total_yards"] > 0 and home["penalty_yards"] is not None


def test_college_summary_lacks_sacks_so_plays_are_counted_from_attempts():
    record = parse_game_summary(load_fixture("espn_cfb_summary_final.json"), "cfb")
    assert record is not None and (record.home.abbreviation, record.away.abbreviation) == (
        "CCU",
        "LIB",
    )
    assert (record.home_score, record.away_score) == (17, 34)
    assert record.home_stats["sacks_taken"] is None
    assert (
        record.home_stats["plays"] == record.home_stats["pass_att"] + record.home_stats["rush_att"]
    )
    assert record.away_stats["first_half_points"] == 10
    assert parse_game_summary({"header": {}}, "cfb") is None
    assert parse_game_summary("nonsense", "cfb") is None


def test_upcoming_summary_gives_venue_weather_and_projection():
    extra = parse_upcoming_summary(load_fixture("espn_cfb_summary_upcoming.json"))
    assert extra["weather"] == {"temperature": 74, "precipitation": 0, "gust": 5}
    assert extra["venue"] == {"name": "Veterans Memorial Stadium (AL)", "grass": False}
    assert extra["predictor"] == {"home": 67.7, "away": 32.3}
    assert parse_upcoming_summary({}) == {}


# -- figures ---------------------------------------------------------------------


A, B, C, D = (
    TeamRef("1", "AAA", "Team A"),
    TeamRef("2", "BBB", "Team B"),
    TeamRef("3", "CCC", "Team C"),
    TeamRef("4", "DDD", "Team D"),
)


def stats(
    points,
    first_half,
    pass_yards,
    rush_yards,
    third=(5, 12),
    turnovers=1,
    plays=60,
    sacks=2,
    pass_td=2,
    rush_td=1,
):
    return {
        "points": points,
        "first_half_points": first_half,
        "first_downs": 20,
        "third_conv": third[0],
        "third_att": third[1],
        "fourth_conv": 0,
        "fourth_att": 1,
        "total_yards": pass_yards + rush_yards,
        "pass_yards": pass_yards,
        "pass_comp": 20,
        "pass_att": 30,
        "rush_yards": rush_yards,
        "rush_att": 28,
        "penalties": 6,
        "penalty_yards": 50,
        "turnovers": turnovers,
        "fumbles_lost": 0,
        "interceptions": turnovers,
        "possession_seconds": 1800,
        "plays": plays,
        "sacks_taken": sacks,
        "pass_td": pass_td,
        "rush_td": rush_td,
    }


def game(game_id, day, home, away, home_stats, away_stats, neutral=False, week=None):
    return GameRecord(
        "nfl",
        game_id,
        2026,
        week or day,
        NOW - timedelta(days=40 - day * 7),
        neutral,
        home,
        away,
        home_stats["points"],
        away_stats["points"],
        (home_stats["first_half_points"], 0, 0, 0),
        (away_stats["first_half_points"], 0, 0, 0),
        home_stats,
        away_stats,
    )


RECORDS = [
    game("g1", 1, A, B, stats(30, 17, 250, 120), stats(10, 3, 150, 80, turnovers=3)),
    game("g2", 2, C, A, stats(21, 14, 200, 100), stats(24, 10, 300, 90)),
    game("g3", 3, A, D, stats(14, 7, 180, 60, turnovers=2), stats(28, 21, 260, 140, turnovers=0)),
    game("g4", 4, B, A, stats(17, 10, 220, 110), stats(35, 21, 310, 150, turnovers=0)),
    game("g5", 2, B, D, stats(20, 10, 210, 100), stats(23, 14, 240, 130)),
]


def test_team_games_are_oldest_first_from_either_side():
    games = team_games(RECORDS, "1")
    assert [g.game_id for g in games] == ["g1", "g2", "g3", "g4"]
    assert [g.home for g in games] == [True, False, True, False]
    assert [g.points for g in games] == [30, 24, 14, 35] and [g.allowed for g in games] == [
        10,
        21,
        28,
        17,
    ]
    assert games[0].opponent.abbreviation == "BBB" and games[0].won and not games[2].won
    assert streak(games) == "W1" and streak(games[:3]) == "L1" and streak(games[:2]) == "W2"
    assert streak([]) == "" and last_n(games, 3) == games[1:] and team_games(RECORDS, "9") == []


def test_aggregate_computes_per_game_figures_and_rates():
    games = team_games(RECORDS, "1")
    figures = aggregate(games)
    assert figures["ppg"] == pytest.approx((30 + 24 + 14 + 35) / 4)
    assert figures["ppg_allowed"] == pytest.approx((10 + 21 + 28 + 17) / 4)
    assert figures["pass_ypg"] == pytest.approx((250 + 300 + 180 + 310) / 4)
    assert figures["rush_ypg_allowed"] == pytest.approx((80 + 100 + 140 + 110) / 4)
    assert figures["third_pct"] == pytest.approx(100 * 20 / 48)
    assert figures["ypp"] == pytest.approx((370 + 390 + 240 + 460) / 240)
    assert figures["turnovers"] == pytest.approx((1 + 1 + 2 + 0) / 4)
    assert figures["takeaways"] == pytest.approx((3 + 1 + 0 + 1) / 4)
    assert figures["to_margin"] == pytest.approx(figures["takeaways"] - figures["turnovers"])
    assert figures["sacks"] == pytest.approx(2.0) and figures["pace"] == pytest.approx(60.0)
    assert figures["possession"] == pytest.approx(30.0) and figures[
        "penalty_yards"
    ] == pytest.approx(50.0)
    assert first_half(games) == {
        "ppg": pytest.approx((17 + 10 + 7 + 21) / 4),
        "ppg_allowed": pytest.approx((3 + 14 + 21 + 10) / 4),
    }
    empty = aggregate([])
    assert all(v is None for v in empty.values()) and first_half([]) == {
        "ppg": None,
        "ppg_allowed": None,
    }
    assert set(empty) == {m.key for m in METRICS}


def test_missing_stats_are_left_out_of_the_averages():
    games = team_games(RECORDS, "1")
    no_sacks = [replace(g, own={**g.own, "sacks_taken": None, "plays": None}) for g in games]
    figures = aggregate(no_sacks)
    assert figures["sacks_allowed"] is None and figures["pace"] is None and figures["ypp"] is None
    assert figures["ppg"] == pytest.approx(103 / 4)


def test_ranks_count_strictly_better_teams():
    assert rank_values({"a": 30.0, "b": 20.0, "c": 20.0, "d": None}, True) == {
        "a": 1,
        "b": 2,
        "c": 2,
    }
    assert rank_values({"a": 30.0, "b": 20.0, "c": 25.0}, False) == {"a": 3, "b": 1, "c": 2}
    ranks, size = league_ranks(RECORDS, "season")
    assert size == 4 and ranks["1"]["ppg"] == 1 and ranks["2"]["ppg"] == 4
    assert ranks["3"]["ppg"] == 3 and ranks["4"]["ppg"] == 2
    assert ranks["4"]["ppg_allowed"] == 1 and ranks["1"]["ppg_allowed"] == 2  # lower is better
    assert "pace" not in ranks["1"]  # neither direction is better
    last3, _ = league_ranks(RECORDS, "last3")
    assert last3["4"]["ppg"] == 1 and last3["1"]["ppg"] == 2  # A: 24, 14, 35; D: 28, 23


# -- the sheet ----------------------------------------------------------------------


def upcoming_row(game_id="u1", home=A, away=B, book=None, extra=None):
    slate = {
        "sport": "nfl",
        "game_id": game_id,
        "season": 2026,
        "week": 6,
        "date": (NOW + timedelta(hours=30)).isoformat(),
        "completed": False,
        "neutral": False,
        "home": {"team_id": home.team_id, "abbreviation": home.abbreviation, "name": home.name},
        "away": {"team_id": away.team_id, "abbreviation": away.abbreviation, "name": away.name},
        "records": {
            "home": {"total": "3-1", "home": "2-0", "road": "1-1"},
            "away": {"total": "1-3"},
        },
        "ranks": {"home": None, "away": 14},
        "venue": {"name": "Big Stadium", "city": "Troy", "state": "AL", "indoor": False},
        "book": book,
    }
    return {"sport": "nfl", "game_id": game_id, "slate": slate, "extra": extra or {}}


def test_build_sheet_fills_both_teams_and_advantages():
    extra = {
        "weather": {"temperature": 74, "precipitation": 0, "gust": 5},
        "venue": {"grass": False},
        "predictor": {"home": 60.0, "away": 40.0},
    }
    sheet = build_sheet(upcoming_row(extra=extra), RECORDS, extra, {"home": 0.55, "away": 0.47})
    home, away = sheet["home"], sheet["away"]
    assert home["abbreviation"] == "AAA" and home["games"] == 4 and away["games"] == 3
    assert home["split_label"] == "Home" and home["split_games"] == 2  # A's home games: g1, g3
    assert away["split_label"] == "Away" and away["split_games"] == 1  # B away: g4
    assert (
        home["streak"] == "W1"
        and home["rest_days"] == (sheet["kickoff"] - team_games(RECORDS, "1")[-1].date).days
    )
    assert home["last5"][-1] == {"opponent": "BBB", "at": "@", "score": "35-17", "won": True}
    assert away["poll_rank"] == 14 and home["record"]["total"] == "3-1"
    assert sheet["advantages"]["ppg"] == "home" and sheet["advantages"].get("pace") is None
    assert sheet["league_size"] == 4 and sheet["games_in_log"] == 5
    assert sheet["venue"] == {
        "name": "Big Stadium",
        "city": "Troy",
        "state": "AL",
        "indoor": False,
        "grass": False,
    }
    assert sheet["weather"]["temperature"] == 74 and sheet["predictor"]["home"] == 60.0
    assert sheet["polymarket"] == {"home": 0.55, "away": 0.47} and sheet["book"] is None


def test_sheet_from_diary_uses_stored_rows_and_the_pregame_line():
    from scanner.models import PregameLineRecord

    diary = Diary(":memory:")
    for record in RECORDS:
        diary.store_football_game(record, NOW)
    _, _, games = parse_week_scoreboard(load_fixture("espn_nfl_week4.json"), "nfl")
    slate_game = replace(games[0], completed=False, date=NOW + timedelta(hours=20))
    diary.store_football_upcoming(slate_game, NOW)
    diary.record_pregame_line(
        PregameLineRecord(
            NOW,
            "nfl",
            slate_game.game_id,
            "nfl-x",
            NOW,
            "CLE",
            "PIT",
            {},
            {"home": 0.6, "away": 0.42},
        )
    )
    sheet = sheet_from_diary(diary, "nfl", slate_game.game_id)
    assert sheet is not None and sheet["home"]["abbreviation"] == "CLE"
    assert (
        sheet["polymarket"]["home"] == 0.6 and sheet["polymarket"]["scanned_at"] == NOW.isoformat()
    )
    assert (
        sheet["home"]["games"] == 0 and sheet["league_size"] == 4
    )  # CLE is not in the synthetic log
    assert sheet_from_diary(diary, "nfl", "nope") is None
    assert diary.football_games("nfl", 2026)[0].game_id == "g1"
    assert diary.football_games("nfl", 2025) == [] and diary.football_game_counts() == {"nfl": 5}


# -- refreshing ------------------------------------------------------------------------


class FakeFeed:
    """Serves the week-4 fixture as the current week, past weeks as given, and summaries by id."""

    def __init__(self):
        current = load_fixture("espn_nfl_week4.json")
        self.current = {"nfl": current, "cfb": load_fixture("espn_cfb_week4.json")}
        self.past = {}  # (sport, week) -> raw scoreboard
        self.calls: list[tuple] = []
        self.fail_summaries = False

    def week(self, sport, week=None):
        self.calls.append(("week", sport, week))
        if week is None:
            return self.current[sport]
        return self.past.get(
            (sport, week), {"events": [], "season": {"year": 2026}, "week": {"number": week}}
        )

    def summary(self, sport, game_id):
        self.calls.append(("summary", sport, game_id))
        if self.fail_summaries:
            raise GameLogError("timeout")
        fixture = "espn_nfl_summary_final.json" if sport == "nfl" else "espn_cfb_summary_final.json"
        raw = load_fixture(fixture)
        raw["header"]["competitions"][0]["id"] = game_id  # the same box score under the asked id
        if game_id == "upcoming-1":
            return load_fixture("espn_cfb_summary_upcoming.json")
        return raw


def add_upcoming(feed, sport="nfl", game_id="upcoming-1", hours=20):
    import copy

    raw = feed.current[sport]
    event = copy.deepcopy(raw["events"][0])
    event["id"] = game_id
    event["competitions"][0]["id"] = game_id
    event["competitions"][0]["date"] = (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ")
    event["date"] = event["competitions"][0]["date"]
    event["status"]["type"] = {
        **event["status"]["type"],
        "name": "STATUS_SCHEDULED",
        "state": "pre",
        "completed": False,
    }
    for competitor in event["competitions"][0]["competitors"]:
        competitor["score"] = "0"
        competitor["linescores"] = []
    raw["events"].append(event)


def build():
    settings = load_settings(DATABASE_PATH=":memory:")
    diary = Diary(":memory:")
    feed = FakeFeed()
    return FootballLog(settings, diary, feed=feed, now=lambda: NOW), diary, feed


def test_refresh_stores_each_finished_game_once_within_the_budget():
    log, diary, feed = build()
    summary = log.refresh("nfl", NOW, budget=2)
    assert (summary.season, summary.week) == (2026, 4)
    assert summary.games_stored == 2 and summary.summaries_fetched == 2 and summary.backlog == 1
    assert diary.football_game_counts() == {"nfl": 2}
    assert not diary.football_week_complete("nfl", 2026, 4)
    # The budget ran out on the current week, so earlier weeks wait for the next pass.
    weeks_read = [c for c in feed.calls if c[0] == "week"]
    assert weeks_read == [("week", "nfl", None), ("week", "nfl", 5)]  # this week and next

    feed.calls.clear()
    summary = log.refresh("nfl", NOW + timedelta(minutes=15), budget=40)
    assert summary.games_stored == 1 and summary.backlog == 0
    assert diary.football_week_complete("nfl", 2026, 4) and diary.football_game_counts() == {
        "nfl": 3
    }
    # Past weeks 1-3 came back empty and are marked complete, so they are not read again.
    assert all(diary.football_week_complete("nfl", 2026, w) for w in (1, 2, 3))
    assert [c for c in feed.calls if c[0] == "week"] == [
        ("week", "nfl", None),
        ("week", "nfl", 5),
        ("week", "nfl", 1),
        ("week", "nfl", 2),
        ("week", "nfl", 3),
    ]

    feed.calls.clear()
    summary = log.refresh("nfl", NOW + timedelta(minutes=30))
    assert summary.summaries_fetched == 0 and diary.football_game_counts() == {"nfl": 3}
    assert [c for c in feed.calls if c[0] == "week"] == [("week", "nfl", None), ("week", "nfl", 5)]


def test_past_weeks_are_backfilled_and_failures_do_not_stop_the_pass():
    log, diary, feed = build()
    feed.past[("nfl", 2)] = {
        "events": [
            {**e, "id": f"w2-{i}", "competitions": [{**e["competitions"][0], "id": f"w2-{i}"}]}
            for i, e in enumerate(feed.current["nfl"]["events"][:2])
        ],
        "season": {"year": 2026},
        "week": {"number": 2},
    }
    summary = log.refresh("nfl", NOW, budget=10)
    assert summary.games_stored == 5 and diary.football_week_complete("nfl", 2026, 2)
    log, diary, feed = build()
    feed.fail_summaries = True
    summary = log.refresh("nfl", NOW, budget=3)
    assert summary.games_stored == 0 and len(summary.errors) == 3 and summary.backlog == 3
    assert not diary.football_week_complete("nfl", 2026, 4)


def test_upcoming_games_get_their_extras_once_an_hour():
    log, diary, feed = build()
    add_upcoming(feed)
    summary = log.refresh("nfl", NOW, budget=0)
    assert summary.upcoming == 1 and summary.games_stored == 0 and summary.backlog == 3
    rows = diary.football_upcoming("nfl")
    assert len(rows) == 1 and rows[0]["game_id"] == "upcoming-1"
    assert (
        rows[0]["extra"]["weather"]["temperature"] == 74
        and rows[0]["summary_fetched_at"] == NOW.isoformat()
    )
    fetched = [c for c in feed.calls if c[0] == "summary"]
    assert fetched == [("summary", "nfl", "upcoming-1")]
    log.refresh("nfl", NOW + timedelta(minutes=30), budget=0)
    assert len([c for c in feed.calls if c == ("summary", "nfl", "upcoming-1")]) == 1
    log.refresh("nfl", NOW + timedelta(minutes=61), budget=0)
    assert len([c for c in feed.calls if c == ("summary", "nfl", "upcoming-1")]) == 2
    # Three days later the game is gone from the slate.
    log.refresh("nfl", NOW + timedelta(days=3), budget=0)
    assert diary.football_upcoming("nfl") == []


def test_next_weeks_games_get_sheets_too():
    import copy

    log, diary, feed = build()
    nxt = copy.deepcopy(feed.current["nfl"])
    nxt["events"] = []
    nxt["week"] = {"number": 5}
    feed.past[("nfl", 5)] = nxt
    add_upcoming(feed)  # into the current week's list
    event = copy.deepcopy(feed.current["nfl"]["events"][-1])
    event["id"] = event["competitions"][0]["id"] = "upcoming-2"
    nxt["events"].append(event)
    summary = log.refresh("nfl", NOW, budget=0)
    assert summary.upcoming == 2
    assert {r["game_id"] for r in diary.football_upcoming("nfl")} == {"upcoming-1", "upcoming-2"}


def test_refresh_all_covers_both_leagues_and_a_scoreboard_failure_is_reported():
    log, diary, feed = build()
    summaries = log.refresh_all(NOW)
    assert [s.sport for s in summaries] == ["nfl", "cfb"]
    assert diary.football_game_counts() == {"nfl": 3, "cfb": 3}

    def broken(sport, week=None):
        raise GameLogError("nope")

    feed.week = broken
    summaries = log.refresh_all(NOW)
    assert all(s.errors == [f"{s.sport} scoreboard: nope"] for s in summaries)
