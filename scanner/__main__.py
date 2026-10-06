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
    parser.add_argument("--send", action="store_true", help="with --shadow-report: also send it")
    parser.add_argument(
        "--pregame-once",
        action="store_true",
        help="run one pre-game scan across every configured sport, print what it found, exit",
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
