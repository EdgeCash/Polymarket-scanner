"""Shared test helpers. Unit tests never touch the network."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scanner import config

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


T0 = datetime(2026, 10, 11, 20, 0, 0, tzinfo=UTC)


def at(seconds: float = 0.0) -> datetime:
    """A fixed, aware UTC time plus an offset, for deterministic freshness tests."""
    return T0 + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Tests start from a blank environment so host settings cannot leak in."""
    for key in list(os.environ):
        if key.isupper() and key in {
            "ALERTS_ENABLED",
            "CLINCHED_OVERS_ENABLED",
            "LEAGUES",
            "DRY_RUN",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "STATUS_TOKEN",
            "DATABASE_PATH",
            "MIN_EDGE",
            "MIN_FAIR",
            "MAX_ALERTS_PER_DAY",
            "PERIOD_MARKETS_ENABLED",
            "PERIOD_ALERTS_ENABLED",
            "OBSERVATION_MINUTES_LEFT",
            "PREGAME_ENABLED",
            "PREGAME_SPORTS",
            "PREGAME_SCAN_MINUTES",
            "PREGAME_MIN_EDGE",
            "PREGAME_HORIZON_HOURS",
            "GIT_COMMIT",
            "RAILWAY_GIT_COMMIT_SHA",
        }:
            monkeypatch.delenv(key, raising=False)
    # Ignore any local .env file so tests are the same on every machine.
    config.Settings.model_config["env_file"] = None
    yield
