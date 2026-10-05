from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from scanner.matching import match_games, normalize, team_matches
from scanner.models import League, MarketTeam, Team
from scanner.polymarket import parse_event as pm_parse
from scanner.scores import parse_scoreboard
from tests.conftest import at, load_fixture


def cfb_saturday():
    games = [pm_parse(e, League.CFB) for e in load_fixture("pm_cfb_saturday.json")["events"]]
    states = parse_scoreboard(load_fixture("espn_cfb_scoreboard.json"), League.CFB, at(0))
    return [g for g in games if g is not None], states


def test_full_saturday_produces_zero_wrong_pairs():
    games, states = cfb_saturday()
    expected = load_fixture("expected_cfb_matches.json")
    result = match_games(games, states)
    got = {m.polymarket.event_slug: m.espn.feed_id for m in result.matches}
    wrong = {slug: fid for slug, fid in got.items() if expected.get(slug) != fid}
    assert wrong == {}
    assert len(got) == len(expected) == 58
    assert result.unmatched_espn == []
    assert result.ambiguous == []
    # Unmatched games are allowed: these are FCS games ESPN's FBS feed does not carry.
    assert len(result.unmatched_polymarket) == len(games) - 58
    assert all(
        len({m.home_team.abbreviation, m.away_team.abbreviation}) == 2 for m in result.matches
    )


def test_orientation_follows_espn_home_and_away():
    games, states = cfb_saturday()
    result = match_games(games, states)
    by_slug = {m.polymarket.event_slug: m for m in result.matches}
    m = by_slug["cfb-ga-ala-2026-10-10"]
    assert m.espn.home.abbreviation == "ALA"
    assert m.home_team.name == "Alabama"
    assert m.away_team.name == "Georgia"


def test_nfl_matches_only_the_game_both_feeds_carry():
    games = [pm_parse(e, League.NFL) for e in load_fixture("pm_nfl_events.json")["events"]]
    games = [g for g in games if g is not None]
    states = parse_scoreboard(load_fixture("espn_nfl_scoreboard.json"), League.NFL, at(0))
    result = match_games(games, states)
    assert [(m.polymarket.event_slug, m.espn.feed_id) for m in result.matches] == [
        ("nfl-atl-no-2026-10-05", "401872979")
    ]
    assert result.matches[0].home_team.abbreviation == "NO"
    assert result.matches[0].away_team.abbreviation == "ATL"


def test_kickoff_more_than_two_hours_apart_does_not_match():
    # The feeds can disagree by an hour on a kickoff (Hawai'i at Arizona State is
    # listed at 01:30Z by ESPN and 02:30Z by Polymarket), so the window is two
    # hours. Five hours off must leave nothing matched.
    games, states = cfb_saturday()
    shifted = [replace(s, kickoff=s.kickoff + timedelta(hours=5)) for s in states]
    assert match_games(games, shifted).matches == []


def test_same_teams_twice_in_the_window_is_ambiguous_and_unmatched():
    games, states = cfb_saturday()
    target = next(s for s in states if s.home.abbreviation == "ALA")
    duplicate = replace(target, feed_id="999999", kickoff=target.kickoff + timedelta(minutes=30))
    result = match_games(games, states + [duplicate])
    assert "cfb-ga-ala-2026-10-10" in result.ambiguous
    assert all(m.polymarket.event_slug != "cfb-ga-ala-2026-10-10" for m in result.matches)


def test_two_polymarket_games_wanting_one_espn_game_are_both_unmatched():
    games, states = cfb_saturday()
    original = next(g for g in games if g.event_slug == "cfb-ga-ala-2026-10-10")
    clone = replace(original, event_slug="cfb-ga-ala-dup", event_id="x")
    result = match_games(games + [clone], states)
    assert {"cfb-ga-ala-2026-10-10", "cfb-ga-ala-dup"} <= set(result.ambiguous)


def test_team_name_rules():
    espn = Team("Miami (OH) RedHawks", "M-OH", "1", location="Miami (OH)")
    assert team_matches(MarketTeam(1, "Miami (OH)", "MOH", True, "RedHawks"), espn)
    assert not team_matches(MarketTeam(2, "Miami", "MIA", True, "Hurricanes"), espn)
    assert team_matches(
        MarketTeam(3, "UMass", "UMASS", True),
        Team("Massachusetts Minutemen", "MASS", "2", location="Massachusetts"),
    )
    assert team_matches(
        MarketTeam(4, "NM State", "NMSU", True),
        Team("New Mexico State Aggies", "NMSU", "3", location="New Mexico State"),
    )
    assert team_matches(
        MarketTeam(5, "Washington Commanders", "WAS", True),
        Team("Washington Commanders", "WSH", "4", location="Washington"),
    )
    assert team_matches(
        MarketTeam(6, "San Jose State", "SJSU", True),
        Team("San José State Spartans", "SJSU", "5", location="San José State"),
    )
    assert not team_matches(MarketTeam(7, "", "", True), espn)


def test_normalize():
    assert normalize("Texas A&M") == "texas a and m"
    assert normalize("San José State") == "san jose state"
    assert normalize("Appalachian State") == "app state"
    assert normalize("Miami (OH)") == "miami oh"
    assert normalize(None) == ""
