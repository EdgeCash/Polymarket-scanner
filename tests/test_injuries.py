from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scanner.injuries import (
    RULES,
    SideContext,
    absentees,
    availability,
    describe,
    margin_adjustment,
    normalise_status,
    parse_depth_chart,
    parse_injury_report,
    parse_roster,
    status_weight,
)
from scanner.projection import fit, grade, project, summarise
from tests.conftest import load_fixture
from tests.test_projection import season

# -- parsing --------------------------------------------------------------------------


def test_statuses_are_normalised_and_weighted():
    assert normalise_status("Injured Reserve") == "injured_reserve"
    assert normalise_status("Day-To-Day") == "day_to_day"
    assert normalise_status("IR-R") == "injured_reserve"
    assert normalise_status("Questionable") == "questionable"
    assert normalise_status("") == "active" and normalise_status(None) == "active"
    assert normalise_status("Something New") == "something_new"
    assert status_weight("out") == 1.0 and status_weight("questionable") == 0.4
    assert status_weight("active") == 0.0 and status_weight("something_new") == 0.5


def test_the_game_summarys_injury_report_is_read_for_both_sides():
    raw = load_fixture("espn_nfl_summary_upcoming.json")
    report = parse_injury_report(raw, "2", "15")  # BUF home, MIA away
    assert {len(report["home"]), len(report["away"])} == {4}
    hamlin = report["home"][0]
    assert hamlin["player_id"] == "4036060" and hamlin["name"] == "Damar Hamlin"
    assert hamlin["position"] == "S" and hamlin["status"] == "questionable"
    assert hamlin["detail"] == "Toe" and hamlin["return_date"] == "2026-11-22"
    assert {e["status"] for e in report["away"]} == {"questionable", "out", "doubtful"}
    nhl = parse_injury_report(load_fixture("espn_nhl_summary_upcoming.json"), "7", "6")
    assert nhl["home"] == []  # CAR lists nobody
    assert [e["status"] for e in nhl["away"]] == ["day_to_day"] + ["injured_reserve"] * 4
    assert nhl["away"][3]["position"] == "G"
    nba = parse_injury_report(load_fixture("espn_nba_summary_upcoming_injuries.json"), "4", "27")
    assert all(e["status"] == "out" for side in nba.values() for e in side)
    # A summary without the block means the sport (or game) publishes none.
    assert parse_injury_report(load_fixture("espn_cfb_summary_upcoming.json"), "1", "2") is None
    assert parse_injury_report({}, "1", "2") is None
    # A side the summary lists under another id is ignored, not misfiled.
    assert parse_injury_report(raw, "2", "99") == {"home": report["home"], "away": []}


def test_rosters_are_read_grouped_or_flat_with_injured_reserve_and_the_practice_squad():
    nfl = {p["name"]: p for p in parse_roster(load_fixture("espn_nfl_roster.json"))}
    assert nfl["Sam Ehlinger"] == {
        "player_id": nfl["Sam Ehlinger"]["player_id"],
        "name": "Sam Ehlinger",
        "position": "QB",
        "status": "active",
        "group": "offense",
        "jersey": nfl["Sam Ehlinger"]["jersey"],
        "experience": nfl["Sam Ehlinger"]["experience"],
    }
    # The injured reserve group: ESPN's own status says day-to-day, the listing says IR.
    assert nfl["Tyler Badie"]["status"] == "injured_reserve"
    assert nfl["Tyler Badie"]["group"] == "injuredReserveOrOut"
    assert nfl["Michael Bandy"]["status"] == "practice_squad"
    nba = {p["name"]: p for p in parse_roster(load_fixture("espn_nba_roster.json"))}
    assert nba["Julian Strawther"]["status"] == "out"  # active on paper, out on the list
    assert nba["Aaron Gordon"]["status"] == "active" and nba["Aaron Gordon"]["group"] is None
    nhl = {p["name"]: p for p in parse_roster(load_fixture("espn_nhl_roster.json"))}
    assert nhl["James Hagens"]["status"] == "injured_reserve"
    assert nhl["Jeremy Swayman"]["position"] == "G" and nhl["Jeremy Swayman"]["group"] == "Goalies"
    assert parse_roster({}) == [] and parse_roster({"athletes": "no"}) == []


def test_the_depth_chart_gives_each_man_his_best_rank():
    depth = parse_depth_chart(load_fixture("espn_nfl_depthchart.json"))
    by_name = {v["name"]: v for v in depth.values()}
    assert by_name["Bo Nix"] == {"rank": 1, "slot": "QB", "name": "Bo Nix"}
    assert by_name["Jarrett Stidham"]["rank"] == 2 and by_name["Sam Ehlinger"]["rank"] == 3
    assert by_name["Zach Allen"]["rank"] == 1 and by_name["Zach Allen"]["slot"] == "LDE"
    assert by_name["Courtland Sutton"]["rank"] == 1 and by_name["Courtland Sutton"]["slot"] == "WR2"
    assert parse_depth_chart({"depthchart": "no"}) == {}


# -- worth ----------------------------------------------------------------------------

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def lines(team_id: str, players: dict[str, list[dict]], games: int = 10) -> list[dict]:
    """Player rows for one team over ``games`` games; a player's list gives his stats per
    game, None for a game he sat out."""
    rows = []
    for i in range(games):
        for player_id, per_game in players.items():
            stats = per_game[i] if i < len(per_game) else None
            rows.append(
                {
                    "game_id": f"{team_id}-g{i}",
                    "date": (T0 + timedelta(days=i)).isoformat(),
                    "team_id": team_id,
                    "team_abbr": team_id,
                    "player_id": player_id,
                    "name": player_id.replace("_", " ").title(),
                    "position": None,
                    "played": stats is not None,
                    "stats": stats or {},
                }
            )
    return rows


def nba_stats(points, assists=4, rebounds=6, minutes=34):
    return {"points": points, "assists": assists, "rebounds": rebounds, "minutes": minutes}


def test_a_basketball_absentee_costs_his_production_above_a_replacement_scaled_by_status():
    rule = RULES["nba"]
    rows = lines(
        "BOS",
        {
            "jayson_tatum": [nba_stats(30, 5, 8)] * 10,  # 30 + 3.5 + 4 = 37.5 a game
            "role_player": [nba_stats(6, 1, 2, 14)] * 10,  # 6 + 0.7 + 1 = 7.7: below replacement
            "part_timer": [nba_stats(20, 4, 6)] * 2 + [None] * 8,  # played 2 of the last 10
        },
    )
    context = SideContext.build("BOS", rows, [], {})
    report = [
        {"player_id": "jayson_tatum", "name": "Jayson Tatum", "position": "F", "status": "out"},
        {"player_id": "role_player", "name": "Role Player", "position": "G", "status": "out"},
        {"player_id": "part_timer", "name": "Part Timer", "position": "C", "status": "day_to_day"},
    ]
    out = {e["player_id"]: e for e in absentees("nba", report, context)}
    tatum = out["jayson_tatum"]
    assert tatum["units"] == {"production": pytest.approx(37.5 - rule.replacement)}
    assert tatum["weight"] == 1.0 and tatum["played_share"] == 1.0
    assert tatum["cost"] == pytest.approx(
        rule.coefficients["production"] * (37.5 - rule.replacement), abs=0.01
    )
    assert out["role_player"]["cost"] == 0.0  # a replacement replaces him
    part = out["part_timer"]
    assert part["played_share"] == 0.2 and part["weight"] == 0.45
    coefficient = rule.coefficients["production"]
    assert part["cost"] == pytest.approx(0.45 * 0.2 * coefficient * (20 + 2.8 + 3 - 8), abs=0.01)
    # The list is sorted by cost, the costliest first.
    assert [e["player_id"] for e in absentees("nba", report, context)][0] == "jayson_tatum"
    # A man on the roster's injured list but not in the report is counted too, and a
    # man the log has never seen costs nothing: the ratings never had him.
    roster = [
        {"player_id": "new_signing", "name": "New Signing", "position": "G", "status": "out"},
        {"player_id": "jayson_tatum", "name": "Jayson Tatum", "position": "F", "status": "active"},
    ]
    context = SideContext.build("BOS", rows, roster, {})
    with_roster = {e["player_id"]: e for e in absentees("nba", report, context)}
    assert with_roster["new_signing"]["cost"] == 0.0 and with_roster["new_signing"]["weight"] == 1
    assert with_roster["jayson_tatum"]["status"] == "out"  # the report outranks the roster
    # The roster's own list stands without a report; with neither there is nobody.
    assert [e["player_id"] for e in absentees("nba", [], context)] == ["new_signing"]
    assert absentees("nba", [], SideContext.build("BOS", rows, [], {})) == []
    assert absentees("mlb", report, context) == []


def test_hockey_counts_skaters_points_and_leaves_the_goalie_to_the_goalie_model():
    rows = lines(
        "BOS",
        {
            "star": [{"goals": 0.6, "assists": 0.8, "toi": 20 * 60}] * 10,
            "grinder": [{"goals": 0.1, "assists": 0.1, "toi": 11 * 60}] * 10,
            "starter_g": [{"saves": 28, "toi": 60 * 60}] * 8 + [None] * 2,
        },
    )
    for row in rows:
        if row["player_id"].endswith("_g"):
            row["position"] = "G"
    context = SideContext.build("BOS", rows, [], {})
    report = [
        {"player_id": "star", "name": "Star", "position": "C", "status": "out"},
        {"player_id": "grinder", "name": "Grinder", "position": "LW", "status": "out"},
        {"player_id": "starter_g", "name": "Starter", "position": "G", "status": "injured_reserve"},
    ]
    out = {e["player_id"]: e for e in absentees("nhl", report, context)}
    rule = RULES["nhl"]
    production = 0.6 + 0.8 + 0.03 * 20
    assert out["star"]["cost"] == pytest.approx(
        rule.coefficients["production"] * (production - rule.replacement), abs=0.01
    )
    assert out["grinder"]["cost"] == 0.0
    # The goalie is on the list (so the goalie model can rule him out) at no cost here.
    assert out["starter_g"]["units"] == {} and out["starter_g"]["cost"] == 0.0
    assert out["starter_g"]["weight"] == 1.0 and availability(list(out.values()))["starter_g"] == 1


def test_football_counts_the_quarterback_skill_yards_and_other_starters():
    rows = lines(
        "DEN",
        {
            "bo_nix": [{"pass_att": 33, "pass_yards": 240}] * 10,
            "jarrett_stidham": [{"pass_att": 2, "pass_yards": 10}] * 10,
            "courtland_sutton": [{"rec_yards": 80, "receptions": 6}] * 10,
            "zach_allen": [{"tackles": 4}] * 10,
        },
    )
    for row in rows:
        row["position"] = {
            "bo_nix": "QB",
            "jarrett_stidham": "QB",
            "courtland_sutton": "WR",
            "zach_allen": "DE",
        }[row["player_id"]]
    depth = {
        "bo_nix": {"rank": 1, "slot": "QB", "name": "Bo Nix"},
        "jarrett_stidham": {"rank": 2, "slot": "QB", "name": "Jarrett Stidham"},
        "courtland_sutton": {"rank": 1, "slot": "WR2", "name": "Courtland Sutton"},
        "zach_allen": {"rank": 1, "slot": "LDE", "name": "Zach Allen"},
        "garett_bolles": {"rank": 1, "slot": "LT", "name": "Garett Bolles"},
    }
    context = SideContext.build("DEN", rows, [], depth)
    assert context.quarterback_one() == "bo_nix"
    report = [
        {"player_id": "bo_nix", "name": "Bo Nix", "position": "QB", "status": "out"},
        {"player_id": "jarrett_stidham", "name": "Stidham", "position": "QB", "status": "out"},
        {
            "player_id": "courtland_sutton",
            "name": "Sutton",
            "position": "WR",
            "status": "questionable",
        },
        {"player_id": "zach_allen", "name": "Zach Allen", "position": "DE", "status": "doubtful"},
        {"player_id": "garett_bolles", "name": "Bolles", "position": "OT", "status": "out"},
    ]
    out = {e["player_id"]: e for e in absentees("nfl", report, context)}
    rule = RULES["nfl"]
    assert out["bo_nix"]["units"] == {"quarterback": 1.0}
    assert out["bo_nix"]["cost"] == pytest.approx(rule.coefficients["quarterback"])
    assert out["jarrett_stidham"]["units"] == {} and out["jarrett_stidham"]["cost"] == 0.0
    assert out["courtland_sutton"]["units"] == {"skill_yards": 80.0}
    assert out["courtland_sutton"]["cost"] == pytest.approx(
        0.4 * rule.coefficients["skill_yards"] * 80, abs=0.01
    )
    assert out["zach_allen"]["cost"] == pytest.approx(0.9 * rule.coefficients["starters"], abs=0.01)
    # A starter the box score never lists (an offensive lineman) still counts as a head.
    assert out["garett_bolles"]["played_share"] == 1.0
    assert out["garett_bolles"]["cost"] == pytest.approx(rule.coefficients["starters"])
    # A starter who records stats but has none this season has not played: nothing lost.
    depth["new_qb"] = {"rank": 1, "slot": "QB", "name": "New Qb"}
    depth["bo_nix"]["rank"] = 2
    hurt_all_year = absentees(
        "nfl",
        [{"player_id": "new_qb", "name": "New Qb", "position": "QB", "status": "injured_reserve"}],
        SideContext.build("DEN", rows, [], depth),
    )
    assert hurt_all_year[0]["units"] == {"quarterback": 1.0} and hurt_all_year[0]["cost"] == 0.0
    # Without a depth chart the quarterback is whoever threw most in the last game.
    assert SideContext.build("DEN", rows, [], {}).quarterback_one() == "bo_nix"
    assert SideContext.build("DEN", [], [], {}).quarterback_one() is None


def test_the_margin_moves_by_the_two_sides_losses_within_the_cap():
    home = [{"player_id": "a", "name": "A B", "position": "F", "status": "out", "weight": 1.0,
             "cost": 4.0}]  # fmt: skip
    away = [
        {"player_id": "c", "name": "C D", "position": "G", "status": "questionable",
         "weight": 0.4, "cost": 1.5},
        {"player_id": "e", "name": "E F", "position": "C", "status": "out", "weight": 1.0,
         "cost": 0.0},
    ]  # fmt: skip
    adjustment = margin_adjustment("nba", home, away)
    assert adjustment["home_loss"] == 4.0 and adjustment["away_loss"] == 1.5
    assert adjustment["margin"] == -2.5
    assert adjustment["home"][0] == {
        "name": "A B", "position": "F", "status": "out", "detail": None, "weight": 1.0, "cost": 4.0
    }  # fmt: skip
    assert margin_adjustment("nba", [], []) is None and margin_adjustment("nba", None, None) is None
    assert margin_adjustment("mlb", home, away) is None
    heavy = [{**home[0], "cost": 9.0}, {**home[0], "name": "G H", "cost": 9.0}]
    assert margin_adjustment("nba", heavy, [])["home_loss"] == RULES["nba"].cap
    assert describe(adjustment, "BOS", "NYK", "points") == (
        "BOS without B (out); NYK without D (questionable): margin -2.5 points"
    )
    assert describe(margin_adjustment("nba", [], away[1:]), "BOS", "NYK", "points") == ""
    assert describe(None, "BOS", "NYK", "points") == ""
    assert availability(away) == {"c": 0.4, "e": 1.0}


def test_the_projection_moves_its_margin_for_the_absentees_and_grades_with_and_without():
    ratings = fit(season(), "nba")
    plain = project(ratings, "A", "D", False, None)
    home_out = [{"player_id": "x", "name": "X Y", "position": "F", "status": "out", "weight": 1.0,
                 "cost": 3.0}]  # fmt: skip
    hurt = project(ratings, "A", "D", False, None, {"injuries": {"home": home_out, "away": []}})
    assert hurt["raw"]["injuries"]["margin"] == -3.0
    assert hurt["raw"]["margin"] == pytest.approx(plain["raw"]["margin"] - 3.0)
    assert hurt["raw"]["total"] == pytest.approx(plain["raw"]["total"] - 3.0)
    assert hurt["raw"]["home_win"] < plain["raw"]["home_win"]
    assert "injuries" not in plain["raw"]
    assert project(ratings, "A", "D", False, None, {"injuries": None}) == plain
    # Grading keeps the miss with the move and the miss the model would have had without it.
    actual_margin = round(plain["raw"]["margin"])
    graded = grade(hurt, 100 + actual_margin, 100, None)
    assert graded["injuries"]["margin"] == -3.0
    assert graded["injuries"]["error_with"] == pytest.approx(
        abs(hurt["raw"]["margin"] - actual_margin)
    )
    assert graded["injuries"]["error_without"] == pytest.approx(
        abs(plain["raw"]["margin"] - actual_margin)
    )
    assert grade(plain, 100, 90, None)["injuries"] is None
    summary = summarise([graded, grade(plain, 100, 90, None)], "nba")
    assert summary["injuries"]["games"] == 1
    assert summary["injuries"]["with"] == pytest.approx(graded["injuries"]["error_with"])
    assert summary["injuries"]["without"] == pytest.approx(graded["injuries"]["error_without"])
    assert summarise([], "nba")["injuries"] == {"games": 0, "with": None, "without": None}
