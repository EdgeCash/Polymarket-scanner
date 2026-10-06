from __future__ import annotations

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
    assert client.get("/").json()["pages"] == ["/health", "/scorecard", "/matchups"]


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
    status = RuntimeStatus()
    status.gamelog = {"last_refresh": at_time.isoformat(), "games": {"nfl": 1}, "backlog": 0}
    client = TestClient(create_app(settings, diary, status))
    assert client.get("/matchups").status_code == 401
    listing = client.get("/matchups?token=phone-secret").text
    assert "<h2>NFL</h2>" in listing and f"href='/matchup/nfl/{upcoming.game_id}'" in listing
    assert "?token=" not in listing and "NFL 1 games" in listing
    cle_logo = "https://a.espncdn.com/i/teamlogos/nfl/500/scoreboard/cle.png"
    assert f"<img class='logo-sm' src='{cle_logo}' alt='CLE'>" in listing
    assert "CLE -2.5, total 41.5" in listing and "implied 19.5 – 22.0" in listing
    page = client.get(f"/matchup/nfl/{upcoming.game_id}?token=phone-secret").text
    assert "<title>PIT at CLE</title>" in page
    assert "Pittsburgh Steelers" in page and "Cleveland Browns" in page
    assert f"<img src='{cle_logo}' alt='CLE'>" in page  # the team card
    assert "<span>Market implied score</span><b>PIT 19.5 – CLE 22.0</b>" in page
    assert "<span>Book win probability</span><b>CLE 58% / PIT 42%</b>" in page
    assert "Off 1st" in page and "Overall 1st of 2" in page and "Def 2nd" in page
    assert "61°F, 10% rain, gusts 8 mph" in page
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
    assert "<th>Game log</th><td>last" in health and "NFL 1 games" in health


def replace_slate(game, **changes):
    from dataclasses import replace

    return replace(game, **changes)
