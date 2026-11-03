from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scanner.diary import Diary
from scanner.gamelog import parse_game_summary
from scanner.players import (
    DISPERSION,
    SPORT_STATS,
    chance_over,
    distribution,
    exposure_projection,
    game_factors,
    grade_line,
    league_rates,
    pit,
    project_game,
    rate_for,
    seasons,
    simulate_player,
    summarise_grades,
    summarise_pmf,
    team_averages,
)
from tests.conftest import load_fixture

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def nba_diary(games: int = 2) -> tuple[Diary, object]:
    """The BOS v BKN box score stored ``games`` times, home and away alternating, a day apart."""
    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nba_summary_final.json"), "nba")
    for i in range(games):
        copy = record if i % 2 == 0 else replace(
            record,
            home=record.away,
            away=record.home,
            home_score=record.away_score,
            away_score=record.home_score,
            home_stats=record.away_stats,
            away_stats=record.home_stats,
        )  # fmt: skip
        diary.store_football_game(
            replace(copy, game_id=f"g{i}", date=record.date + timedelta(days=i)), NOW
        )
    return diary, record


def test_seasons_group_a_players_games_and_say_who_played_the_last_one():
    diary, record = nba_diary(2)
    by_id = seasons(diary.player_games("nba"), "nba")
    assert len(by_id) == 27
    tatum = next(s for s in by_id.values() if s.name == "Jayson Tatum")
    assert tatum.team_id == record.home.team_id and tatum.team_abbr == "BOS"
    assert tatum.position == "F" and len(tatum.games) == 2 and tatum.played_last
    assert tatum.last_team_game == "g1" and tatum.total("points") == 80
    assert tatum.exposure_total("minutes") == 84 and tatum.exposure_total(None) == 2
    benched = next(s for s in by_id.values() if not s.games)
    assert not benched.played_last and benched.total("points") == 0
    rates = league_rates(by_id, "nba")
    assert set(rates) >= {"", "F", "G", "C"}
    assert rates[""]["points"] == pytest.approx(0.472, abs=0.01)  # points a minute, league-wide
    assert rates["G"]["assists"] > rates["C"]["assists"]
    assert seasons([], "nba") == {}


def test_playing_time_and_rates_come_from_recent_games_shrunk_toward_the_league():
    diary, _ = nba_diary(2)
    by_id = seasons(diary.player_games("nba"), "nba")
    rates = league_rates(by_id, "nba")
    tatum = next(s for s in by_id.values() if s.name == "Jayson Tatum")
    mu, sd = exposure_projection(tatum, "nba")
    assert mu == 42.0 and sd == 4.0  # the same 42 minutes twice: the floor spread
    rate, league = rate_for(tatum, "points", "nba", rates)
    own = 80 / 84
    assert league == rates["F"]["points"] and league < own < rate + 1e-9 or rate < own
    assert rate == pytest.approx((80 + 72 * league) / (84 + 72))
    # A player with no minutes projects nothing; football is per game.
    benched = next(s for s in by_id.values() if not s.games)
    assert exposure_projection(benched, "nba") == (0.0, 4.0)
    assert exposure_projection(tatum, "nfl") == (1.0, 0.0)
    # Varied minutes widen the spread.
    varied = replace(tatum)
    minutes = (30, 44, 20, 40, 36, 42)
    varied.games = [
        {**g, "stats": {**g["stats"], "minutes": m}}
        for g, m in zip(tatum.games * 3, minutes, strict=False)
    ]
    mu, sd = exposure_projection(varied, "nba")
    assert 30 < mu < 44 and sd > 4.0


def test_game_factors_scale_by_the_teams_projected_scoring_and_the_pace():
    slate = {"home": {"team_id": "h"}, "away": {"team_id": "a"}}
    averages = {"h": 110.0, "a": 100.0, "league": 105.0}
    projection = {"raw": {"home": 121.0, "away": 90.0, "total": 231.0}, "league": {"points": 105.0}}
    factors = game_factors(projection, slate, averages)
    assert factors["h"]["team"] == pytest.approx(1.1) and factors["a"]["team"] == pytest.approx(0.9)
    assert (
        factors["h"]["pace"] == pytest.approx(1.1) and factors["a"]["pace"] == factors["h"]["pace"]
    )
    assert game_factors(None, slate, averages) == {
        "h": {"team": 1.0, "pace": 1.0},
        "a": {"team": 1.0, "pace": 1.0},
    }
    wild = {"raw": {"home": 300.0, "away": 10.0, "total": 10.0}, "league": {"points": 105.0}}
    clamped = game_factors(wild, slate, averages)
    assert (
        clamped["h"]["team"] == 1.4 and clamped["a"]["team"] == 0.7 and clamped["h"]["pace"] == 0.7
    )


def test_the_distribution_and_the_simulation_agree():
    params = {
        "rate": 0.7,
        "exposure_mu": 34.0,
        "exposure_sd": 4.0,
        "exposure_cap": 48.0,
        "factor": 1.05,
        "k": DISPERSION["points"],
    }
    pmf = distribution(params)
    assert abs(sum(pmf.values()) - 1.0) < 1e-9 and min(pmf) == 0
    exact = summarise_pmf(pmf)
    assert exact["mean"] == pytest.approx(0.7 * 34 * 1.05, abs=0.3)
    assert exact["p10"] < exact["median"] < exact["p90"] and exact["sd"] > 5
    sim = simulate_player(params, draws=40_000, seed=3)
    assert sim["draws"] == 40_000 and sim["seed"] == 3
    assert sim["mean"] == pytest.approx(exact["mean"], abs=0.2)
    assert abs(sim["median"] - exact["median"]) <= 1
    assert abs(sim["p10"] - exact["p10"]) <= 1 and abs(sim["p90"] - exact["p90"]) <= 1
    assert sum(sim["hist"].values()) == 40_000
    assert simulate_player(params, draws=1000, seed=3) == simulate_player(params, 1000, 3)
    # No playing time means nothing; a fixed exposure has no mixture.
    assert distribution({**params, "exposure_mu": 0.0, "exposure_sd": 0.0}) == {0: 1.0}
    fixed = distribution({**params, "exposure_sd": 0.0})
    assert summarise_pmf(fixed)["sd"] < exact["sd"]
    # A big mean goes through the normal shortcut in the draws.
    large = simulate_player({**params, "rate": 8.0}, draws=5_000, seed=1)
    assert large["mean"] == pytest.approx(8.0 * 34 * 1.05, rel=0.05)
    assert chance_over(pmf, 24.5)["above"] == pytest.approx(
        sum(p for x, p in pmf.items() if x > 24.5)
    )
    assert chance_over(pmf, 25)["push"] == pytest.approx(pmf[25])
    assert pit(pmf, 0) == pytest.approx(pmf[0] / 2) and 0.99 < pit(pmf, 200) <= 1.0


def test_project_game_writes_the_leaders_lines_for_both_sides():
    diary, record = nba_diary(3)
    rows = diary.player_games("nba")
    averages = team_averages(diary.football_games("nba"), "nba")
    assert averages[record.home.team_id] == 125.0  # BOS scored 125 in each copy
    assert averages["league"] == pytest.approx(124.5)
    slate = {
        "game_id": "up",
        "home": {"team_id": record.home.team_id, "abbreviation": "BOS"},
        "away": {"team_id": record.away.team_id, "abbreviation": "BKN"},
    }
    projection = {
        "raw": {"home": 118.0, "away": 112.0, "total": 230.0},
        "league": {"points": 115.0},
    }
    kickoff = NOW + timedelta(hours=8)
    lines = project_game("nba", slate, kickoff, projection, rows, averages, NOW)
    by_stat = {}
    for line in lines:
        by_stat.setdefault(line["stat"], []).append(line)
    assert {stat: len(v) for stat, v in by_stat.items()} == {
        "points": 16,
        "rebounds": 16,
        "assists": 16,
        "three_made": 12,
    }
    top = max(by_stat["points"], key=lambda r: r["mean"])
    assert top["name"] == "Jayson Tatum" and top["team_abbr"] == "BOS" and top["home"] is True
    assert top["opponent_abbr"] == "BKN" and top["kickoff"] == kickoff and top["model"] == "p1"
    assert 26 < top["mean"] < 34 and top["p10"] < top["median"] < top["p90"]
    assert top["params"]["games"] == 3 and top["params"]["exposure_mu"] == 42.0
    assert top["params"]["factor"] == pytest.approx(118.0 / averages[record.home.team_id])
    rebounds = max(by_stat["rebounds"], key=lambda r: r["mean"])
    assert rebounds["params"]["factor"] == pytest.approx(230.0 / 230.0)  # pace alone
    assert all(float(p) <= 1.0 for r in lines for p in r["pmf"].values())
    assert all(abs(sum(r["pmf"].values()) - 1.0) < 1e-9 for r in lines)
    # Players who sat out the team's last game, or have one game, are left out.
    names = {r["name"] for r in lines}
    benched = next(r["name"] for r in rows if not r["played"])
    assert benched not in names
    assert project_game("nba", slate, kickoff, None, rows[:27], averages, NOW) == []
    assert project_game("mlb", slate, kickoff, None, rows, averages, NOW) == []
    no_projection = project_game("nba", slate, kickoff, None, rows, averages, NOW)
    assert (
        next(r for r in no_projection if r["name"] == "Jayson Tatum" and r["stat"] == "points")[
            "params"
        ]["factor"]
        == 1.0
    )


def test_grading_a_line_and_summarising_the_record():
    params = {
        "rate": 0.7,
        "exposure_mu": 34.0,
        "exposure_sd": 4.0,
        "exposure_cap": 48.0,
        "factor": 1.0,
        "k": 12.0,
    }
    pmf = distribution(params)
    row = {"pmf": {str(x): p for x, p in pmf.items()}, **summarise_pmf(pmf)}
    hit = grade_line(row, row["median"] + 3)
    assert hit["outcome"] == "graded" and hit["over_median"] and not hit["on_median"]
    assert (
        hit["in_band"]
        and 0.5 < hit["pit"] < 0.9
        and hit["error"] == pytest.approx(row["median"] + 3 - row["mean"])
    )
    miss = grade_line(row, row["p90"] + 10)
    assert not miss["in_band"] and miss["pit"] > 0.95
    assert (
        grade_line(row, row["median"])["on_median"]
        and not grade_line(row, row["median"])["over_median"]
    )
    assert grade_line(row, None) == {"outcome": "did_not_play"}
    rows = [
        {"sport": "nba", "stat": "points", **grade_line(row, row["median"] + 3)},
        {"sport": "nba", "stat": "points", **grade_line(row, row["median"] - 2)},
        {"sport": "nba", "stat": "assists", **grade_line(row, row["p90"] + 10)},
        {"sport": "nhl", "stat": "shots", **grade_line(row, None)},
    ]
    s = summarise_grades(rows)
    assert s["projected"] == 4 and s["did_not_play"] == 1 and s["graded"] == 3
    assert s["over_median"] == pytest.approx(2 / 3) and s["in_band"] == pytest.approx(2 / 3)
    assert (
        s["mae"] > 0 and len(s["pit_deciles"]) == 10 and sum(s["pit_deciles"]) == pytest.approx(1.0)
    )
    assert list(s["by_sport"]) == ["nba"] and s["by_sport"]["nba"]["graded"] == 3
    assert s["by_sport"]["nba"]["by_stat"]["points"]["graded"] == 2
    assert s["by_sport"]["nba"]["by_stat"]["assists"]["in_band"] == 0.0
    assert summarise_grades([]) == {"projected": 0, "did_not_play": 0, "graded": 0, "by_sport": {}}
    assert set(SPORT_STATS) == {"nba", "cbb", "nhl", "nfl", "cfb"}


def test_hockey_and_football_lines_use_their_own_exposure():
    diary = Diary(":memory:")
    nhl = parse_game_summary(load_fixture("espn_nhl_summary_final.json"), "nhl")
    diary.store_football_game(nhl, NOW)
    diary.store_football_game(replace(nhl, game_id="n2", date=nhl.date + timedelta(days=2)), NOW)
    rows = diary.player_games("nhl")
    slate = {
        "game_id": "up",
        "home": {"team_id": nhl.home.team_id, "abbreviation": "NSH"},
        "away": {"team_id": nhl.away.team_id, "abbreviation": "NYI"},
    }
    lines = project_game(
        "nhl", slate, NOW, None, rows, team_averages(diary.football_games("nhl"), "nhl"), NOW
    )
    stats = {}
    for line in lines:
        stats.setdefault(line["stat"], []).append(line)
    assert {s: len(v) for s, v in stats.items()} == {
        "shots": 20,
        "goals": 20,
        "assists": 20,
        "saves": 2,
    }
    goalie = stats["saves"][0]
    assert goalie["position"] == "G" and goalie["params"]["exposure_mu"] > 3000
    assert 15 < goalie["mean"] < 40 and all(r["position"] != "G" for r in stats["shots"])
    nfl = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    diary.store_football_game(nfl, NOW)
    diary.store_football_game(replace(nfl, game_id="f2", date=nfl.date + timedelta(days=7)), NOW)
    rows = diary.player_games("nfl")
    slate = {
        "game_id": "up",
        "home": {"team_id": nfl.home.team_id, "abbreviation": "CLE"},
        "away": {"team_id": nfl.away.team_id, "abbreviation": "PIT"},
    }
    lines = project_game(
        "nfl", slate, NOW, None, rows, team_averages(diary.football_games("nfl"), "nfl"), NOW
    )
    stats = {}
    for line in lines:
        stats.setdefault(line["stat"], []).append(line)
    assert {s: len(v) for s, v in stats.items()} == {
        "pass_yards": 2,
        "rush_yards": 3,  # one side's second ball-carrier never gained a yard
        "rec_yards": 8,
        "receptions": 8,
        "pass_td": 2,
    }
    rodgers = next(r for r in stats["pass_yards"] if r["name"] == "Aaron Rodgers")
    assert rodgers["params"]["exposure_mu"] == 1.0 and rodgers["params"]["exposure_sd"] == 0.0
    assert 220 < rodgers["mean"] < 320 and rodgers["p10"] < 299 < rodgers["p90"]
    assert all(r["mean"] > 0 for r in stats["rec_yards"])
