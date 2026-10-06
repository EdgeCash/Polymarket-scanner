"""Two read-only pages: ``/health`` and ``/scorecard``. Both need STATUS_TOKEN.

Built for a phone screen: one column, large text, no scripts. Nothing here
can change any setting or send anything.
"""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from scanner import __version__
from scanner.config import Settings
from scanner.diary import Diary

STYLE = """
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 16px;
         max-width: 480px; font-size: 17px; line-height: 1.45; }
  h1 { font-size: 22px; margin: 0 0 12px; }
  h2 { font-size: 18px; margin: 20px 0 8px; }
  table { border-collapse: collapse; width: 100%; }
  td, th { text-align: left; padding: 6px 4px; border-bottom: 1px solid #8884;
           vertical-align: top; }
  td.num { text-align: right; white-space: nowrap; }
  .note { font-size: 14px; opacity: 0.8; }
  .ok { color: #1a7f37; } .bad { color: #b42318; }
  code { font-size: 14px; }
</style>
"""


@dataclass
class RuntimeStatus:
    """What the loop reports about itself. The loop updates it; the page reads it."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    awake: bool = False
    window_note: str = "not started"
    games_watched: int = 0
    live_games: int = 0
    candidates: int = 0
    last_score_poll: dict[str, str] = field(default_factory=dict)
    last_price_poll: str | None = None
    score_feed_failing_since: str | None = None
    price_feed_failing_since: str | None = None
    alerts_today: int = 0
    last_alert_at: str | None = None
    last_error: str | None = None
    leagues: dict[str, str] = field(default_factory=dict)
    unmatched_polymarket: int = 0

    def as_dict(self) -> dict:
        return {
            "version": __version__,
            "started_at": self.started_at.isoformat(),
            "awake": self.awake,
            "window_note": self.window_note,
            "games_watched": self.games_watched,
            "live_games": self.live_games,
            "candidates": self.candidates,
            "last_score_poll": self.last_score_poll,
            "last_price_poll": self.last_price_poll,
            "score_feed_failing_since": self.score_feed_failing_since,
            "price_feed_failing_since": self.price_feed_failing_since,
            "alerts_today": self.alerts_today,
            "last_alert_at": self.last_alert_at,
            "last_error": self.last_error,
            "leagues": self.leagues,
            "unmatched_polymarket": self.unmatched_polymarket,
        }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _local(iso: str | None, tz: str) -> str:
    if not iso:
        return "never"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return html.escape(iso)
    return moment.astimezone(ZoneInfo(tz)).strftime("%a %-I:%M:%S %p")


def render_scorecard(card: dict, settings: Settings) -> str:
    def section(title: str, s: dict) -> str:
        leagues = ", ".join(f"{k.upper()} {v}" for k, v in sorted(s["by_league"].items())) or "none"
        rows = [
            ("Alerts", f"{s['alerts']} ({s['sent']} sent)"),
            ("By league", leagues),
            ("Graded", f"{s['graded']} ({s['not_graded']} not graded)"),
            ("Wins / losses / ties", f"{s['wins']} / {s['losses']} / {s['ties']}"),
            ("Win rate needed", _pct(s["win_rate_needed"])),
            ("Actual win rate", _pct(s["actual_win_rate"])),
            ("Profit per 100 contracts", _money(s["profit_per_100"])),
            (
                "Still available at 30s",
                f"{_pct(s['still_available_at_30s'])} of {s['followups_checked']} checked",
            ),
        ]
        body = "".join(
            f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>"
            for k, v in rows
        )
        return f"<h2>{html.escape(title)}</h2><table>{body}</table>"

    by_type = card.get("near_misses_by_type") or {}
    labelled = [("Winner", r, n) for r, n in by_type.get("winner", {}).items()] + [
        ("Over", r, n) for r, n in by_type.get("clinched_over", {}).items()
    ]
    miss_rows = (
        "".join(
            f"<tr><td>{kind}</td><td>{html.escape(reason)}</td><td class='num'>{n}</td></tr>"
            for kind, reason, n in sorted(labelled, key=lambda row: (-row[2], row[0]))
        )
        or "<tr><td colspan='2'>none</td><td class='num'>0</td></tr>"
    )
    generated = html.escape(_local(card["generated_at"], settings.TZ))
    zone = html.escape(settings.TZ)
    sending = "are" if settings.ALERTS_ENABLED else "are NOT"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanner scorecard</title>{STYLE}</head>
<body>
<h1>Scorecard</h1>
<p class="note">Paper results at the alerted price, after fees. Real trades can fill at
worse prices, and a few weeks is a small sample.</p>
<p>Alerts recorded: <b>{card["alerts_total"]}</b> (sent to the phone: {card["alerts_sent"]})</p>
{section("Winner alerts", card["by_type"]["winner"])}
{section("Clinched-over alerts", card["by_type"]["clinched_over"])}
<h2>Near misses by reason</h2>
<table><tr><th>Type</th><th>Reason</th><th></th></tr>{miss_rows}</table>
<p class="note">Generated {generated} ({zone}). Alerts {sending} being sent.</p>
</body></html>"""


def render_health(status: RuntimeStatus, settings: Settings) -> str:
    d = status.as_dict()
    feeds_ok = not d["score_feed_failing_since"] and not d["price_feed_failing_since"]
    rows = [
        ("Version", d["version"]),
        ("Started", _local(d["started_at"], settings.TZ)),
        ("Game window", ("awake" if d["awake"] else "sleeping") + f": {d['window_note']}"),
        ("Leagues", ", ".join(f"{k.upper()} ({v})" for k, v in d["leagues"].items()) or "none yet"),
        (
            "Games watched",
            f"{d['games_watched']} ({d['live_games']} live, {d['candidates']} candidates)",
        ),
        ("Unmatched Polymarket games", str(d["unmatched_polymarket"])),
        (
            "Last score poll",
            ", ".join(
                f"{k.upper()} {_local(v, settings.TZ)}" for k, v in d["last_score_poll"].items()
            )
            or "never",
        ),
        ("Last price poll", _local(d["last_price_poll"], settings.TZ)),
        ("Alerts today", f"{d['alerts_today']} (last {_local(d['last_alert_at'], settings.TZ)})"),
        ("Alerts enabled", "yes" if settings.ALERTS_ENABLED else "no (shadow mode)"),
        ("Clinched overs", "on" if settings.CLINCHED_OVERS_ENABLED else "off"),
        (
            "Feeds",
            "ok"
            if feeds_ok
            else (
                f"score failing since {d['score_feed_failing_since']}, "
                f"price failing since {d['price_feed_failing_since']}"
            ),
        ),
        ("Last error", d["last_error"] or "none"),
    ]
    body = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
    klass = "ok" if feeds_ok else "bad"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanner status</title>{STYLE}</head>
<body>
<h1>Scanner status: <span class="{klass}">{"ok" if feeds_ok else "feed trouble"}</span></h1>
<table>{body}</table>
<p class="note"><a href="/scorecard?token=">Scorecard</a> (add your token)</p>
</body></html>"""


def create_app(settings: Settings, diary: Diary, status: RuntimeStatus) -> FastAPI:
    app = FastAPI(title="polymarket-scanner", docs_url=None, redoc_url=None, openapi_url=None)

    def authorised(token: str | None, header: str | None) -> bool:
        expected = settings.STATUS_TOKEN.get_secret_value() if settings.STATUS_TOKEN else ""
        if not expected:
            return False
        for candidate in (token, header):
            if candidate and secrets.compare_digest(candidate, expected):
                return True
        return False

    def denied() -> JSONResponse:
        if not settings.STATUS_TOKEN or not settings.STATUS_TOKEN.get_secret_value():
            return JSONResponse({"error": "STATUS_TOKEN is not configured"}, status_code=503)
        return JSONResponse({"error": "token required"}, status_code=401)

    @app.get("/health", response_class=HTMLResponse)
    def health(
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
    ):
        if not authorised(token, x_status_token):
            return denied()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(status.as_dict())
        return HTMLResponse(render_health(status, settings))

    @app.get("/scorecard", response_class=HTMLResponse)
    def scorecard(
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
    ):
        if not authorised(token, x_status_token):
            return denied()
        card = diary.scorecard()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(card)
        return HTMLResponse(render_scorecard(card, settings))

    @app.get("/")
    def root():
        return JSONResponse({"service": "polymarket-scanner", "pages": ["/health", "/scorecard"]})

    return app
