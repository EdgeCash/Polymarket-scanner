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
    assert client.get("/").json()["pages"] == ["/health", "/scorecard"]


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
