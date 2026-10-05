from __future__ import annotations

from fastapi.testclient import TestClient

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
    assert client.get("/").json()["pages"] == ["/health", "/scorecard"]
