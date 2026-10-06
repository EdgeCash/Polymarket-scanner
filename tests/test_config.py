from __future__ import annotations

import json
import logging

import pytest

from scanner import __main__ as entry
from scanner.config import Settings, load_settings


def test_defaults_match_the_brief():
    s = load_settings()
    assert s.ALERTS_ENABLED is False
    assert s.CLINCHED_OVERS_ENABLED is True
    assert s.leagues == ("nfl", "cfb")
    assert s.SCORE_POLL_SECONDS == 5
    assert s.PRICE_POLL_SECONDS == 3
    assert s.MAX_MINUTES_LEFT == 8
    assert s.MIN_FAIR == 0.93
    assert s.MIN_EDGE == 0.03
    assert s.MIN_EDGE_CLINCHED == 0.02
    assert s.MIN_DOLLARS_AVAILABLE == 50
    assert s.SCORE_COOLDOWN_SECONDS == 20
    assert s.CLINCH_COOLDOWN_SECONDS == 60
    assert s.REPEAT_ALERT_MINUTES == 5
    assert s.MAX_ALERTS_PER_DAY == 10
    assert s.CFB_EXTRA_MARGIN == 0.01
    assert s.DATABASE_PATH == "/data/diary.db"
    assert s.TZ == "America/Chicago"
    assert s.TELEGRAM_BOT_TOKEN is None
    assert s.telegram_configured is False


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("LEAGUES", "NFL")
    monkeypatch.setenv("MIN_EDGE", "0.04")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    s = load_settings()
    assert s.ALERTS_ENABLED is True
    assert s.leagues == ("nfl",)
    assert s.MIN_EDGE == 0.04
    assert s.telegram_configured is True


def test_unknown_league_is_rejected(monkeypatch):
    monkeypatch.setenv("LEAGUES", "nfl,nba")
    with pytest.raises(ValueError):
        load_settings()


def test_redacted_hides_every_secret():
    s = Settings(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="99", STATUS_TOKEN="shh")
    out = s.redacted()
    text = json.dumps(out)
    assert "123:abc" not in text
    assert "shh" not in text
    assert out["TELEGRAM_BOT_TOKEN"] == "<set>"
    assert out["STATUS_TOKEN"] == "<set>"
    assert out["TELEGRAM_CHAT_ID"] == "<set>"
    # repr of the settings object must not leak either
    assert "123:abc" not in repr(s)


def test_dry_run_logs_settings_with_secrets_hidden_and_exits_cleanly(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")
    monkeypatch.setenv("STATUS_TOKEN", "status-secret")
    caplog.set_level(logging.INFO)
    code = entry.main(["--dry-run"])
    assert code == 0
    captured = caplog.text
    assert "settings:" in captured
    assert "secret-token" not in captured
    assert "status-secret" not in captured
    assert 'ALERTS_ENABLED": false' in captured
    assert "nothing is sent" in captured


def test_commit_comes_from_railways_variable_or_git_commit(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    assert Settings().short_commit == "unknown"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "e039edb1234567890abcdef\n")
    assert Settings().GIT_COMMIT == "e039edb1234567890abcdef\n"
    assert Settings().short_commit == "e039edb"
    monkeypatch.setenv("GIT_COMMIT", "abc1234ffff")
    assert Settings().short_commit == "abc1234"
    monkeypatch.delenv("GIT_COMMIT")
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA")
    assert Settings(GIT_COMMIT="  0123456789  ").short_commit == "0123456"


def test_period_market_and_observation_defaults():
    s = Settings()
    assert s.PERIOD_MARKETS_ENABLED is True and s.PERIOD_ALERTS_ENABLED is False
    assert s.OBSERVATION_MINUTES_LEFT == 15
    assert Settings(OBSERVATION_MINUTES_LEFT=0).OBSERVATION_MINUTES_LEFT == 0
