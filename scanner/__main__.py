"""Entry point: ``python -m scanner [--dry-run]``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from scanner import __version__
from scanner.config import load_settings

log = logging.getLogger("scanner")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx logs every request at INFO; the scanner makes thousands a day.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scanner", description="Polymarket football scanner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="load settings, log them with secrets hidden, and exit without scanning",
    )
    parser.add_argument(
        "--once", action="store_true", help="run one scan pass and exit (used for checks)"
    )
    parser.add_argument(
        "--send-test-message",
        action="store_true",
        help="send one test message to the configured Telegram chat and exit",
    )
    parser.add_argument(
        "--shadow-report",
        action="store_true",
        help="print the shadow report from the diary (and send it when --send is given)",
    )
    parser.add_argument(
        "--send", action="store_true", help="with --shadow-report or --monthly-check: send it too"
    )
    parser.add_argument(
        "--gamelog-once",
        action="store_true",
        help="refresh the football game log once (one budget of summaries) and exit",
    )
    parser.add_argument(
        "--pregame-once",
        action="store_true",
        help="run one pre-game scan across every configured sport, print what it found, exit",
    )
    parser.add_argument(
        "--card",
        action="store_true",
        help="print the latest daily card and its record from the diary, and exit",
    )
    parser.add_argument(
        "--monthly-check",
        action="store_true",
        help="print the month's projections against the finals and the closing lines "
        "(last full month, or --month YYYY-MM; sent too when --send is given)",
    )
    parser.add_argument("--month", default=None, help="with --monthly-check: the month, YYYY-MM")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="with --monthly-check: ask the running service at SCANNER_URL to build and send "
        "it (what the monthly cron does, having no diary of its own)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.LOG_LEVEL)
    log.info("scanner %s starting", __version__)
    log.info("settings: %s", json.dumps(settings.redacted(), sort_keys=True, default=str))
    if not settings.ALERTS_ENABLED:
        log.info("ALERTS_ENABLED is false: everything runs, nothing is sent")

    if args.dry_run:
        log.info("dry run complete, exiting")
        return 0

    if args.send_test_message:
        from scanner.notify import Notifier

        return 0 if Notifier(settings).send_test_message() else 1

    if args.shadow_report:
        from datetime import UTC, datetime

        from scanner.diary import Diary
        from scanner.notify import DryRunSender, build_sender
        from scanner.report import shadow_report

        text = shadow_report(Diary(settings.DATABASE_PATH), datetime.now(UTC), settings.TZ)
        print(text)
        if args.send:
            sender = build_sender(settings)
            if isinstance(sender, DryRunSender):
                log.warning("Telegram is not configured; report printed only")
                return 1
            return 0 if sender.send(text) else 1
        return 0

    if args.gamelog_once:
        from scanner.diary import Diary
        from scanner.gamelog import GameLog

        diary = Diary(settings.DATABASE_PATH)
        summaries = GameLog(settings, diary).refresh_all()
        for s in summaries:
            print(
                f"{s.sport}: season {s.season} week {s.week}; {s.games_stored} games stored, "
                f"{s.summaries_fetched} summaries fetched, {s.backlog} still to fetch, "
                f"{s.upcoming} upcoming"
                + (f", {s.days_pending} days still to read" if s.days_pending else "")
                + (f"; errors: {'; '.join(s.errors)}" if s.errors else "")
            )
        print("games in the log:", diary.football_game_counts())
        return 0 if not any(s.errors for s in summaries) else 1

    if args.card:
        from scanner.diary import Diary
        from scanner.report import card_report

        print(card_report(Diary(settings.DATABASE_PATH), settings))
        return 0

    if args.monthly_check and args.remote:
        from scanner.monthly import send_remote

        if not settings.SCANNER_URL or not settings.STATUS_TOKEN:
            log.error("--remote needs SCANNER_URL and STATUS_TOKEN")
            return 1
        import httpx

        try:
            answer = send_remote(
                settings.SCANNER_URL, settings.STATUS_TOKEN.get_secret_value(), args.month
            )
        except httpx.HTTPError as exc:
            log.error(
                "the service at %s did not take the monthly check: %s", settings.SCANNER_URL, exc
            )
            return 1
        print(answer.get("text", ""))
        print(
            f"{answer.get('month')}: {answer.get('games')} graded, "
            f"{'sent' if answer.get('sent') else 'not sent'}"
            + (" (dry run: Telegram not configured there)" if answer.get("dry_run") else "")
        )
        return 0 if answer.get("sent") and not answer.get("dry_run") else 1

    if args.monthly_check:
        from datetime import UTC, datetime

        from scanner.diary import Diary
        from scanner.monthly import month_bounds, report, summary
        from scanner.notify import DryRunSender, build_sender

        start, end, label = month_bounds(args.month, datetime.now(UTC), settings.TZ)
        text = report(summary(Diary(settings.DATABASE_PATH), start, end, label), settings.TZ)
        print(text)
        if args.send:
            sender = build_sender(settings)
            if isinstance(sender, DryRunSender):
                log.warning("Telegram is not configured; check printed only")
                return 1
            return 0 if sender.send(text) else 1
        return 0

    if args.pregame_once:
        from scanner.diary import Diary
        from scanner.pregame import PregameScanner
        from scanner.web import RuntimeStatus

        diary = Diary(settings.DATABASE_PATH)
        summary = PregameScanner(settings, diary, RuntimeStatus()).scan_once()
        print(summary.describe())
        for gap in diary.pregame_gaps(limit=20):
            print(
                f"- {gap['created_at']} {gap['sport'].upper()} {gap['away']} at {gap['home']}: "
                f"{gap['pick']} buy {gap['buy_price'] * 100:.1f}c, book "
                f"{gap['book_fair'] * 100:.1f}% ({gap['book_odds']}), edge "
                f"{gap['edge'] * 100:+.1f}c -> {gap['outcome'] or 'open'}"
            )
        return 0 if not summary.errors else 1

    from scanner.loop import run

    return run(settings, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
