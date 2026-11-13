from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from scanner.books import BookLine
from scanner.config import load_settings
from scanner.diary import Diary
from scanner.web import RuntimeStatus, create_app
from tests.test_diary import winner_alert


def make_client(token="phone-secret", **overrides):
    settings = (
        load_settings(STATUS_TOKEN=token, **overrides) if token else load_settings(**overrides)
    )
    diary = Diary(":memory:")
    diary.record_alert(winner_alert(), sent=True)
    status = RuntimeStatus()
    status.games_watched = 12
    status.leagues = {"nfl": "nfl", "cfb": "cfb"}
    return TestClient(create_app(settings, diary, status)), settings


def test_pages_require_the_status_token():
    client, _ = make_client()
    assert client.get("/health").status_code == 401
    assert client.get("/scorecard").status_code == 401
    assert client.get("/scorecard?token=wrong").status_code == 401
    assert client.get("/scorecard?token=phone-secret").status_code == 200
    assert client.get("/health", headers={"X-Status-Token": "phone-secret"}).status_code == 200


def test_one_visit_with_the_token_signs_the_device_in():
    client, _ = make_client()
    assert client.get("/health").status_code == 401
    wrong = client.get("/health?token=wrong")
    assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
    signed = client.get("/health?token=phone-secret")
    assert signed.status_code == 200
    cookie = signed.headers["set-cookie"]
    assert cookie.startswith("scanner_token=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=31536000" in cookie
    # From now on the address needs no token on this device.
    assert client.get("/health").status_code == 200
    assert client.get("/scorecard").status_code == 200
    assert client.get("/matchups").status_code == 200
    assert "?token=" not in client.get("/health").text
    assert "/logout" in client.get("/health").text
    signed_out = client.get("/logout")
    assert signed_out.status_code == 200 and "Signed out" in signed_out.text
    assert client.get("/health").status_code == 401
    stranger, _ = make_client()
    stranger.cookies.set("scanner_token", "wrong")
    assert stranger.get("/health").status_code == 401
    # A token-less page never sets a cookie: only the token in the address signs in.
    assert (
        "set-cookie"
        not in client.get("/health", headers={"X-Status-Token": "phone-secret"}).headers
    )


def test_pages_are_closed_when_no_token_is_configured():
    client, _ = make_client(token=None)
    assert client.get("/health").status_code == 503
    assert client.get("/scorecard?token=anything").status_code == 503


def test_scorecard_renders_for_a_phone():
    client, _ = make_client()
    page = client.get("/scorecard?token=phone-secret").text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "max-width: 480px" in page
    assert "Paper results at the alerted price" in page
    assert "Winner alerts" in page and "Clinched-over alerts" in page
    assert "Near misses by reason" in page
    assert "Alerts recorded: <b>1</b>" in page
    assert "are NOT" in page  # ALERTS_ENABLED defaults to false


def test_health_renders_for_a_phone_and_as_json():
    client, _ = make_client()
    page = client.get("/health?token=phone-secret").text
    assert 'name="viewport"' in page and "max-width: 480px" in page
    assert "Games watched" in page and "12" in page
    assert "shadow mode" in page
    data = client.get("/health?token=phone-secret", headers={"accept": "application/json"}).json()
    assert data["games_watched"] == 12 and data["awake"] is False
    assert "version" in data


def test_health_shows_the_build_commit(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    client, _ = make_client()
    page = client.get("/health?token=phone-secret").text
    assert "<th>Build</th><td>unknown</td>" in page
    client, _ = make_client(GIT_COMMIT="e039edb0f1e2d3c4b5a6978877665544332211")
    page = client.get("/health?token=phone-secret").text
    assert "<th>Build</th><td>e039edb</td>" in page
    data = client.get("/health?token=phone-secret", headers={"accept": "application/json"}).json()
    assert data["commit"] == "e039edb"


def test_scorecard_json_matches_the_diary():
    client, _ = make_client()
    data = client.get(
        "/scorecard?token=phone-secret", headers={"accept": "application/json"}
    ).json()
    assert data["alerts_total"] == 1
    assert data["by_type"]["winner"]["alerts"] == 1


def test_pages_never_leak_the_token_or_secrets():
    client, _ = make_client()
    page = client.get("/health?token=phone-secret").text
    assert "phone-secret" not in page
    assert client.get("/").json()["pages"] == [
        "/health",
        "/scorecard",
        "/matchups",
        "/card",
        "/props",
    ]


def test_scorecard_near_miss_table_names_the_alert_type():
    from scanner.models import League, NearMiss
    from tests.conftest import at

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    diary.record_near_miss(
        NearMiss(at(1), League.NFL, "g", "s", "PHI", "stale score", 0.98, 0.93, None)
    )
    diary.record_near_miss(
        NearMiss(at(2), League.NFL, "g", "s", "OVER 47.5", "edge too small", 0.995, 0.99, 0.004)
    )
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    page = client.get("/scorecard?token=phone-secret").text
    assert "<td>Winner</td><td>stale score</td>" in page
    assert "<td>Over</td><td>edge too small</td>" in page


def test_scorecard_shows_the_period_and_observation_sections():
    client, _ = make_client()
    page = client.get("/scorecard?token=phone-secret").text
    assert "Period markets (quarter and half, decided)" in page
    assert "Observation window (8 to 15 minutes left, nothing sent)" in page
    assert "nothing recorded yet" in page
    client, _ = make_client(OBSERVATION_MINUTES_LEFT=0)
    page = client.get("/scorecard?token=phone-secret").text
    assert "<h2>Observation window</h2><p class='note'>off</p>" in page


def test_scorecard_observation_numbers_and_period_near_misses():
    from scanner.models import League, NearMiss
    from tests.conftest import at
    from tests.test_diary import observation

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    diary.record_observation(observation(would_alert=False, reason="rule 3: edge too small"))
    diary.record_observation(observation(created_at=at(30)))
    diary.record_near_miss(
        NearMiss(at(2), League.NFL, "g", "s", "1Q PHI +2.5", "market not open", 0.995, None, None)
    )
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    page = client.get("/scorecard?token=phone-secret").text
    assert "<th>Checks</th><td class='num'>2 on 1 games</td>" in page
    assert "<th>Would have alerted</th><td class='num'>1 picks (1 checks)</td>" in page
    assert "<td>rule 3: edge too small</td><td class='num'>1</td>" in page
    assert "<td>Period</td><td>market not open</td>" in page
    data = client.get(
        "/scorecard?token=phone-secret", headers={"accept": "application/json"}
    ).json()
    assert data["observations"]["picks"] == 1 and data["by_type"]["period"]["alerts"] == 0


def test_health_shows_the_period_and_observation_switches():
    client, _ = make_client()
    page = client.get("/health?token=phone-secret").text
    assert "<th>Period markets</th><td>recorded, not sent</td>" in page
    assert "<th>Observation window</th><td>8 to 15 min, nothing sent</td>" in page
    client, _ = make_client(PERIOD_ALERTS_ENABLED=True, OBSERVATION_MINUTES_LEFT=0)
    page = client.get("/health?token=phone-secret").text
    assert "<th>Period markets</th><td>alerts on</td>" in page
    assert "<th>Observation window</th><td>off</td>" in page
    client, _ = make_client(PERIOD_MARKETS_ENABLED=False)
    assert "<th>Period markets</th><td>off</td>" in client.get("/health?token=phone-secret").text


def test_scorecard_and_health_show_the_pregame_scan():
    from datetime import datetime

    from scanner.models import PregameGap

    client, _ = make_client()
    page = client.get("/scorecard?token=phone-secret").text
    assert (
        "Pre-game gaps (every sport, 3c edge, nothing sent)" in page
        and "no gaps recorded yet" in page
    )
    health = client.get("/health?token=phone-secret").text
    assert "<th>Pre-game scan</th><td>not run yet</td>" in health

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    diary.upsert_pregame_gap(
        PregameGap(
            created_at=datetime.fromisoformat("2026-10-06T14:00:00+00:00"),
            sport="mlb",
            feed_id="1",
            event_slug="mlb-lad-atl",
            start=None,
            home="ATL",
            away="LAD",
            market="moneyline",
            pick="ATL",
            pick_side="home",
            line=None,
            market_slug="aec",
            side_label="short",
            buy_price=0.5,
            fee=0.0174,
            book_fair=0.6429,
            book_odds=-200,
            edge=0.1255,
            provider="DraftKings",
        ),
        datetime.fromisoformat("2026-10-06T14:00:00+00:00"),
    )
    status = RuntimeStatus()
    status.pregame = {
        "last_scan": "2026-10-06T14:00:00+00:00",
        "matched": 12,
        "unmatched": 3,
        "open_gaps": 1,
        "error": None,
    }
    client = TestClient(create_app(settings, diary, status))
    page = client.get("/scorecard?token=phone-secret").text
    assert "<th>Gaps</th><td class='num'>1 (MLB 1)</td>" in page
    assert "<td>ATL</td><td class='num'>50.0c<br>book 64.3%</td>" in page
    assert "Beat the closing line" in page
    health = client.get("/health?token=phone-secret").text
    assert "12 games matched, 3 unmatched, 1 open gaps" in health
    data = client.get("/health?token=phone-secret", headers={"accept": "application/json"}).json()
    assert data["pregame"]["matched"] == 12
    client, _ = make_client(PREGAME_ENABLED=False)
    assert (
        "<h2>Pre-game gaps</h2><p class='note'>off</p>"
        in client.get("/scorecard?token=phone-secret").text
    )


# -- matchup sheets ---------------------------------------------------------------


def test_matchup_pages_render_from_the_diary():
    from datetime import datetime

    from scanner.gamelog import parse_game_summary, parse_week_scoreboard
    from tests.conftest import load_fixture

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    at_time = datetime.fromisoformat("2026-10-06T12:00:00+00:00")
    for name in ("espn_nfl_summary_final.json",):
        diary.store_football_game(parse_game_summary(load_fixture(name), "nfl"), at_time)
    _, _, games = parse_week_scoreboard(load_fixture("espn_nfl_week4.json"), "nfl")
    book = BookLine("DraftKings", -150, 130, None, 41.5, -110, -110, -2.5, -110, -110)
    upcoming = replace_slate(games[0], completed=False, date=at_time, book=book)
    diary.store_football_upcoming(upcoming, at_time)
    diary.update_football_upcoming_extra(
        "nfl",
        upcoming.game_id,
        {"weather": {"temperature": 61, "precipitation": 10, "gust": 8}},
        at_time,
    )
    from scanner.projection import fit, project

    ratings = fit(diary.football_games("nfl"), "nfl")
    conditions = {"venue": {"indoor": False}, "weather": {"gust": 8}}
    projection = project(ratings, "5", "23", False, book, conditions)  # CLE at home to PIT
    diary.upsert_projection("nfl", upcoming.game_id, at_time, "CLE", "PIT", projection, at_time)
    stake = {
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
    kickoff = datetime.fromisoformat("2026-10-07T00:00:00+00:00")
    diary.sync_stakes(
        "nfl", upcoming.game_id, kickoff, "CLE", "PIT", [stake], False, 1000, None, at_time
    )
    status = RuntimeStatus()
    status.gamelog = {
        "last_refresh": at_time.isoformat(),
        "games": {"nfl": 1},
        "backlog": 0,
        "stale": 12,
        "projected": 1,
        "staked": 1,
    }
    client = TestClient(create_app(settings, diary, status))
    assert client.get("/matchups").status_code == 401
    listing = client.get("/matchups?token=phone-secret").text
    assert "<h2>NFL</h2>" in listing and f"href='/matchup/nfl/{upcoming.game_id}'" in listing
    assert "?token=" not in listing and "NFL 1 games" in listing
    cle_logo = "https://a.espncdn.com/i/teamlogos/nfl/500/scoreboard/cle.png"
    assert f"<img class='logo-sm' src='{cle_logo}' alt='CLE'>" in listing
    assert "CLE -2.5, total 41.5" in listing and "implied 19.5 – 22.0" in listing
    assert f"model CLE {-projection['raw']['margin']:+.1f}, total" in listing
    page = client.get(f"/matchup/nfl/{upcoming.game_id}?token=phone-secret").text
    assert "<title>PIT at CLE</title>" in page
    assert "Pittsburgh Steelers" in page and "Cleveland Browns" in page
    assert f"<img src='{cle_logo}' alt='CLE'>" in page  # the team card
    assert "<span>Market implied score</span><b>PIT 19.5 – CLE 22.0</b>" in page
    assert "<span>Book win probability</span><b>CLE 58% / PIT 42%</b>" in page
    assert "Off 1st" in page and "Overall 1st of 2" in page and "Def 2nd" in page
    assert "<h2>Projection</h2>" in page and "<th>Model</th><th>Blend</th><th>Book</th>" in page
    assert (
        "<td class='label'>Spread (CLE)</td>" in page and "<td class='label'>1st half</td>" in page
    )
    assert "Model vs book: " in page and "points more than the book" in page
    assert "Thin: fewer than 3 games" in page
    assert "<h2>Stake</h2><p class='note'>Hidden until the model has earned it: 0 of 50 NFL" in page
    assert "no closing lines graded yet" in page and "$50" not in page
    shown = (
        TestClient(
            create_app(
                load_settings(STATUS_TOKEN="phone-secret", STAKE_GATE_GAMES=0), diary, status
            )
        )
        .get(f"/matchup/nfl/{upcoming.game_id}?token=phone-secret")
        .text
    )
    assert "<th class='label'>Side</th><th>Stake</th><th>Price</th>" in shown
    assert "<td class='label'>CLE<br><span class='note'>moneyline</span></td>" in shown
    assert "<td class='l3'>$50</td><td>50.0c</td><td>62.0%</td><td>+10.3c</td>" in shown
    assert "capped at 5% of a $1,000 bankroll" in shown and "nothing is placed" in shown
    assert "1 and 1 games used, blend 14% model" in page
    assert "61°F, 10% rain, gusts 8 mph" in page
    # The wind's effect on the model's total sits in the projection block.
    assert "<td class='label'>Weather</td>" in page
    assert "wind 5 mph (gusts 8): total +0.2 on the model's total" in page
    assert "<td class='label'>Points</td>" in page and "<h2>Defense</h2>" in page
    assert "vs PIT 27-24" in page  # CLE's last result, from the log
    assert "Nothing on this page is a recommendation" in page
    data = client.get(
        f"/matchup/nfl/{upcoming.game_id}?token=phone-secret",
        headers={"accept": "application/json"},
    ).json()
    assert data["home"]["abbreviation"] == "CLE" and data["league_size"] == 2
    assert client.get("/matchup/nfl/999?token=phone-secret").status_code == 404
    assert client.get("/matchup/mlb/1?token=phone-secret").status_code == 404
    health = client.get("/health?token=phone-secret").text
    assert "<th>Game log</th><td>last" in health
    assert "NFL 1 games, 12 to re-read, 1 sheets projected, 1 stakes open" in health
    data = client.get(
        f"/matchup/nfl/{upcoming.game_id}", headers={"accept": "application/json"}
    ).json()
    assert data["projection"]["raw"]["margin"] == pytest.approx(projection["raw"]["margin"])


def test_scorecard_shows_the_projection_model():
    from datetime import UTC, datetime, timedelta

    from scanner.gamelog import parse_game_summary
    from tests.conftest import load_fixture

    client, _ = make_client()
    scorecard = client.get("/scorecard?token=phone-secret").text
    assert "Projection model (graded" in scorecard and "no projections yet" in scorecard

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    kickoff = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
    projection = {
        "model": "v1",
        "raw": {"margin": 4.0, "total": 44.0, "home_win": 0.6},
        "blend": {"margin": 3.0, "total": 43.0, "home_win": 0.58},
        "book": {"margin": 2.0, "total": 42.0, "home_win": 0.55},
    }
    diary.upsert_projection("nfl", record.game_id, kickoff, "CLE", "PIT", projection, kickoff)
    diary.store_football_game(record, kickoff + timedelta(hours=4))
    assert diary.grade_projections("nfl", kickoff + timedelta(hours=6)) == 1
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    scorecard = client.get("/scorecard?token=phone-secret").text
    assert "<th>Graded</th><td class='num'>1 (NFL 1), 0 open</td>" in scorecard
    assert (
        "<th>Avg margin miss: model / blend / book</th><td class='num'>1.0 / 0.0 / 1.0</td>"
        in scorecard
    )
    assert (
        "Spread record, model 1+ points off the book</th><td class='num'>1-0-0 (100%)</td>"
        in scorecard
    )
    assert "Total record, model 5+ points off the book</th><td class='num'>0-0-0</td>" in scorecard
    assert "<tr><th colspan='2'>NFL</th></tr>" in scorecard
    assert "NFL PIT at CLE" in scorecard and "CLE -4.0 / 44.0" in scorecard and "24-27" in scorecard
    assert "home win" in scorecard and "over win" in scorecard
    data = client.get(
        "/scorecard?token=phone-secret", headers={"accept": "application/json"}
    ).json()
    assert data["projections"]["graded"] == 1
    # The monthly check: the last full month and this month so far, against the close.
    assert "Monthly check (the model against the finals and the closing lines)" in scorecard
    months = data["monthly"]
    assert len(months) == 2 and all(set(m) >= {"month", "games", "sports"} for m in months)
    graded_month = next((m for m in months if m["games"]), None)
    if graded_month:  # the fixture's October 2026 game, while that month is on the page
        nfl = graded_month["sports"]["nfl"]
        assert nfl["games"] == 1 and nfl["brier"]["model"] == pytest.approx(0.16)
        assert "Brier: model / blend / close" in scorecard
    else:
        assert "nothing graded" in scorecard
    off = make_client(PROJECTION_ENABLED=False)[0].get("/scorecard?token=phone-secret").text
    assert "<h2>Projection model</h2><p class='note'>off</p>" in off
    assert "<h2>Monthly check</h2><p class='note'>off</p>" in off
    assert "<h2>Stake suggestions</h2><p class='note'>off</p>" in off


def test_scorecard_shows_the_stake_suggestions():
    from datetime import UTC, datetime, timedelta

    from scanner.gamelog import parse_game_summary
    from tests.conftest import load_fixture

    client, _ = make_client()
    assert "no suggestions yet" in client.get("/scorecard?token=phone-secret").text
    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    kickoff = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
    stake = {
        "market": "spread",
        "side": "home",
        "side_label": "CLE -2.5",
        "line": -2.5,
        "buy_price": 0.45,
        "fee": 0.0172,
        "model_prob": 0.544,
        "edge": 0.0768,
        "kelly": 0.1441,
        "share": 0.036,
        "stake": 36.0,
        "contracts": 77.0,
    }
    diary.sync_stakes(
        "nfl",
        record.game_id,
        kickoff,
        "CLE",
        "PIT",
        [stake],
        True,
        1000,
        None,
        kickoff - timedelta(hours=2),
    )
    diary.store_football_game(record, kickoff + timedelta(hours=4))
    assert diary.grade_stakes("nfl", kickoff + timedelta(hours=6)) == 1
    page = (
        TestClient(create_app(settings, diary, RuntimeStatus()))
        .get("/scorecard?token=phone-secret")
        .text
    )
    assert "Stake suggestions (shadow until the gate opens" in page
    assert "<th>Suggested</th><td class='num'>1 (0 open, 1 shown on a sheet)</td>" in page
    assert "<th>Wins / losses / pushes</th><td class='num'>1 / 0 / 0</td>" in page
    assert "<th>Staked (paper)</th><td class='num'>$36.00</td>" in page
    assert "NFL PIT at CLE" in page and "CLE -2.5<br><span class='note'>spread</span>" in page
    assert "win $" in page and "model 54.4%" in page


def replace_slate(game, **changes):
    from dataclasses import replace

    return replace(game, **changes)


def test_hockey_and_basketball_sheets_use_their_own_words():
    from datetime import datetime

    from scanner.gamelog import parse_game_summary, parse_week_scoreboard
    from scanner.projection import fit, project
    from tests.conftest import load_fixture

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    at_time = datetime.fromisoformat("2026-10-28T16:00:00+00:00")
    nhl_book = BookLine("DraftKings", -140, 120, None, 6.5, -110, -110, -1.5, 110, -130)
    nba_book = BookLine("DraftKings", -300, 240, None, 223.5, -110, -110, -7.5, -110, -110)
    games = {}
    for sport, summary, day, home, book in (
        ("nhl", "espn_nhl_summary_final.json", "espn_nhl_day.json", "NSH", nhl_book),
        ("nba", "espn_nba_summary_final.json", "espn_nba_day.json", "BOS", nba_book),
    ):
        diary.store_football_game(parse_game_summary(load_fixture(summary), sport), at_time)
        _, _, slate = parse_week_scoreboard(load_fixture(day), sport)
        game = next(g for g in slate if g.home.abbreviation == home)
        diary.store_football_upcoming(
            replace_slate(game, completed=False, date=at_time, book=book), at_time
        )
        ratings = fit(diary.football_games(sport), sport)
        projection = project(ratings, game.home.team_id, game.away.team_id, False, book)
        diary.upsert_projection(
            sport, game.game_id, at_time, home, game.away.abbreviation, projection, at_time
        )
        games[sport] = game
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    listing = client.get("/matchups?token=phone-secret").text
    assert "Upcoming games with a matchup sheet" in listing
    assert all(f"<h2>{s}</h2>" in listing for s in ("NFL", "CFB", "NBA", "NHL"))
    assert "NSH -140 / NYI +120, total 6.5" in listing and "BOS -7.5, total 223.5" in listing
    assert "implied 108.0 – 115.5" in listing  # basketball splits the total by the spread
    assert "implied 3.1 – 3.4" in listing  # hockey splits it by the moneyline's margin

    page = client.get(f"/matchup/nhl/{games['nhl'].game_id}?token=phone-secret").text
    assert "<title>NYI at NSH</title>" in page and "<span>Puck drop</span>" in page
    assert "<span>Surface</span>" not in page  # an arena: nothing to say
    assert "<td class='label'>Margin (NSH)</td>" in page and "<th>1st period</th>" in page
    assert "<td class='label'>1st period</td>" in page and "Goals allowed" in page
    assert "Shooting %" in page and "Penalty kill %" in page and "<h2>Situational</h2>" in page
    assert "goals more than the book" in page and "Ratings in goals" in page
    assert "home ice 0.2" in page and "goals scored in play" in page
    assert "split by its moneyline" in page and "Thin: fewer than 3 games" in page
    assert "<span>Market implied score</span><b>NYI " in page
    data = client.get(
        f"/matchup/nhl/{games['nhl'].game_id}?token=phone-secret",
        headers={"accept": "application/json"},
    ).json()
    assert data["home"]["season"]["save_pct"] is not None and data["projection"]["sport"] == "nhl"

    page = client.get(f"/matchup/nba/{games['nba'].game_id}?token=phone-secret").text
    assert "<title>BKN at BOS</title>" in page and "<span>Tip-off</span>" in page
    assert "<td class='label'>Spread (BOS)</td>" in page and "<th>1st half</th>" in page
    assert "Points per 100 poss" in page and "Rebound margin" in page
    assert "home court 2" in page and "points more than the book" in page
    assert "Possessions are estimated" in page and "split by its spread" in page
    assert "<span>Market implied score</span><b>BKN 108.0 – BOS 115.5</b>" in page
    assert client.get("/matchup/nhl/999?token=phone-secret").status_code == 404


def test_health_counts_the_season_days_still_to_read():
    from scanner.web import _gamelog_health

    status = RuntimeStatus()
    status.gamelog = {"last_refresh": "2026-10-28T16:00:00+00:00", "games": {"nhl": 80}}
    assert _gamelog_health(status, load_settings()).endswith(": NHL 80 games")
    status.gamelog["player_games"] = {"nhl": 30}
    assert _gamelog_health(status, load_settings()).endswith("NHL 80 games, 30 with player lines")
    status.gamelog["player_games"] = {"nhl": 80}
    assert _gamelog_health(status, load_settings()).endswith(": NHL 80 games")  # all filled in
    del status.gamelog["player_games"]
    status.gamelog["days_pending"] = 12
    assert _gamelog_health(status, load_settings()).endswith("NHL 80 games, 12 days still to read")


def test_the_daily_card_is_public_and_shows_the_picks_and_the_record():
    from datetime import UTC, datetime, timedelta

    from scanner.card import build_if_due
    from tests.test_card import seeded_diary

    client, _ = make_client()
    empty = client.get("/card")
    assert empty.status_code == 200 and "No card yet. The first locks at" in empty.text
    assert "10:00 AM ET" in empty.text and "phone-secret" not in empty.text
    assert client.get("/card/2026-10-11").status_code == 404
    assert client.get("/card/2026-10-11/1").status_code == 404
    assert client.get("/card", headers={"accept": "application/json"}).json()["card"] is None

    settings = load_settings(STATUS_TOKEN="phone-secret", GAMELOG_SPORTS="nfl,cfb")
    diary, record, first, second = seeded_diary()
    now = datetime(2026, 10, 11, 14, 5, tzinfo=UTC)
    assert build_if_due(diary, settings, now)["picks"] == 2
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    page = client.get("/card").text  # no token needed
    assert "<h1>Daily card, 2026-10-11</h1>" in page
    assert "Locked Sun Oct 11, 10:05 AM ET. 2 picks, one per game." in page
    assert '<span class="rank">#1</span>' in page and "Over 41.5" in page and "(-105)" in page
    assert "WSH +3.5" in page and "<span class='chip open'>open</span>" in page
    assert "In 100,000 simulated games Over 41.5 lands" in page and "the price says 51%" in page
    assert "<svg" in page and "book 41.5" in page and "blend 51.0" in page
    # The blend is what the pick is ranked and drawn on; the raw model sits beside it.
    assert "Blend 51.0 (model 51.0, 100% model) · book 41.5 · " in page
    assert "/card/2026-10-11/1'>Full simulation" in page and "Record by rank" not in page
    assert client.get("/card/2026-10-11").text == page
    # Each pick shows the edge it was ranked by: its chance against what the price needs.
    assert re.search(r"Edge \+\d+\.\d points: the blend's \d+\.\d% against the 51\.2%", page)
    # A pick locked before the card ranked by edge keeps its old words.
    from scanner.web import _pick_block

    old_pick = diary.card_pick("2026-10-11", 1)
    old_pick["detail"] = {
        k: v
        for k, v in old_pick["detail"].items()
        if k not in ("edge", "chance_no_push", "break_even")
    }
    assert "Conviction 0." in _pick_block(old_pick, settings, link=False)
    # A pick locked when the card ranked on the raw model says so.
    old_pick["detail"] = {k: v for k, v in old_pick["detail"].items() if k != "basis"}
    assert "Model 51.0 · book 41.5" in _pick_block(old_pick, settings, link=False)

    # The pick's own page re-runs the same simulation with every score and line.
    pick = client.get("/card/2026-10-11/1").text
    assert "<h1>Pick #1, 2026-10-11</h1>" in pick and "Likeliest finals" in pick
    assert "blend 51.0" in pick  # the picture is the blend's, as the ranking was
    assert "Every score" in pick and "CLE score" in pick and "Spread at other lines" in pick
    assert "Total at other lines" in pick and "<td>41.5</td>" in pick
    assert "Full simulation" not in pick
    data = client.get("/card/2026-10-11/1", headers={"accept": "application/json"}).json()
    assert data["rank"] == 1 and data["detail"]["sim"]["draws"] == 100_000

    # Graded: the result chip, the final and the record appear.
    from dataclasses import replace

    kickoff = datetime.fromisoformat(data["kickoff"])
    diary.store_football_game(replace(record, game_id=first.game_id), kickoff + timedelta(hours=4))
    diary.grade_card_picks(kickoff + timedelta(hours=6))
    page = client.get("/card").text
    assert "<span class='chip win'>WIN +0.95u</span>" in page
    assert "Final PIT 24, CLE 27. Closing line moved -3.0 points toward the pick." in page
    assert "<span>Record</span><b>1-0-0</b>" in page and "Record by rank" in page
    assert "<th>Rank 1</th><td class='num'>1-0-0 (100%), +0.95u</td>" in page
    assert "<a href='/card/2026-10-11'>2026-10-11</a>" in page
    body = client.get("/card", headers={"accept": "application/json"}).json()
    assert body["record"]["wins"] == 1 and body["history"][0]["graded"] == 1

    scorecard = client.get("/scorecard?token=phone-secret").text
    assert "Daily card (chosen by rule" in scorecard
    assert "<th>Wins / losses / pushes</th><td class='num'>1 / 0 / 0</td>" in scorecard
    assert "<th>Rank 1</th><td class='num'>1-0-0 (100%), +0.95u</td>" in scorecard
    assert "2026-10-11 #1<br>NFL PIT at CLE" in scorecard
    off = make_client(CARD_ENABLED=False)[0].get("/scorecard?token=phone-secret").text
    assert "<h2>Daily card</h2><p class='note'>off</p>" in off

    # The simulation page for any projected game stays behind the token (the client above
    # was signed in by its first visit with the token, so a fresh one asks).
    fresh = TestClient(create_app(settings, diary, RuntimeStatus()))
    assert fresh.get(f"/sim/nfl/{second.game_id}").status_code == 401
    assert fresh.get("/card").status_code == 200
    sim = client.get(f"/sim/nfl/{second.game_id}?token=phone-secret")
    title = f"{second.away.abbreviation} at {second.home.abbreviation}"
    assert sim.status_code == 200 and "Every score" in sim.text and title in sim.text
    assert client.get("/sim/nfl/nope?token=phone-secret").status_code == 404
    assert client.get("/sim/mlb/x?token=phone-secret").status_code == 404


def test_health_shows_the_daily_card():
    client, _ = make_client()
    page = client.get("/health?token=phone-secret").text
    assert "<th>Daily card</th><td>none locked since start; locks at 10:00 America/New_York" in page
    settings = load_settings(STATUS_TOKEN="phone-secret")
    status = RuntimeStatus()
    status.gamelog = {
        "last_refresh": "2026-10-11T14:05:00+00:00",
        "games": {"nfl": 3},
        "card": {
            "card_date": "2026-10-11",
            "picks": 5,
            "late": True,
            "locked_at": "2026-10-11T14:05:00+00:00",
        },
    }
    page = (
        TestClient(create_app(settings, Diary(":memory:"), status))
        .get("/health?token=phone-secret")
        .text
    )
    assert "<th>Daily card</th><td>2026-10-11: 5 picks locked Sun 9:05:00 AM, late</td>" in page


def test_the_share_images_are_served_as_png_without_a_token():
    from tests.test_share import graded_card, png_size

    client, _ = make_client()
    assert client.get("/card.png").status_code == 404
    diary, settings = graded_card()
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    latest = client.get("/card.png")
    assert latest.status_code == 200 and latest.headers["content-type"] == "image/png"
    assert latest.headers["cache-control"] == "public, max-age=300"
    assert png_size(latest.content) == (2400, 1350)
    assert client.get("/card/2026-10-11.png").content == latest.content
    pick = client.get("/card/2026-10-11/1.png")
    assert pick.status_code == 200 and png_size(pick.content) == (2400, 1350)
    assert client.get("/card/2026-10-10.png").status_code == 404
    assert client.get("/card/2026-10-11/9.png").status_code == 404
    assert client.get("/card/2026-10-11").status_code == 200  # the page still answers
    page = client.get("/card").text
    assert "href='/card/2026-10-11.png'" in page and "href='/card/2026-10-11/1.png'" in page


def test_college_basketball_sheets_say_tip_off_and_home_court():
    from datetime import datetime

    from scanner.gamelog import parse_game_summary, parse_week_scoreboard
    from scanner.projection import fit, project
    from tests.conftest import load_fixture

    settings = load_settings(STATUS_TOKEN="phone-secret", GAMELOG_SPORTS="cbb")
    diary = Diary(":memory:")
    at_time = datetime.fromisoformat("2026-11-03T12:00:00+00:00")
    record = parse_game_summary(load_fixture("espn_cbb_summary_final.json"), "cbb")
    diary.store_football_game(record, at_time)
    _, _, games = parse_week_scoreboard(load_fixture("espn_cbb_day.json"), "cbb")
    duke = next(g for g in games if g.home.abbreviation == "DUKE")
    book = BookLine("DraftKings", None, None, None, 150.5, -110, -110, -30.5, -110, -110)
    upcoming = replace_slate(duke, completed=False, date=at_time, book=book)
    diary.store_football_upcoming(upcoming, at_time)
    ratings = fit(diary.football_games("cbb"), "cbb")
    projection = project(ratings, duke.home.team_id, duke.away.team_id, False, book)
    diary.upsert_projection("cbb", duke.game_id, at_time, "DUKE", "ARMY", projection, at_time)
    client = TestClient(create_app(settings, diary, RuntimeStatus()))
    listing = client.get("/matchups?token=phone-secret").text
    assert "<h2>CBB</h2>" in listing and "DUKE -30.5, total 150.5" in listing
    page = client.get(f"/matchup/cbb/{duke.game_id}?token=phone-secret").text
    assert "<title>ARMY at DUKE</title>" in page and "<span>Tip-off</span>" in page
    assert "<th>1st half</th>" in page and "home court 4" in page
    assert "Points per 100 poss" in page and "Division I games only" in page
    assert "Thin: fewer than 3 games" in page
    sim = client.get(f"/sim/cbb/{duke.game_id}?token=phone-secret").text
    assert "Spread at other lines" in sim and "Margin (DUKE, points)" in sim


def test_scorecard_shows_the_player_lines():
    client, _ = make_client()
    page = client.get("/scorecard?token=phone-secret").text
    assert "Player lines (every leader" in page and "no player lines yet" in page
    off = make_client(PLAYERS_ENABLED=False)[0].get("/scorecard?token=phone-secret").text
    assert "<h2>Player lines</h2><p class='note'>off</p>" in off
    from datetime import UTC, datetime, timedelta

    from scanner.gamelog import parse_game_summary
    from scanner.players import distribution, summarise_pmf
    from tests.conftest import load_fixture

    settings = load_settings(STATUS_TOKEN="phone-secret")
    diary = Diary(":memory:")
    record = parse_game_summary(load_fixture("espn_nba_summary_final.json"), "nba")
    tatum = next(p for p in record.players if p.name == "Jayson Tatum")
    kickoff = datetime(2026, 10, 30, 23, 0, tzinfo=UTC)
    params = {
        "rate": 0.7,
        "exposure_mu": 40.0,
        "exposure_sd": 4.0,
        "exposure_cap": 48.0,
        "factor": 1.0,
        "k": 12.0,
    }
    pmf = distribution(params)
    line = {
        "player_id": tatum.player_id,
        "stat": "points",
        "team_id": tatum.team_id,
        "team_abbr": "BOS",
        "opponent_abbr": "BKN",
        "home": True,
        "name": tatum.name,
        "position": "F",
        "kickoff": kickoff,
        "model": "p1",
        "params": params,
        "pmf": {str(x): p for x, p in pmf.items()},
        **summarise_pmf(pmf),
    }
    diary.replace_player_projections("nba", record.game_id, [line], kickoff - timedelta(hours=5))
    diary.store_football_game(record, kickoff + timedelta(hours=3))
    assert diary.grade_player_projections("nba", kickoff + timedelta(hours=6)) == 1
    page = (
        TestClient(create_app(settings, diary, RuntimeStatus()))
        .get("/scorecard?token=phone-secret")
        .text
    )
    assert "<th>Graded</th><td class='num'>1</td>" in page
    assert "<th>Actual above the median (ties half)</th><td class='num'>100.0%</td>" in page
    assert "<th>Inside the 80% band</th><td class='num'>100.0%</td>" in page
    assert "<tr><th colspan='2'>NBA (1)</th></tr>" in page and "<th>Points</th>" in page
    assert "Jayson Tatum (BOS v BKN)" in page and "<td class='num'>40</td>" in page


def props_client():
    """A log with an upcoming NBA game whose player lines are written, and a client."""
    from datetime import timedelta

    from scanner.gamelog import GameLog
    from tests.conftest import load_fixture
    from tests.test_gamelog import NBA_NOW, FakeDailyFeed, scheduled_copy

    settings = load_settings(STATUS_TOKEN="phone-secret", GAMELOG_SPORTS="nba")
    diary = Diary(":memory:")
    feed = FakeDailyFeed()
    yesterday = load_fixture("espn_nba_day.json")
    feed.days[("nba", "20261027")] = yesterday
    tonight = scheduled_copy(yesterday["events"][0], "up-1", NBA_NOW + timedelta(hours=7))
    feed.days[("nba", "20261028")] = {**yesterday, "events": [tonight]}
    log = GameLog(settings, diary, feed=feed, now=lambda: NBA_NOW)
    assert log.refresh("nba", NBA_NOW, budget=40).player_lines > 0
    return TestClient(create_app(settings, diary, RuntimeStatus())), diary, log


def test_player_lines_are_public_on_the_props_pages_and_the_sheet():
    from datetime import timedelta

    from scanner.gamelog import parse_game_summary
    from tests.conftest import load_fixture
    from tests.test_gamelog import NBA_NOW
    from tests.test_share import png_size

    empty, _ = make_client()
    page = empty.get("/props").text
    assert "<h1>Player lines</h1>" in page and "No games with player lines" in page
    assert empty.get("/props/nba/up-1").status_code == 404

    client, diary, log = props_client()
    lines = diary.player_projections("nba", "up-1")
    top = max((r for r in lines if r["stat"] == "points"), key=lambda r: r["mean"])
    assert top["name"] == "Jayson Tatum"
    index = client.get("/props").text  # no token
    assert "<h2>NBA</h2>" in index and "href='/props/nba/up-1'" in index
    assert f"{len(lines)} lines" in index and "Lines graded" not in index
    data = client.get("/props", headers={"accept": "application/json"}).json()
    assert data["games"]["nba"]["up-1"] == len(lines)

    game = client.get("/props/nba/up-1").text
    assert "<h1>BKN at BOS" in game and "<th class='label'>BOS</th>" in game
    assert "<th>Points</th>" in game and "<th>Threes</th>" in game
    assert f"href='/props/nba/up-1/{top['player_id']}'>Jayson Tatum</a>" in game
    assert f"{top['median']} ({top['p10']}–{top['p90']})" in game
    assert len(client.get("/props/nba/up-1", headers={"accept": "application/json"}).json()) == len(
        lines
    )

    player = client.get(f"/props/nba/up-1/{top['player_id']}").text
    assert "<h1>Jayson Tatum <span class='note'>F</span>" in player
    assert f"<h2>Points: {top['median']}" in player and "100,000 simulated games" in player
    assert (
        player.index("<h2>Points:") < player.index("<h2>Rebounds:") < player.index("<h2>Assists:")
    )
    assert "<svg" in player and f"median {top['median']}" in player
    assert "<th>Line</th><th>Over</th><th>Under</th>" in player
    assert "<h2>Recent games</h2>" in player and "<th>Min</th>" in player
    assert "Rate 0." in player and "games used" in player
    assert client.get("/props/nba/up-1/nobody").status_code == 404
    assert client.get("/props/nba/up-1/nobody.png").status_code == 404
    picture = client.get(f"/props/nba/up-1/{top['player_id']}.png")
    assert picture.status_code == 200 and png_size(picture.content) == (2400, 1350)

    sheet = client.get("/matchup/nba/up-1?token=phone-secret").text
    assert "<h2>Player lines</h2>" in sheet and "Jayson Tatum</a>" in sheet
    assert "href='/props/nba/up-1'>The public page</a>" in sheet

    # Graded: the actuals show on the game page, the player page and the record.
    from dataclasses import replace

    played = replace(
        parse_game_summary(load_fixture("espn_nba_summary_final.json"), "nba"), game_id="up-1"
    )
    diary.store_football_game(played, NBA_NOW + timedelta(hours=12))
    log.refresh("nba", NBA_NOW + timedelta(hours=14), budget=40)
    game = client.get("/props/nba/up-1").text
    assert "<br><b>40</b>" in game  # Tatum's 40 beside his line
    player = client.get(f"/props/nba/up-1/{top['player_id']}").text
    assert "<b>Actual 40</b>" in player and "actual 40" in player and "percentile" in player
    index = client.get("/props").text
    assert "Lines graded" in index and "Inside the 80% band" in index
    off = make_client(PLAYERS_ENABLED=False)[0].get("/matchup/nba/up-1?token=phone-secret")
    assert off.status_code == 404  # no game in that diary; the block itself is off


def test_a_sheet_without_lines_says_so():
    from tests.test_card import seeded_diary

    settings = load_settings(STATUS_TOKEN="phone-secret", GAMELOG_SPORTS="nfl,cfb")
    diary, _, first, _ = seeded_diary()
    page = (
        TestClient(create_app(settings, diary, RuntimeStatus()))
        .get(f"/matchup/nfl/{first.game_id}?token=phone-secret")
        .text
    )
    assert "<h2>Player lines</h2><p class='note'>none yet" in page


def test_the_root_is_a_public_front_door_for_browsers_and_json_for_the_rest():
    client, _ = make_client()
    page = client.get("/", headers={"accept": "text/html"})
    assert page.status_code == 200 and "<h1>Research, graded</h1>" in page.text
    assert "No card yet; the first locks at 10:00 AM ET" in page.text
    assert 'href="/card"' in page.text and "href='/props'" in page.text
    assert "phone-secret" not in page.text and "Build unknown." in page.text
    assert client.get("/").json()["pages"][0] == "/health"  # API clients still get the list
    built = make_client(GIT_COMMIT="4b0b26d267daad864f251f26b6fa16da2925718b")[0]
    assert "Build 4b0b26d." in built.get("/", headers={"accept": "text/html"}).text

    from datetime import UTC, datetime

    from scanner.card import build_if_due
    from tests.test_card import seeded_diary

    settings = load_settings(STATUS_TOKEN="phone-secret", GAMELOG_SPORTS="nfl,cfb")
    diary, _, _, _ = seeded_diary()
    build_if_due(diary, settings, datetime(2026, 10, 11, 14, 5, tzinfo=UTC))
    page = (
        TestClient(create_app(settings, diary, RuntimeStatus()))
        .get("/", headers={"accept": "text/html"})
        .text
    )
    assert "Card 2026-10-11, 2 picks." in page and "Over 41.5 (-105)" in page
    assert "<span class='chip open'>open</span>" in page


def test_the_owners_pages_link_to_the_card_and_the_player_lines():
    client, _ = make_client()
    for path in ("/health", "/scorecard", "/matchups"):
        page = client.get(f"{path}?token=phone-secret").text
        assert 'href="/card">Daily card</a>' in page and 'href="/props">Player lines</a>' in page
