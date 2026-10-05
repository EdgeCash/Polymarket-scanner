"""Phone alerts. One small interface, ``send(text)``, with Telegram behind it.

The bot token and chat id come from settings and never appear in logs. In
dry-run mode, or whenever ``ALERTS_ENABLED`` is false, nothing is sent: the
message is logged with a "would send" marker instead.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from scanner.config import Settings
from scanner.fees import break_even_rate, risk_and_reward
from scanner.models import Alert, AlertType, GameState, Side

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
ZONE_LABELS = {
    "America/Chicago": "CT",
    "America/New_York": "ET",
    "America/Denver": "MT",
    "America/Phoenix": "MT",
    "America/Los_Angeles": "PT",
}


class Sender(Protocol):
    def send(self, text: str) -> bool: ...


class DryRunSender:
    """Logs instead of sending. Used when no Telegram settings exist."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        log.info("DRY RUN, would send:\n%s", text)
        return True


class TelegramSender:
    """Sends a plain-text message to one chat through the Bot API."""

    def __init__(
        self, token: str, chat_id: str, client: httpx.Client | None = None, timeout: float = 10.0
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=timeout)

    def send(self, text: str) -> bool:
        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        try:
            response = self._client.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            )
        except httpx.HTTPError as exc:
            log.error("telegram send failed: %s", type(exc).__name__)
            return False
        if response.status_code != 200:
            # Never log the URL: it carries the token.
            log.error("telegram send failed: HTTP %s %s", response.status_code, response.text[:200])
            return False
        try:
            ok = bool(response.json().get("ok"))
        except ValueError:
            ok = False
        if not ok:
            log.error("telegram send failed: %s", response.text[:200])
        return ok


def build_sender(settings: Settings) -> Sender:
    if settings.DRY_RUN or not settings.telegram_configured:
        return DryRunSender()
    assert settings.TELEGRAM_BOT_TOKEN is not None and settings.TELEGRAM_CHAT_ID is not None
    return TelegramSender(
        settings.TELEGRAM_BOT_TOKEN.get_secret_value(), settings.TELEGRAM_CHAT_ID.get_secret_value()
    )


# -- formatting ---------------------------------------------------------------


def cents(price: float) -> str:
    """0.93 -> '93c', 0.995 -> '99.5c'."""
    value = round(price * 100, 1)
    if value == int(value):
        return f"{int(value)}c"
    return f"{value:.1f}c"


def cents_1(price: float) -> str:
    """Always one decimal: 0.0604 -> '6.0c'."""
    return f"{price * 100:.1f}c"


def clock(seconds_left_in_period: float | None) -> str:
    if seconds_left_in_period is None:
        return "?:??"
    total = int(round(seconds_left_in_period))
    return f"{total // 60}:{total % 60:02d}"


def local_time(moment: datetime, tz: str) -> str:
    local = moment.astimezone(ZoneInfo(tz))
    label = ZONE_LABELS.get(tz) or local.tzname() or tz
    return f"{local.strftime('%-I:%M:%S %p')} {label}"


def _header(alert: Alert) -> str:
    return f"{alert.league.value.upper()} - {alert.away} at {alert.home}"


def _situation_line(alert: Alert) -> str:
    s = alert.situation
    home_score, away_score = s.get("home_score"), s.get("away_score")
    period, clock_seconds = s.get("period"), s.get("clock_seconds")
    if home_score is None or away_score is None:
        score = "score unknown"
    elif home_score == away_score:
        score = f"Tied {home_score}-{away_score}"
    elif home_score > away_score:
        score = f"{alert.home} leads {home_score}-{away_score}"
    else:
        score = f"{alert.away} leads {away_score}-{home_score}"
    when = f"Q{period} {clock(clock_seconds)}" if period else "clock unknown"
    possession = s.get("possession")
    ball = ""
    if possession == Side.HOME.value:
        ball = f", {alert.home} ball"
    elif possession == Side.AWAY.value:
        ball = f", {alert.away} ball"
    return f"{score}, {when}{ball}"


def _money_lines(alert: Alert, theta: float) -> list[str]:
    worst = alert.worst_price if alert.worst_price is not None else alert.buy_price
    risk, reward = risk_and_reward(alert.buy_price, theta)
    return [
        f"${alert.dollars_available:,.0f} for sale at {cents(worst)} or better",
        f"Per 100 contracts: risk ${risk:,.0f} to make ${reward:,.2f}",
        f"Break-even: must win {break_even_rate(alert.buy_price, theta) * 100:.1f}% of the time",
    ]


def _theta(alert: Alert) -> float:
    # fee = theta x p x (1 - p); recover theta so the message uses the market's own rate
    p = alert.buy_price
    if 0 < p < 1 and alert.fee > 0:
        return alert.fee / (p * (1 - p))
    from scanner.config import DEFAULT_THETA

    return DEFAULT_THETA


def format_winner(alert: Alert, tz: str) -> str:
    theta = _theta(alert)
    lines = [
        _header(alert),
        _situation_line(alert),
        f"Fair {cents(alert.fair_price)} | Buy {alert.pick} {cents(alert.buy_price)} | "
        f"Edge {cents_1(alert.edge)} after fee",
        *_money_lines(alert, theta),
    ]
    if alert.polymarket_score_differs and alert.polymarket_score:
        lines.append(f"Polymarket scoreboard shows {alert.polymarket_score} (behind)")
    lines.append(f"Checked {local_time(alert.created_at, tz)}")
    return "\n".join(lines)


def format_clinched(alert: Alert, tz: str) -> str:
    theta = _theta(alert)
    s = alert.situation
    away_score, home_score = s.get("away_score"), s.get("home_score")
    line = f"{alert.line:g}" if alert.line is not None else "?"
    combined = alert.combined_score if alert.combined_score is not None else "?"
    stood = s.get("score_age_seconds")
    lines = [
        _header(alert),
        f"OVER {line} is clinched: {away_score}-{home_score} ({combined} points)",
        f"Fair {cents(alert.fair_price)} | Buy Over {cents(alert.buy_price)} | "
        f"Edge {cents_1(alert.edge)} after fee",
        *_money_lines(alert, theta),
    ]
    if stood is not None:
        lines.append(f"Score has stood for {int(stood)} seconds")
    if alert.polymarket_score_differs and alert.polymarket_score:
        lines.append(f"Polymarket scoreboard shows {alert.polymarket_score} (behind)")
    lines.append(f"Checked {local_time(alert.created_at, tz)}")
    return "\n".join(lines)


def format_alert(alert: Alert, tz: str) -> str:
    if alert.alert_type is AlertType.CLINCHED_OVER:
        return format_clinched(alert, tz)
    return format_winner(alert, tz)


def heartbeat_message(games: int) -> str:
    return f"Scanner is up, watching {games} game{'s' if games != 1 else ''}"


def feed_failure_message(feed: str, since: datetime, tz: str) -> str:
    name = "Score feed" if feed == "score" else "Price feed"
    return f"{name} is failing (since {local_time(since, tz)})"


def paused_message(cap: int) -> str:
    return f"Alerts paused for today ({cap} sent, the daily cap)"


def weekly_summary(card: dict) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines = ["Weekly scorecard (paper results at the alerted price, after fees)"]
    for key, title in (("winner", "Winner alerts"), ("clinched_over", "Clinched overs")):
        s = card["by_type"][key]
        leagues = ", ".join(f"{k.upper()} {v}" for k, v in sorted(s["by_league"].items())) or "none"
        lines.append(
            f"{title}: {s['alerts']} ({leagues}); {s['wins']}W {s['losses']}L {s['ties']}T of "
            f"{s['graded']} graded; need {pct(s['win_rate_needed'])}, actual "
            f"{pct(s['actual_win_rate'])}; ${s['profit_per_100']:,.2f} per 100 contracts; "
            f"still there at 30s {pct(s['still_available_at_30s'])}"
        )
    misses = card.get("near_misses") or {}
    if misses:
        top = sorted(misses.items(), key=lambda kv: -kv[1])[:4]
        lines.append("Near misses: " + ", ".join(f"{r} {n}" for r, n in top))
    lines.append("Small sample. Real fills can be worse than the alerted price.")
    return "\n".join(lines)


def game_summary(state: GameState) -> str:
    away = f"{state.away.abbreviation} {state.away_score}"
    home = f"{state.home.abbreviation} {state.home_score}"
    return f"{away} at {home}"


# -- the notifier -------------------------------------------------------------


class Notifier:
    """Decides whether anything leaves the box, and formats what does."""

    def __init__(self, settings: Settings, sender: Sender | None = None) -> None:
        self.settings = settings
        self.sender = sender or build_sender(settings)
        self.sent_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ALERTS_ENABLED)

    def send_alert(self, alert: Alert) -> bool:
        """Format and send one alert. Returns True only when it really went out."""
        alert.message = format_alert(alert, self.settings.TZ)
        return self._send(alert.message)

    def send_system(self, text: str) -> bool:
        return self._send(text)

    def _send(self, text: str) -> bool:
        if not self.enabled:
            log.info("ALERTS_ENABLED is false, not sending:\n%s", text)
            return False
        ok = self.sender.send(text)
        if ok:
            self.sent_count += 1
        return ok

    def send_test_message(self) -> bool:
        """An explicit, by-hand check that the phone is reachable. Ignores the switch."""
        if isinstance(self.sender, DryRunSender):
            log.warning("Telegram is not configured (or DRY_RUN is on); nothing sent")
            return False
        text = (
            "Test message from the Polymarket football scanner. "
            "If you can read this, alerts can reach you. "
            "You can now turn SEND_TEST_MESSAGE_ON_START back off."
        )
        ok = self.sender.send(text)
        log.info("test message %s", "sent" if ok else "FAILED")
        return ok
