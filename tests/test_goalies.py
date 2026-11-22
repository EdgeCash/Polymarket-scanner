from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scanner.goalies import (
    CARRY,
    DEFAULT_SHOTS,
    LEAGUE_SAVE_RATE,
    PRIOR_SHOTS,
    REPEAT_ON_BACK_TO_BACK,
    describe,
    expected_edge,
    league_save_rate,
    margin_adjustment,
    quality,
    start_chances,
    team_goalies,
)
from scanner.projection import fit, grade, project, summarise
from scanner.scratches import (
    as_report,
    match_game,
    normalise_name,
    parse_nhl_schedule,
    parse_scratches,
)
from tests.conftest import load_fixture
from tests.test_projection import season

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def goalie_rows(team: str, starters: str, shots: int = 30) -> list[dict]:
    """One row a goalie a game: ``starters`` names each game's starter ('A' or 'B'); the
    other dresses and does not play. A stops 28 of 30, B 26 of 30."""
    rows = []
    for i, starter in enumerate(starters):
        for goalie, saves in (("A", shots - 2), ("B", shots - 4)):
            played = goalie == starter
            rows.append(
                {
                    "game_id": f"{team}-g{i:02d}",
                    "date": (T0 + timedelta(days=2 * i)).isoformat(),
                    "team_id": team,
                    "player_id": f"{team}-{goalie}",
                    "name": f"Goalie {goalie}",
                    "position": "G",
                    "played": played,
                    "stats": (
                        {"saves": saves, "shots_against": shots, "toi": 3600} if played else {}
                    ),
                }
            )
    return rows


def test_a_teams_goalies_come_from_the_log_with_a_shrunk_save_rate():
    rows = goalie_rows("BOS", "AAABAAABAA")
    team = team_goalies(rows, "BOS")
    assert team.starts == ["BOS-A"] * 3 + ["BOS-B"] + ["BOS-A"] * 3 + ["BOS-B"] + ["BOS-A"] * 2
    assert team.games == 10 and team.played[0] == {"BOS-A"} and team.played[3] == {"BOS-B"}
    assert team.shots_per_game == 30.0 and team.names["BOS-A"] == "Goalie A"
    assert team.saves["BOS-A"] == 8 * 28 and team.shots["BOS-B"] == 60
    league = league_save_rate(rows)  # too few shots yet: the default
    assert league == LEAGUE_SAVE_RATE
    assert league_save_rate(goalie_rows("BOS", "AB" * 40)) == pytest.approx(54 / 60)
    a, b = quality(team, "BOS-A", league), quality(team, "BOS-B", league)
    assert a == pytest.approx((224 + PRIOR_SHOTS * league) / (240 + PRIOR_SHOTS))
    # His earlier seasons count at the carry: a .930 goalie over 2,000 shots before this one.
    veteran = team_goalies(rows, "BOS", {"BOS-B": (1860.0, 2000.0)})
    assert quality(veteran, "BOS-B", league) == pytest.approx(
        (52 + CARRY * 1860 + PRIOR_SHOTS * league) / (60 + CARRY * 2000 + PRIOR_SHOTS)
    )
    assert quality(veteran, "BOS-B", league) > quality(veteran, "BOS-A", league)
    assert 26 / 30 < b < league < a < 28 / 30  # both pulled most of the way to the league
    assert team_goalies(rows, "NYR").games == 0
    assert team_goalies([], "BOS").shots_per_game == DEFAULT_SHOTS


def test_who_starts_follows_recent_starts_the_back_to_back_rule_and_who_is_ruled_out():
    team = team_goalies(goalie_rows("BOS", "AAABAAABAA"), "BOS")
    rested = start_chances(team, back_to_back=False)
    assert rested["BOS-A"] == pytest.approx(8.5 / 11) and sum(rested.values()) == pytest.approx(1)
    # Last night's starter (A) starts again about one time in ten.
    tired = start_chances(team, back_to_back=True)
    assert tired == {
        "BOS-A": REPEAT_ON_BACK_TO_BACK,
        "BOS-B": pytest.approx(1 - REPEAT_ON_BACK_TO_BACK),
    }
    # With the other man ruled out there is nobody else, back-to-back or not.
    assert start_chances(team, True, {"BOS-B"}) == {"BOS-A": 1.0}
    assert start_chances(team, False, {"BOS-A"}) == {"BOS-B": 1.0}
    assert start_chances(team, False, {"BOS-A", "BOS-B"}) == {}
    assert start_chances(team_goalies([], "BOS"), False) == {}


def test_the_edge_is_tonights_expected_goalie_against_the_usual_in_goals():
    team = team_goalies(goalie_rows("BOS", "AAABAAABAA"), "BOS")
    league = LEAGUE_SAVE_RATE
    a, b = quality(team, "BOS-A", league), quality(team, "BOS-B", league)
    usual = 0.8 * a + 0.2 * b
    tired = expected_edge(team, league, back_to_back=True)
    tonight = REPEAT_ON_BACK_TO_BACK * a + (1 - REPEAT_ON_BACK_TO_BACK) * b
    assert tired["edge"] == pytest.approx((tonight - usual) * 30, abs=0.001) and tired["edge"] < 0
    assert tired["goalies"][0]["player_id"] == "BOS-B"
    assert tired["goalies"][0]["chance"] == pytest.approx(1 - REPEAT_ON_BACK_TO_BACK, abs=0.005)
    assert tired["usual_save_rate"] == pytest.approx(usual, abs=1e-4) and tired["back_to_back"]
    # Rested, the chances are the recent shares, so the edge is next to nothing.
    assert abs(expected_edge(team, league, False)["edge"]) < 0.02
    # The usual starter ruled out: the whole gap to the backup, in goals.
    hurt = expected_edge(team, league, False, {"BOS-A"})
    assert hurt["edge"] == pytest.approx((b - usual) * 30, abs=0.001)
    assert expected_edge(team_goalies(goalie_rows("BOS", "AAB"), "BOS"), league, True) is None
    assert expected_edge(team, league, False, {"BOS-A", "BOS-B"}) is None


def test_the_margin_moves_by_the_difference_and_the_projection_takes_goals_off_the_other_side():
    home = {"edge": -0.12, "goalies": [{"name": "Joonas Korpisalo", "chance": 0.9,
            "save_rate": 0.8972}], "usual_save_rate": 0.9081}  # fmt: skip
    away = {"edge": 0.03, "goalies": [{"name": "Igor Shesterkin", "chance": 0.8,
            "save_rate": 0.912}], "usual_save_rate": 0.911}  # fmt: skip
    adjustment = margin_adjustment("nhl", home, away)
    assert adjustment["margin"] == -0.15 and adjustment["home_edge"] == -0.12
    assert margin_adjustment("nhl", None, None) is None
    assert margin_adjustment("nba", home, away) is None
    assert margin_adjustment("nhl", home, None)["margin"] == -0.12
    assert describe(adjustment, "BOS", "NYR") == (
        "BOS likely in goal: Korpisalo 90% (.897 against .908 usual); "
        "NYR likely in goal: Shesterkin 80% (.912 against .911 usual): margin -0.15 goals"
    )
    assert describe(margin_adjustment("nhl", {**home, "edge": 0.0}, None), "BOS", "NYR") == ""
    assert describe(None, "BOS", "NYR") == ""
    ratings = fit(season(), "nhl")
    plain = project(ratings, "A", "D", False, None)
    moved = project(ratings, "A", "D", False, None, {"goalies": {"home": home, "away": away}})
    # The home side's worse goalie lets the visitors score more; the visitors' better one
    # takes a little off the home side.
    assert moved["raw"]["away"] == pytest.approx(plain["raw"]["away"] + 0.12)
    assert moved["raw"]["home"] == pytest.approx(plain["raw"]["home"] - 0.03)
    assert moved["raw"]["margin"] == pytest.approx(plain["raw"]["margin"] - 0.15)
    assert moved["raw"]["goalies"]["margin"] == -0.15 and "goalies" not in plain["raw"]
    graded = grade(moved, 2, 3, None)
    assert graded["goalies"]["margin"] == -0.15
    assert graded["goalies"]["error_without"] == pytest.approx(abs(plain["raw"]["margin"] + 1))
    assert grade(plain, 2, 3, None)["goalies"] is None
    assert summarise([graded], "nhl")["goalies"]["games"] == 1


def test_the_leagues_scratch_list_is_matched_to_the_game_and_the_men():
    schedule = parse_nhl_schedule(load_fixture("nhl_api_schedule.json"))
    assert len(schedule) == 14 and schedule[0] == {
        "game_id": "2026020359",
        "start": datetime(2026, 11, 21, 18, 0, tzinfo=UTC),
        "home": "CAR",
        "away": "EDM",
    }
    kickoff = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    assert match_game(schedule, "CAR", "EDM", kickoff) == "2026020359"
    assert match_game(schedule, "CAR", "EDM", kickoff + timedelta(hours=3)) is None
    assert match_game(schedule, "EDM", "CAR", kickoff) is None
    # ESPN's abbreviations differ for five clubs: SJ is the league's SJS, and so on.
    late = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
    assert match_game(schedule, "SJ", "SEA", late) == "2026020372"
    assert parse_nhl_schedule({}) == [] and parse_nhl_schedule(None) == []
    posted = parse_scratches(load_fixture("nhl_api_right_rail.json"))
    assert posted["away"] == ["Ryan Nugent-Hopkins", "Jake Walman", "Max Jones"]
    assert "Nicolas Deslauriers" in posted["home"] and "Juuso Välimäki" in posted["home"]
    # Forty minutes out the sheet is empty, which is not the same as nobody scratched.
    assert parse_scratches(load_fixture("nhl_api_right_rail_early.json")) is None
    assert parse_scratches({}) is None
    assert normalise_name("Juuso Välimäki") == "juuso valimaki"
    assert normalise_name("Ryan Nugent-Hopkins") == "ryan nugent hopkins"
    known = {"11": "Ryan Nugent-Hopkins", "12": "Juuso Valimaki", "13": "Somebody Else"}
    report = as_report(["Ryan Nugent-Hopkins", "Juuso Välimäki", "Not Known"], known, {"11": "C"})
    assert [(e["player_id"], e["status"], e["detail"]) for e in report] == [
        ("11", "out", "scratch"),
        ("12", "out", "scratch"),
    ]
    assert report[0]["position"] == "C" and report[1]["position"] is None


def test_the_carry_over_file_is_used_only_for_the_season_after_it(monkeypatch, tmp_path):
    import json

    import scanner.goalies as goalies

    path = tmp_path / "goalie_priors.json"
    path.write_text(json.dumps({"season": 2026, "goalies": {"7": {"saves": 900, "shots": 1000}}}))
    monkeypatch.setattr(goalies, "PRIORS_PATH", path)
    goalies._load_priors.cache_clear()
    assert goalies.career_for(2027) == {"7": (900.0, 1000.0)}
    assert goalies.career_for(2028) == {} and goalies.career_for(None) == {}
    monkeypatch.setattr(goalies, "PRIORS_PATH", tmp_path / "missing.json")
    goalies._load_priors.cache_clear()
    assert goalies.career_for(2027) == {}
    goalies._load_priors.cache_clear()


def test_the_starting_goalies_page_is_read_for_who_is_reported_and_how_firmly():
    from scanner.starters import (
        STATUS_CHANCE,
        match_starters,
        named_starter,
        parse_starting_goalies,
    )

    with open("tests/fixtures/starting_goalies.html", encoding="utf-8") as fh:
        listing = parse_starting_goalies(fh.read())
    assert [(g["away"], g["home"]) for g in listing] == [
        ("CAR", "NYR"), ("BUF", "TBL"), ("PIT", "DAL"), ("EDM", "STL"), ("CHI", "VAN"),
    ]  # fmt: skip
    first = listing[0]
    assert first["start"] == datetime(2026, 11, 22, 17, 30, tzinfo=UTC)
    assert first["home_goalie"]["name"] == "Dylan Garand"
    assert first["home_goalie"]["status"] == "confirmed"
    assert first["home_goalie"]["source"] == "Mollie Walker"
    assert "warmups" in first["home_goalie"]["detail"]
    # No report yet is the site's own projection, which is left alone; "Likely" is kept.
    assert listing[1]["home_goalie"] is None and listing[1]["away_goalie"] is None
    assert listing[2]["home_goalie"]["status"] == "confirmed" and listing[2]["away_goalie"] is None
    assert listing[4]["away_goalie"]["status"] == "likely"
    assert parse_starting_goalies("<html></html>") == [] and parse_starting_goalies(None) == []
    assert parse_starting_goalies('<script id="__NEXT_DATA__">not json</script>') == []
    # ESPN calls Tampa Bay TB; the page, like the league, calls it TBL.
    kickoff = datetime(2026, 11, 22, 22, 0, tzinfo=UTC)
    assert match_starters(listing, "TB", "BUF", kickoff) == {"home": None, "away": None}
    blues = match_starters(listing, "STL", "EDM", datetime(2026, 11, 23, 0, 0, tzinfo=UTC))
    assert blues["home"]["name"] == "Joel Hofer" and blues["away"] is None
    assert match_starters(listing, "STL", "EDM", kickoff - timedelta(hours=6)) is None
    assert match_starters(listing, "BOS", "EDM", kickoff) is None
    known = {"77": "Joel Hofer", "78": "Jordan Binnington"}
    named = named_starter(blues["home"], known)
    assert named == {
        "player_id": "77", "name": "Joel Hofer", "status": "confirmed",
        "chance": STATUS_CHANCE["confirmed"],
    }  # fmt: skip
    stranger = named_starter(listing[4]["home_goalie"], known)
    assert stranger["player_id"] == "name:nikita tolopilo" and stranger["chance"] == 0.85
    assert named_starter(None, known) is None


def test_a_reported_starter_takes_the_reports_chance_and_the_rest_share_what_is_left():
    team = team_goalies(goalie_rows("BOS", "AAABAAABAA"), "BOS")
    league = LEAGUE_SAVE_RATE
    confirmed = {"player_id": "BOS-B", "name": "Goalie B", "status": "confirmed", "chance": 0.97}
    chances = start_chances(team, False, None, confirmed)
    assert chances == {"BOS-B": pytest.approx(0.97), "BOS-A": pytest.approx(0.03)}
    # The back-to-back rule gives way to the report too.
    assert start_chances(team, True, None, {**confirmed, "player_id": "BOS-A"})["BOS-A"] == (
        pytest.approx(0.97)
    )
    edge = expected_edge(team, league, False, None, confirmed)
    a, b = quality(team, "BOS-A", league), quality(team, "BOS-B", league)
    usual = 0.8 * a + 0.2 * b
    assert edge["reported"] == "confirmed" and edge["goalies"][0]["name"] == "Goalie B"
    assert edge["edge"] == pytest.approx((0.97 * b + 0.03 * a - usual) * 30, abs=0.001)
    # A man the log has never seen is rated at the league's rate, under his reported name.
    new = {"player_id": "name:new man", "name": "New Man", "status": "likely", "chance": 0.85}
    unseen = expected_edge(team, league, False, None, new)
    assert unseen["goalies"][0] == {
        "player_id": "name:new man", "name": "New Man", "chance": 0.85,
        "save_rate": pytest.approx(league),
    }  # fmt: skip
    assert unseen["reported"] == "likely"
    # A named man the injury report rules out is not believed over it.
    ruled_out = expected_edge(team, league, False, {"BOS-B"}, confirmed)
    assert ruled_out["reported"] is None and ruled_out["goalies"][0]["player_id"] == "BOS-A"
    # With nobody else on record the named man takes it all.
    alone = start_chances(team, False, {"BOS-A"}, confirmed)
    assert alone == {"BOS-B": 1.0}
    assert expected_edge(team, league, False)["reported"] is None
    adjustment = margin_adjustment("nhl", edge, None)
    assert describe(adjustment, "BOS", "NYR").startswith("BOS confirmed in goal: B 97% (")
