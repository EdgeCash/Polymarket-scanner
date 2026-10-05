from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx

from scanner import __main__ as entry
from scanner.config import load_settings
from scanner.models import Alert, AlertType, League
from scanner.notify import (
    DryRunSender,
    Notifier,
    TelegramSender,
    build_sender,
    cents,
    feed_failure_message,
    format_clinched,
    format_winner,
    heartbeat_message,
    paused_message,
    weekly_summary,
)

CHECKED = datetime(2026, 10, 11, 19, 41, 7, tzinfo=UTC)  # 2:41:07 PM CDT


def brief_winner_alert() -> Alert:
    """The exact example from the brief: DAL at PHI, PHI leads 31-14, Q4 3:52, PHI ball."""
    return Alert(
        alert_type=AlertType.WINNER,
        league=League.NFL,
        created_at=CHECKED,
        feed_id="1",
        event_slug="nfl-dal-phi-2026-10-11",
        market_slug="aec-nfl-dal-phi-2026-10-11",
        home="PHI",
        away="DAL",
        pick="PHI",
        side_label="short",
        fair_price=0.995,
        model_price=0.999,
        espn_price=0.995,
        buy_price=0.93,
        fee=0.0695 * 0.93 * 0.07,
        edge=0.995 - 0.93 - 0.0695 * 0.93 * 0.07,
        dollars_available=180.0,
        average_price=0.93,
        worst_price=0.93,
        situation={
            "home_score": 31,
            "away_score": 14,
            "period": 4,
            "clock_seconds": 232.0,
            "possession": "home",
        },
        polymarket_score="24-14",
        polymarket_score_differs=True,
        pick_side="home",
    )


def brief_clinched_alert() -> Alert:
    return Alert(
        alert_type=AlertType.CLINCHED_OVER,
        league=League.NFL,
        created_at=CHECKED,
        feed_id="1",
        event_slug="nfl-dal-phi-2026-10-11",
        market_slug="tsc-nfl-dal-phi-2026-10-11-total-47pt5",
        home="PHI",
        away="DAL",
        pick="OVER 47.5",
        side_label="long",
        fair_price=0.995,
        model_price=None,
        espn_price=None,
        buy_price=0.96,
        fee=0.0695 * 0.96 * 0.04,
        edge=0.995 - 0.96 - 0.0695 * 0.96 * 0.04,
        dollars_available=120.0,
        average_price=0.96,
        worst_price=0.96,
        situation={"home_score": 20, "away_score": 31, "period": 4, "score_age_seconds": 75},
        polymarket_score="31-20",
        polymarket_score_differs=False,
        line=47.5,
        combined_score=51,
    )


def test_winner_message_matches_the_brief_snapshot():
    expected = (
        "NFL - DAL at PHI\n"
        "PHI leads 31-14, Q4 3:52, PHI ball\n"
        "Fair 99.5c | Buy PHI 93c | Edge 6.0c after fee\n"
        "$180 for sale at 93c or better\n"
        "Per 100 contracts: risk $93 to make $6.55\n"
        "Break-even: must win 93.5% of the time\n"
        "Polymarket scoreboard shows 24-14 (behind)\n"
        "Checked 2:41:07 PM CT"
    )
    assert format_winner(brief_winner_alert(), "America/Chicago") == expected
    assert len(expected.splitlines()) <= 10


def test_clinched_message_matches_the_brief_snapshot():
    expected = (
        "NFL - DAL at PHI\n"
        "OVER 47.5 is clinched: 31-20 (51 points)\n"
        "Fair 99.5c | Buy Over 96c | Edge 3.2c after fee\n"
        "$120 for sale at 96c or better\n"
        "Per 100 contracts: risk $96 to make $3.73\n"
        "Break-even: must win 96.3% of the time\n"
        "Score has stood for 75 seconds\n"
        "Checked 2:41:07 PM CT"
    )
    assert format_clinched(brief_clinched_alert(), "America/Chicago") == expected
    assert len(expected.splitlines()) <= 10


def test_polymarket_line_only_appears_when_the_scores_differ():
    alert = replace(brief_winner_alert(), polymarket_score_differs=False)
    text = format_winner(alert, "America/Chicago")
    assert "Polymarket scoreboard" not in text
    assert len(text.splitlines()) == 7


def test_tied_game_and_unknown_possession_read_sensibly():
    alert = replace(
        brief_winner_alert(),
        situation={"home_score": 14, "away_score": 14, "period": 4, "clock_seconds": 61.0},
    )
    assert "Tied 14-14, Q4 1:01" in format_winner(alert, "America/Chicago").splitlines()[1]
    away_leads = replace(
        brief_winner_alert(),
        situation={
            "home_score": 10,
            "away_score": 17,
            "period": 4,
            "clock_seconds": 5.0,
            "possession": "away",
        },
    )
    assert (
        format_winner(away_leads, "America/Chicago").splitlines()[1]
        == "DAL leads 17-10, Q4 0:05, DAL ball"
    )


def test_college_header_and_other_zones():
    alert = replace(brief_winner_alert(), league=League.CFB, home="ALA", away="UGA", pick="ALA")
    text = format_winner(alert, "America/New_York")
    assert text.startswith("CFB - UGA at ALA\n")
    assert text.endswith("Checked 3:41:07 PM ET")


def test_cents_formatting():
    assert cents(0.93) == "93c" and cents(0.995) == "99.5c" and cents(0.9345) == "93.5c"
    assert cents(1.0) == "100c"


def test_other_messages():
    assert heartbeat_message(3) == "Scanner is up, watching 3 games"
    assert heartbeat_message(1) == "Scanner is up, watching 1 game"
    assert feed_failure_message("score", CHECKED, "America/Chicago") == (
        "Score feed is failing (since 2:41:07 PM CT)"
    )
    assert feed_failure_message("price", CHECKED, "America/Chicago").startswith(
        "Price feed is failing"
    )
    assert paused_message(10) == "Alerts paused for today (10 sent, the daily cap)"


def test_weekly_summary_reads_the_scorecard():
    card = {
        "by_type": {
            "winner": {
                "alerts": 4,
                "by_league": {"nfl": 3, "cfb": 1},
                "wins": 3,
                "losses": 1,
                "ties": 0,
                "graded": 4,
                "win_rate_needed": 0.935,
                "actual_win_rate": 0.75,
                "profit_per_100": -73.5,
                "still_available_at_30s": 0.5,
            },
            "clinched_over": {
                "alerts": 0,
                "by_league": {},
                "wins": 0,
                "losses": 0,
                "ties": 0,
                "graded": 0,
                "win_rate_needed": None,
                "actual_win_rate": None,
                "profit_per_100": 0.0,
                "still_available_at_30s": None,
            },
        },
        "near_misses": {"rule 4: not enough for sale": 7, "rule 6: repeat too soon": 2},
    }
    text = weekly_summary(card)
    assert "Winner alerts: 4 (CFB 1, NFL 3); 3W 1L 0T of 4 graded" in text
    assert "need 93.5%, actual 75.0%; $-73.50 per 100 contracts" in text
    assert "Clinched overs: 0 (none)" in text
    assert "Near misses: rule 4: not enough for sale 7, rule 6: repeat too soon 2" in text


class FakeSender:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def test_nothing_is_sent_when_alerts_are_disabled():
    sender = FakeSender()
    notifier = Notifier(
        load_settings(ALERTS_ENABLED=False, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c"), sender
    )
    alert = brief_winner_alert()
    assert notifier.send_alert(alert) is False
    assert notifier.send_system(heartbeat_message(2)) is False
    assert notifier.send_system(paused_message(10)) is False
    assert sender.messages == []
    assert notifier.sent_count == 0
    # the message text is still produced, so the diary records exactly what would have gone out
    assert alert.message.startswith("NFL - DAL at PHI")


def test_alerts_are_sent_when_enabled():
    sender = FakeSender()
    notifier = Notifier(load_settings(ALERTS_ENABLED=True), sender)
    assert notifier.send_alert(brief_clinched_alert()) is True
    assert notifier.send_system("Scanner is up, watching 2 games") is True
    assert len(sender.messages) == 2 and notifier.sent_count == 2
    assert sender.messages[0].startswith("NFL - DAL at PHI\nOVER 47.5 is clinched")


def test_dry_run_sender_logs_instead_of_sending(caplog):
    caplog.set_level(logging.INFO)
    settings = load_settings(
        ALERTS_ENABLED=True, DRY_RUN=True, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c"
    )
    sender = build_sender(settings)
    assert isinstance(sender, DryRunSender)
    Notifier(settings, sender).send_system("hello phone")
    assert "would send" in caplog.text and "hello phone" in caplog.text
    assert isinstance(build_sender(load_settings(ALERTS_ENABLED=True)), DryRunSender)


def test_telegram_sender_posts_to_the_bot_api_and_hides_the_token(caplog):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = TelegramSender("123:SECRET", "9999", client=client)
    assert sender.send("hi") is True
    assert seen["url"] == "https://api.telegram.org/bot123:SECRET/sendMessage"
    body = json.loads(seen["json"])
    assert body == {"chat_id": "9999", "text": "hi", "disable_web_page_preview": True}

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    caplog.set_level(logging.ERROR)
    bad = TelegramSender(
        "123:SECRET", "9999", client=httpx.Client(transport=httpx.MockTransport(failing))
    )
    assert bad.send("hi") is False
    assert "SECRET" not in caplog.text

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = TelegramSender(
        "123:SECRET", "9999", client=httpx.Client(transport=httpx.MockTransport(boom))
    )
    assert down.send("hi") is False


def test_test_message_command_needs_telegram_configured(monkeypatch):
    assert entry.main(["--send-test-message"]) == 1  # not configured: nothing sent, non-zero exit
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "9")
    monkeypatch.setattr(
        "scanner.notify.TelegramSender.send", lambda self, text: sent.append(text) or True
    )
    assert entry.main(["--send-test-message"]) == 0
    assert sent and sent[0].startswith("Test message from the Polymarket football scanner")


def test_token_and_chat_id_are_cleaned_of_pasted_whitespace():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = TelegramSender("123:SECRET\n", " 9999 \n", client=client)
    assert sender.send("hi") is True
    assert seen["url"] == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert seen["json"]["chat_id"] == "9999"


def test_a_sender_that_raises_never_crashes_the_notifier(caplog):
    class Exploding:
        def send(self, text):
            raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")

    caplog.set_level(logging.ERROR)
    notifier = Notifier(load_settings(ALERTS_ENABLED=True), Exploding())
    assert notifier.send_system("hello") is False
    assert notifier.send_test_message() is False
    assert notifier.send_alert(brief_winner_alert()) is False
    assert "InvalidURL" in caplog.text
    assert "hello" not in caplog.text  # message bodies are not logged on failure


def test_a_token_with_a_newline_cannot_crash_the_real_sender():
    sender = TelegramSender("123:SECRET\n", "9999")
    sender._token = "bad\ntoken"  # bypass the cleaning to prove the catch-all works
    assert sender.send("hi") is False
