"""Settings, read from environment variables.

Every alert threshold lives here with the default from the build brief. Only the
owner changes them, by setting the variable on the host. Secrets are ``SecretStr``
so they never appear in logs or the status page.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed operating constants from the brief. These are not settings: changing them
# changes the safety rules, which the brief says needs the owner's say-so.
SCORE_STALE_SECONDS = 15.0  # a score read older than this cannot trigger an alert
PRICE_STALE_SECONDS = 5.0  # a price read older than this cannot trigger an alert
MODEL_ESPN_MAX_DISAGREEMENT = 0.05  # model and ESPN must agree within 5 cents
ESPN_MISSING_MARGIN = 0.02  # fair = model - 2 cents when ESPN has no number
DEFAULT_THETA = 0.0695  # taker fee coefficient from the fee schedule, 1 Oct 2026
CLINCHED_FAIR_PRICE = 0.995  # a clinched over is priced at 99.5 cents, not 100
KNEEL_FAIR_PRICE = 0.999  # leader has the ball and can kneel the clock out
MODEL_MAX_PRICE = 0.995  # the model alone never prices anything above 99.5 cents
NFL_TWO_MINUTE_WARNING = 120.0  # the NFL clock stops once at 2:00, like an extra timeout
KNEEL_SECONDS_PER_DOWN = 40.0  # a kneel burns a 40 second play clock
GAME_LIST_REFRESH_SECONDS = 60.0  # how often the Polymarket game list is re-read
POLYMARKET_MAX_RPS = 5.0  # stay well under the 25/s published limit
FEED_FAILURE_ALERT_SECONDS = 120.0  # a source failing this long sends a message
FEED_FAILURE_REPEAT_SECONDS = 900.0  # and repeats at most every 15 minutes
WAKE_BEFORE_KICKOFF_SECONDS = 30 * 60  # the loop wakes 30 minutes before kickoff
FOLLOW_UP_SECONDS = (30, 120)  # re-read the buy price this long after an alert

LEAGUE_NAMES = ("nfl", "cfb")
SECRET_FIELDS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "STATUS_TOKEN")


class Settings(BaseSettings):
    """All runtime settings. Field names match the environment variable names."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Switches
    ALERTS_ENABLED: bool = False
    CLINCHED_OVERS_ENABLED: bool = True
    LEAGUES: str = "nfl,cfb"
    DRY_RUN: bool = False
    # Set to true once, from the host's variables page, to prove the phone is
    # reachable: one test message goes out at startup whatever ALERTS_ENABLED says.
    SEND_TEST_MESSAGE_ON_START: bool = False

    # Polling
    SCORE_POLL_SECONDS: float = Field(5, gt=0)
    PRICE_POLL_SECONDS: float = Field(3, gt=0)

    # Winner alert rules
    MAX_MINUTES_LEFT: float = Field(8, gt=0)
    MIN_FAIR: float = Field(0.93, ge=0, le=1)
    MIN_EDGE: float = Field(0.03, ge=0, le=1)
    MIN_DOLLARS_AVAILABLE: float = Field(50, ge=0)
    SCORE_COOLDOWN_SECONDS: float = Field(20, ge=0)
    REPEAT_ALERT_MINUTES: float = Field(5, ge=0)
    MAX_ALERTS_PER_DAY: int = Field(10, ge=0)
    CFB_EXTRA_MARGIN: float = Field(0.01, ge=0, le=1)

    # Clinched-over rules
    MIN_EDGE_CLINCHED: float = Field(0.02, ge=0, le=1)
    CLINCH_COOLDOWN_SECONDS: float = Field(60, ge=0)

    # Storage, time, web
    DATABASE_PATH: str = "/data/diary.db"
    TZ: str = "America/Chicago"
    PORT: int = Field(8080, ge=1, le=65535)
    LOG_LEVEL: str = "INFO"

    # Secrets. Never logged, never shown.
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_CHAT_ID: SecretStr | None = None
    STATUS_TOKEN: SecretStr | None = None

    @field_validator("LEAGUES")
    @classmethod
    def _check_leagues(cls, value: str) -> str:
        names = [part.strip().lower() for part in value.split(",") if part.strip()]
        unknown = [n for n in names if n not in LEAGUE_NAMES]
        if unknown:
            raise ValueError(f"unknown league(s) {unknown}; choose from {LEAGUE_NAMES}")
        if not names:
            raise ValueError("LEAGUES must name at least one league")
        return ",".join(names)

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(self.LEAGUES.split(","))

    @property
    def telegram_configured(self) -> bool:
        return bool(
            self.TELEGRAM_BOT_TOKEN
            and self.TELEGRAM_BOT_TOKEN.get_secret_value()
            and self.TELEGRAM_CHAT_ID
            and self.TELEGRAM_CHAT_ID.get_secret_value()
        )

    def redacted(self) -> dict[str, Any]:
        """Settings as a plain dict with every secret replaced by a marker."""
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if isinstance(value, SecretStr):
                out[name] = "<set>" if value.get_secret_value() else "<empty>"
            elif value is None and name in SECRET_FIELDS:
                out[name] = "<not set>"
            else:
                out[name] = value
        return out


def load_settings(**overrides: Any) -> Settings:
    """Build settings from the environment, with keyword overrides for tests."""
    return Settings(**overrides)
