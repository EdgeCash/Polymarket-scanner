"""Read-only pages: ``/health``, ``/scorecard`` and the matchup sheets need STATUS_TOKEN;
the daily card (``/card``) is public.

Built for a phone screen: one column, large text, no scripts. Nothing here
can change any setting or send anything.
"""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Cookie, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from scanner import __version__
from scanner.books import BookLine
from scanner.card import (
    MARKET_MONEYLINE,
    MARKET_SPREAD,
    MARKET_TOTAL,
    card_date_for,
    lock_moment,
)
from scanner.config import Settings
from scanner.diary import Diary
from scanner.gamelog import LOGS, SECTIONS, sheet_from_diary
from scanner.players import SPORT_STATS, chance_over, simulate_player
from scanner.projection import book_margin, tuning_for
from scanner.simulate import chances, ladder, seed_for, simulate, svg_heatmap, svg_histogram
from scanner.stakes import gate

COOKIE = "scanner_token"
COOKIE_MAX_AGE = 365 * 24 * 3600  # a year: the owner signs in once per device
LOCAL_HOSTS = ("testserver", "localhost", "127.0.0.1")

STYLE = """
<style>
  :root { color-scheme: light dark; --bg: #fff; --bg-alt: #f1f1f1; }
  @media (prefers-color-scheme: dark) { :root { --bg: #121212; --bg-alt: #1e1e1e; } }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 16px;
         max-width: 480px; font-size: 17px; line-height: 1.45; background: var(--bg); }
  h1 { font-size: 22px; margin: 0 0 12px; }
  h2 { font-size: 18px; margin: 20px 0 8px; }
  table { border-collapse: collapse; width: 100%; }
  td, th { text-align: left; padding: 6px 4px; border-bottom: 1px solid #8884;
           vertical-align: top; }
  td.num { text-align: right; white-space: nowrap; }
  .note { font-size: 14px; opacity: 0.8; }
  .ok { color: #1a7f37; } .bad { color: #b42318; }
  code { font-size: 14px; }
  body.wide { max-width: 1100px; }
  .wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table.sheet { font-size: 13px; white-space: nowrap; border: 1px solid #8884; }
  table.sheet th, table.sheet td { padding: 6px 7px; text-align: right; }
  table.sheet th, table.sheet td { border-bottom: 1px solid #8883; }
  table.sheet th { font-size: 11px; text-transform: uppercase; opacity: .75; }
  table.sheet th { letter-spacing: .03em; }
  table.sheet th.label, table.sheet td.label { text-align: left; font-weight: 600; }
  table.sheet th.label:first-child, table.sheet td.label:first-child {
    position: sticky; left: 0; background: var(--bg); z-index: 1; }
  table.sheet td.l3 { font-weight: 700; }
  table.sheet th.left, table.sheet td.left { text-align: left; }
  table.sheet th.side { text-align: center; font-size: 13px; opacity: 1; }
  table.sheet tr:nth-child(even) td { background: #8881; }
  table.sheet tr:nth-child(even) td.label:first-child { background: var(--bg-alt); }
  td.when, td.who { white-space: nowrap; }
  td.r1 { color: #1a7f37; font-weight: 700; background: #1a7f3722 !important; }
  td.r2 { color: #b26a00; font-weight: 700; background: #b26a0022 !important; }
  td.r3 { color: #b42318; font-weight: 700; background: #b4231822 !important; }
  .chip { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 12px;
          margin: 3px 3px 0 0; color: #fff; font-weight: 600; }
  .win { background: #1a7f37; } .loss { background: #b42318; }
  .band { background: #0f172a; color: #fff; border-radius: 12px; padding: 14px; }
  .band .note { opacity: .75; }
  .cards { display: flex; flex-wrap: wrap; gap: 12px; }
  .card { flex: 1 1 280px; display: flex; gap: 12px; align-items: flex-start; }
  .card img { width: 64px; height: 64px; object-fit: contain; flex: none; }
  .card h2 { margin: 0; font-size: 22px; line-height: 1.1; }
  .card .ranks span { display: inline-block; margin-right: 10px; }
  .strip { display: flex; flex-wrap: wrap; gap: 16px 22px; font-size: 14px; margin: 12px 0;
           background: #1f6f8b; color: #fff; border-radius: 10px; padding: 12px 14px; }
  .strip div span { display: block; font-size: 11px; opacity: 0.8; text-transform: uppercase; }
  .strip div b { font-size: 16px; }
  .adv { text-align: center; }
  .adv img { width: 22px; height: 22px; vertical-align: middle; }
  .logo-sm { width: 20px; height: 20px; vertical-align: middle; margin-right: 4px; }
  .implied { font-size: 20px; font-weight: 700; }
  .pick { border: 1px solid #8884; border-radius: 12px; padding: 12px 14px; margin: 14px 0; }
  .pick h2 { margin: 6px 0 0; font-size: 20px; }
  .rank { display: inline-block; background: #0f172a; color: #fff; border-radius: 6px;
          padding: 1px 8px; font-weight: 700; margin-right: 6px; }
  .sport { background: #1f6f8b; }
  .call { font-size: 24px; font-weight: 800; margin: 6px 0 2px; }
  .call .odds { font-size: 15px; font-weight: 500; opacity: .75; }
  .push, .not_graded { background: #6b7280; } .open { background: #1f6f8b; }
  .pick svg { display: block; margin-top: 8px; }
  .pick .meta { margin: 2px 0; }
  .scores td { padding: 4px 8px; }
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
    pregame: dict = field(default_factory=dict)  # the pre-game thread's last scan
    gamelog: dict = field(default_factory=dict)  # the football game log's last refresh

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
            "pregame": self.pregame,
            "gamelog": self.gamelog,
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


TYPE_LABELS = {"winner": "Winner", "clinched_over": "Over", "period": "Period"}


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
    labelled = [
        (label, reason, n)
        for kind, label in TYPE_LABELS.items()
        for reason, n in by_type.get(kind, {}).items()
    ]
    miss_rows = (
        "".join(
            f"<tr><td>{kind}</td><td>{html.escape(reason)}</td><td class='num'>{n}</td></tr>"
            for kind, reason, n in sorted(labelled, key=lambda row: (-row[2], row[0]))
        )
        or "<tr><td colspan='2'>none</td><td class='num'>0</td></tr>"
    )
    period_section = ""
    if "period" in card["by_type"]:
        period_section = section(
            "Period markets (quarter and half, decided)", card["by_type"]["period"]
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
{period_section}
{_observation_section(card.get("observations") or {}, settings)}
{_pregame_section(card.get("pregame") or {}, settings)}
{_projection_section(card.get("projections") or {}, settings)}
{_stake_section(card.get("stakes") or {}, settings)}
{_card_section(card.get("card") or {}, settings)}
{_players_section(card.get("players") or {}, settings)}
<h2>Near misses by reason</h2>
<table><tr><th>Type</th><th>Reason</th><th></th></tr>{miss_rows}</table>
<p class="note">Generated {generated} ({zone}). Alerts {sending} being sent.</p>
<p class="note"><a href="/health">Status</a> · <a href="/matchups">Matchups</a></p>
</body></html>"""


def _observation_section(obs: dict, settings: Settings) -> str:
    """The window just outside the late-game filter: checked, graded, never sent."""
    low, high = settings.MAX_MINUTES_LEFT, settings.OBSERVATION_MINUTES_LEFT
    if high <= low:
        return "<h2>Observation window</h2><p class='note'>off</p>"
    title = f"Observation window ({low:g} to {high:g} minutes left, nothing sent)"
    if not obs or not obs.get("rows"):
        return f"<h2>{html.escape(title)}</h2><p class='note'>nothing recorded yet</p>"
    rows = [
        ("Checks", f"{obs['rows']} on {obs['games']} games"),
        ("Would have alerted", f"{obs['picks']} picks ({obs['would_alert_rows']} checks)"),
        ("Graded", str(obs["graded"])),
        ("Wins / losses / ties", f"{obs['wins']} / {obs['losses']} / {obs['ties']}"),
        ("Win rate needed", _pct(obs["win_rate_needed"])),
        ("Actual win rate", _pct(obs["actual_win_rate"])),
        ("Profit per 100 contracts", _money(obs["profit_per_100"])),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>" for k, v in rows
    )
    reasons = "".join(
        f"<tr><td>{html.escape(reason)}</td><td class='num'>{n}</td></tr>"
        for reason, n in (obs.get("reasons") or {}).items()
    )
    if reasons:
        reasons = f"<table><tr><th>Why not</th><th></th></tr>{reasons}</table>"
    return f"<h2>{html.escape(title)}</h2><table>{body}</table>{reasons}"


def _signed_cents(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}c"


def _pregame_section(pg: dict, settings: Settings) -> str:
    """Every sport's pre-game gaps against the book: recorded, graded, never sent."""
    if not settings.PREGAME_ENABLED:
        return "<h2>Pre-game gaps</h2><p class='note'>off</p>"
    title = f"Pre-game gaps (every sport, {settings.PREGAME_MIN_EDGE * 100:g}c edge, nothing sent)"
    if not pg or not pg.get("gaps"):
        return f"<h2>{html.escape(title)}</h2><p class='note'>no gaps recorded yet</p>"
    sports = ", ".join(f"{k.upper()} {v}" for k, v in sorted(pg["by_sport"].items())) or "none"
    markets = ", ".join(f"{k} {v}" for k, v in sorted(pg["by_market"].items())) or "none"
    rows = [
        ("Gaps", f"{pg['gaps']} ({sports})"),
        ("By market", markets),
        ("Graded", f"{pg['graded']} ({pg['not_graded']} not graded)"),
        ("Wins / losses / pushes", f"{pg['wins']} / {pg['losses']} / {pg['pushes']}"),
        ("Win rate needed", _pct(pg["win_rate_needed"])),
        ("Actual win rate", _pct(pg["actual_win_rate"])),
        ("Profit per 100 contracts", _money(pg["profit_per_100"])),
        ("Avg edge when seen", _signed_cents(pg["avg_edge"])),
        ("Avg edge at the close", f"{_signed_cents(pg['avg_clv'])} ({pg['closed']} closed)"),
        ("Beat the closing line", _pct(pg["positive_clv_share"])),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>" for k, v in rows
    )
    recent = "".join(
        "<tr><td>{when}<br>{game}</td><td>{pick}</td><td class='num'>{buy}<br>{book}</td>"
        "<td class='num'>{edge}<br>{clv}</td><td>{result}</td></tr>".format(
            when=html.escape(_local(r["created_at"], settings.TZ)),
            game=html.escape(f"{r['sport'].upper()} {r['away']} at {r['home']}"),
            pick=html.escape(r["pick"]),
            buy=html.escape(f"{r['buy_price'] * 100:.1f}c"),
            book=html.escape(f"book {r['book_fair'] * 100:.1f}%"),
            edge=html.escape(_signed_cents(r["edge"])),
            clv=html.escape("close " + _signed_cents(r["clv"])),
            result=html.escape(r["outcome"] or "open"),
        )
        for r in pg.get("recent") or []
    )
    table = (
        "<table><tr><th>Game</th><th>Pick</th><th>Buy / book</th><th>Edge / close</th>"
        f"<th>Result</th></tr>{recent}</table>"
        if recent
        else ""
    )
    note = (
        "<p class='note'>Edge is the book's vig-free probability minus the Polymarket buy "
        "price and fee. Close is the same edge against the book's last line before the "
        "start: positive means the market moved our way.</p>"
    )
    return f"<h2>{html.escape(title)}</h2><table>{body}</table>{table}{note}"


def _signed(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "–"
    return f"{round(value, decimals) + 0.0:+.{decimals}f}"  # + 0.0 turns -0.0 into +0.0


def _record(r: dict) -> str:
    played = r["wins"] + r["losses"]
    share = f" ({r['wins'] / played * 100:.0f}%)" if played else ""
    return f"{r['wins']}-{r['losses']}-{r['pushes']}{share}"


def _projection_rows(part: dict, sport: str) -> list[tuple[str, str]]:
    """One sport's model record, in its own unit (points, or goals in hockey)."""

    def trio(block: dict, decimals: int = 1) -> str:
        return " / ".join(_fmt(block.get(k), decimals) for k in ("raw", "blend", "book"))

    unit = LOGS[sport].unit if sport in LOGS else "points"
    line = "margin" if tuning_for(sport).margin_from_moneyline else "spread"
    rows = [
        ("Graded", str(part["graded"])),
        ("Avg margin miss: model / blend / book", trio(part["margin_error"])),
        ("Avg total miss: model / blend / book", trio(part["total_error"])),
        ("Brier score: model / blend / book", trio(part["brier"], 3)),
    ]
    for bucket in part.get("buckets") or ():
        rows.append(
            (
                f"{line.capitalize()} record, model {bucket}+ {unit} off the book",
                _record(part["ats"][bucket]),
            )
        )
    for bucket in part.get("buckets") or ():
        rows.append(
            (f"Total record, model {bucket}+ {unit} off the book", _record(part["ou"][bucket]))
        )
    clv = part["clv"]
    moved = (
        f"{_signed(clv['margin'], 2)} {unit} avg, {_pct(clv['positive_share'])} of "
        f"{clv['closed']} closed"
    )
    rows.append(("Line moved the model's way", moved))
    return rows


def _projection_section(pj: dict, settings: Settings) -> str:
    """How the model has done against the final scores and the book, sport by sport."""
    title = "Projection model (graded against the book, nothing sent)"
    if not settings.PROJECTION_ENABLED:
        return "<h2>Projection model</h2><p class='note'>off</p>"
    if not pj or not (pj.get("graded") or pj.get("open")):
        return f"<h2>{html.escape(title)}</h2><p class='note'>no projections yet</p>"

    sports = ", ".join(f"{k.upper()} {v}" for k, v in sorted(pj.get("by_sport", {}).items()))
    body = (
        f"<tr><th>Graded</th><td class='num'>{pj['graded']} ({html.escape(sports or 'none')}), "
        f"{pj['open']} open</td></tr>"
    )
    for sport, part in (pj.get("sports") or {}).items():
        body += f"<tr><th colspan='2'>{html.escape(sport.upper())}</th></tr>"
        body += "".join(
            f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>"
            for k, v in _projection_rows(part, sport)
        )
    recent = "".join(
        "<tr><td>{when}<br>{game}</td><td class='num'>{model}<br>{book}</td>"
        "<td class='num'>{final}</td><td>{ats}</td><td>{ou}</td></tr>".format(
            when=html.escape(_local(r["kickoff"], settings.TZ)),
            game=html.escape(f"{r['sport'].upper()} {r['away']} at {r['home']}"),
            model=html.escape(f"{r['home']} {-r['raw_margin']:+.1f} / {r['raw_total']:.1f}"),
            book=html.escape(
                f"book {r['home']} {-r['book_margin']:+.1f} / {r['book_total']:.1f}"
                if r["book_margin"] is not None and r["book_total"] is not None
                else "no book line"
            ),
            final=html.escape(f"{r['final']['away']}-{r['final']['home']}"),
            ats=html.escape(f"{r['ats']['side']} {r['ats']['result']}" if r["ats"] else "–"),
            ou=html.escape(f"{r['ou']['side']} {r['ou']['result']}" if r["ou"] else "–"),
        )
        for r in pj.get("recent") or []
    )
    table = (
        "<table><tr><th>Game</th><th>Model / book</th><th>Final</th><th>Spread</th>"
        f"<th>Total</th></tr>{recent}</table>"
        if recent
        else ""
    )
    note = (
        "<p class='note'>The model is half points and half what the yards (football) or "
        "shots (hockey) say, points alone in basketball, each adjusted for who was played "
        "and shrunk toward average, plus home advantage; the blend leans on the book while "
        "games are few. Miss is the average distance from the final margin or total, lower "
        "is better, and the book is the bar to beat. Brier scores the win probability, lower "
        "is better. The spread and total records count the side the model took against the "
        "book's line (in hockey, the margin its moneyline implies) when it sat at least that "
        "far from it. Nothing here is sent or placed.</p>"
    )
    return f"<h2>{html.escape(title)}</h2><table>{body}</table>{table}{note}"


def _stake_section(st: dict, settings: Settings) -> str:
    """What the model would have staked, and how that paper money did."""
    title = "Stake suggestions (shadow until the gate opens, nothing placed)"
    if not settings.STAKE_ENABLED or not settings.PROJECTION_ENABLED:
        return "<h2>Stake suggestions</h2><p class='note'>off</p>"
    if not st or not st.get("suggested"):
        return f"<h2>{html.escape(title)}</h2><p class='note'>no suggestions yet</p>"
    markets = ", ".join(
        f"{k} {v['suggested']}" for k, v in st["by_market"].items() if v["suggested"]
    )
    rows = [
        ("Suggested", f"{st['suggested']} ({st['open']} open, {st['shown']} shown on a sheet)"),
        ("By market", markets or "none"),
        ("Graded", f"{st['graded']} ({st['not_graded']} not graded)"),
        ("Wins / losses / pushes", f"{st['wins']} / {st['losses']} / {st['pushes']}"),
        ("Staked (paper)", _money(st["staked"])),
        ("Profit (paper, after fees)", _money(st["profit"])),
        ("Return on stake", _pct(st["roi"])),
        ("Avg edge when suggested", _signed_cents(st["avg_edge"])),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>" for k, v in rows
    )
    recent = "".join(
        "<tr><td>{when}<br>{game}</td><td>{side}<br><span class='note'>{market}</span></td>"
        "<td class='num'>{buy}<br>{model}</td><td class='num'>{edge}<br>{stake}</td>"
        "<td>{result}</td></tr>".format(
            when=html.escape(_local(r["kickoff"], settings.TZ)),
            game=html.escape(f"{r['sport'].upper()} {r['away']} at {r['home']}"),
            side=html.escape(r["side_label"]),
            market=html.escape(r["market"] + ("" if r["gate_open"] else ", shadow")),
            buy=html.escape(f"{r['buy_price'] * 100:.1f}c"),
            model=html.escape(f"model {r['model_prob'] * 100:.1f}%"),
            edge=html.escape(_signed_cents(r["edge"])),
            stake=html.escape(f"${r['stake']:.0f}"),
            result=html.escape(
                f"{r['outcome']} {_money(r['profit'])}"
                if r["outcome"] in ("win", "loss", "push")
                else (r["outcome"] or "open")
            ),
        )
        for r in st.get("recent") or []
    )
    table = (
        "<table><tr><th>Game</th><th>Side</th><th>Price / model</th><th>Edge / stake</th>"
        f"<th>Result</th></tr>{recent}</table>"
        if recent
        else ""
    )
    note = (
        "<p class='note'>A stake is a quarter-Kelly share of the bankroll on the blended "
        "projection's probability against the last Polymarket price seen, after the fee, "
        f"when the edge clears {settings.STAKE_MIN_EDGE * 100:g}c. Rows are recorded whether "
        "or not the sheet showed them; shadow rows are the ones it did not. Profit is at the "
        "suggested stake, after fees. Nothing is placed.</p>"
    )
    return f"<h2>{html.escape(title)}</h2><table>{body}</table>{table}{note}"


def _units(value: float | None) -> str:
    return "–" if value is None else f"{value:+.2f}u"


def _record_line(t: dict) -> str:
    """W-L-P with the units beside it, for the card's tables."""
    if not t.get("graded"):
        return "–"
    rate = f" ({t['win_rate'] * 100:.0f}%)" if t.get("win_rate") is not None else ""
    return f"{t['wins']}-{t['losses']}-{t['pushes']}{rate}, {_units(t['units'])}"


def _card_section(cd: dict, settings: Settings) -> str:
    """The daily card's whole record: overall, rank by rank, and the last picks graded."""
    title = "Daily card (chosen by rule, graded at the final, nothing sent)"
    if not settings.CARD_ENABLED or not settings.PROJECTION_ENABLED:
        return "<h2>Daily card</h2><p class='note'>off</p>"
    if not cd or not cd.get("picks"):
        return f"<h2>{html.escape(title)}</h2><p class='note'>no card yet</p>"
    clv = (
        f"{_signed(cd['clv'], 2)} avg, {_pct(cd['clv_positive_share'])} toward the pick"
        if cd.get("clv") is not None
        else "–"
    )
    rows = [
        ("Days", str(cd["days"])),
        ("Picks", f"{cd['picks']} ({cd['open']} open)"),
        ("Graded", f"{cd['graded']} ({cd['not_graded']} not graded)"),
        ("Wins / losses / pushes", f"{cd['wins']} / {cd['losses']} / {cd['pushes']}"),
        ("Win rate", _pct(cd["win_rate"])),
        ("Units (one unit a pick)", _units(cd["units"])),
        ("Return per pick", _pct(cd["roi"])),
        ("Closing line", clv),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>" for k, v in rows
    )
    by_rank = "".join(
        f"<tr><th>Rank {html.escape(rank)}</th><td class='num'>{html.escape(_record_line(t))}</td>"
        "</tr>"
        for rank, t in cd["by_rank"].items()
    )
    by_sport = "".join(
        f"<tr><th>{html.escape(s.upper())}</th><td class='num'>{html.escape(_record_line(t))}</td>"
        "</tr>"
        for s, t in cd["by_sport"].items()
    )
    by_market = "".join(
        f"<tr><th>{html.escape(m.capitalize())}</th><td class='num'>"
        f"{html.escape(_record_line(t))}</td></tr>"
        for m, t in cd["by_market"].items()
    )
    recent = "".join(
        "<tr><td>{day} #{rank}<br>{game}</td><td>{side}</td><td class='num'>{final}</td>"
        "<td>{result}</td></tr>".format(
            day=html.escape(r["card_date"]),
            rank=r["rank"],
            game=html.escape(f"{r['sport'].upper()} {r['away']} at {r['home']}"),
            side=html.escape(f"{r['side_label']} ({_odds(r['odds'])})"),
            final=html.escape(f"{r['final_away']}-{r['final_home']}"),
            result=html.escape(f"{r['outcome']} {_units(r['units'])}"),
        )
        for r in cd.get("recent") or []
    )
    table = (
        f"<table><tr><th>Pick</th><th>Side</th><th>Final</th><th>Result</th></tr>{recent}</table>"
        if recent
        else ""
    )
    note = (
        f"<p class='note'>Each day at {settings.CARD_LOCK_TIME} {html.escape(settings.CARD_TZ)} "
        f"the {settings.CARD_PICKS} sides the model is surest of against the book are locked: "
        "one per game, ranked by the gap from the book in that sport's spread of results, "
        "weighted by how often the sport's closing line has moved the model's way. Units are "
        "at one unit a pick at the book's price. The record by rank is the point: it says "
        "whether the top of the card deserves more trust than the bottom. "
        "<a href='/card'>The public card page</a>. Nothing is sent or placed.</p>"
    )
    return (
        f"<h2>{html.escape(title)}</h2><table>{body}</table>"
        f"<table>{by_rank}{by_sport}{by_market}</table>{table}{note}"
    )


STAT_LABELS = {
    "points": "Points",
    "rebounds": "Rebounds",
    "assists": "Assists",
    "three_made": "Threes",
    "shots": "Shots on goal",
    "goals": "Goals",
    "saves": "Saves",
    "pass_yards": "Passing yards",
    "rush_yards": "Rushing yards",
    "rec_yards": "Receiving yards",
    "receptions": "Receptions",
    "pass_td": "Passing TDs",
}


def _players_section(ps: dict, settings: Settings) -> str:
    """How the player lines have done: the miss, the bias and whether the bands hold."""
    title = "Player lines (every leader's stat line, graded at the final, nothing sent)"
    if not settings.PLAYERS_ENABLED or not settings.PROJECTION_ENABLED:
        return "<h2>Player lines</h2><p class='note'>off</p>"
    if not ps or not (ps.get("projected") or ps.get("open")):
        return f"<h2>{html.escape(title)}</h2><p class='note'>no player lines yet</p>"
    rows = [
        ("Lines", f"{ps['projected']} graded or settled, {ps['open']} open"),
        ("Did not play", str(ps["did_not_play"])),
    ]
    if ps.get("graded"):
        rows += [
            ("Graded", str(ps["graded"])),
            ("Avg miss", f"{ps['mae']:.2f}"),
            ("Bias (actual minus projected)", _signed(ps["bias"], 2)),
            ("Actual above the median", _pct(ps["over_median"])),
            ("Inside the 80% band", _pct(ps["in_band"])),
        ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td class='num'>{html.escape(v)}</td></tr>" for k, v in rows
    )
    by_sport = ""
    for sport, part in (ps.get("by_sport") or {}).items():
        by_sport += f"<tr><th colspan='2'>{html.escape(sport.upper())} ({part['graded']})</th></tr>"
        for stat, tally in (part.get("by_stat") or {}).items():
            text = (
                f"{tally['graded']} graded, miss {tally['mae']:.1f}, bias "
                f"{_signed(tally['bias'], 1)}, over median {_pct(tally['over_median'])}, "
                f"in band {_pct(tally['in_band'])}"
            )
            by_sport += (
                f"<tr><th>{html.escape(STAT_LABELS.get(stat, stat))}</th>"
                f"<td class='num'>{html.escape(text)}</td></tr>"
            )
    recent = "".join(
        "<tr><td>{when}<br>{who}</td><td>{stat}</td><td class='num'>{line}</td>"
        "<td class='num'>{actual}</td></tr>".format(
            when=html.escape(_local(r["kickoff"], settings.TZ)),
            who=html.escape(f"{r['name']} ({r['team_abbr']} v {r['opponent_abbr']})"),
            stat=html.escape(STAT_LABELS.get(r["stat"], r["stat"])),
            line=html.escape(f"{r['median']} ({r['p10']}–{r['p90']})"),
            actual=html.escape(str(r["actual"])),
        )
        for r in ps.get("recent") or []
    )
    table = (
        "<table><tr><th>Player</th><th>Stat</th><th>Line (80% band)</th><th>Actual</th></tr>"
        f"{recent}</table>"
        if recent
        else ""
    )
    note = (
        "<p class='note'>A line is the player's rate per minute (per second on the ice; per "
        "game in football) shrunk toward his position's league rate, times his projected "
        "playing time from his last five games, scaled by how the game projection sees his "
        "team's scoring and the pace. The spread is a negative binomial mixed over his "
        "minutes. Only players who played their team's last game are projected. Actual above "
        "the median should sit near 50% and inside the band near 80% if the spread is honest; "
        "the miss is the average distance from the projected mean. Nothing is sent or placed.</p>"
    )
    return (
        f"<h2>{html.escape(title)}</h2><table>{body}</table><table>{by_sport}</table>{table}{note}"
    )


def _card_health(status: RuntimeStatus, settings: Settings) -> str:
    if not settings.CARD_ENABLED or not settings.PROJECTION_ENABLED:
        return "off"
    built = (status.gamelog or {}).get("card")
    if not built:
        return f"none locked since start; locks at {settings.CARD_LOCK_TIME} {settings.CARD_TZ}"
    late = ", late" if built.get("late") else ""
    return (
        f"{built['card_date']}: {built['picks']} picks locked "
        f"{_local(built.get('locked_at'), settings.TZ)}{late}"
    )


def _pregame_health(status: RuntimeStatus, settings: Settings) -> str:
    if not settings.PREGAME_ENABLED:
        return "off"
    pg = status.pregame or {}
    if not pg.get("last_scan"):
        return "not run yet"
    text = (
        f"last {_local(pg.get('last_scan'), settings.TZ)}: {pg.get('matched', 0)} games matched, "
        f"{pg.get('unmatched', 0)} unmatched, {pg.get('open_gaps', 0)} open gaps"
    )
    if pg.get("error"):
        text += f"; error: {pg['error']}"
    return text


def _gamelog_health(status: RuntimeStatus, settings: Settings) -> str:
    if not settings.GAMELOG_ENABLED:
        return "off"
    gl = status.gamelog or {}
    if not gl.get("last_refresh"):
        return "not run yet"
    games = (
        ", ".join(f"{k.upper()} {v}" for k, v in sorted((gl.get("games") or {}).items())) or "none"
    )
    text = f"last {_local(gl.get('last_refresh'), settings.TZ)}: {games} games"
    if gl.get("backlog"):
        text += f", {gl['backlog']} still to fetch"
    if gl.get("stale"):
        text += f", {gl['stale']} to re-read"
    if gl.get("days_pending"):
        text += f", {gl['days_pending']} days still to read"
    if gl.get("player_games") is not None:
        with_players = sum(gl["player_games"].values())
        games_total = sum((gl.get("games") or {}).values())
        if with_players < games_total:
            text += f", {with_players} with player lines"
    if gl.get("projected"):
        text += f", {gl['projected']} sheets projected"
    if gl.get("staked"):
        text += f", {gl['staked']} stakes open"
    if gl.get("player_lines"):
        text += f", {gl['player_lines']} player lines"
    if gl.get("error"):
        text += f"; error: {gl['error']}"
    return text


# -- matchup sheets ------------------------------------------------------------------


def _fmt(value: float | None, decimals: int = 1, percent: bool = False) -> str:
    if value is None:
        return "–"
    text = f"{value:.{decimals}f}"
    return text + "%" if percent else text


def _rank_cell(rank: int | None, league_size: int) -> str:
    if rank is None or league_size <= 0:
        return "<td>–</td>"
    third = league_size / 3.0
    klass = "r1" if rank <= third else ("r2" if rank <= 2 * third else "r3")
    return f"<td class='{klass}'>{rank}</td>"


def _odds(value: int | None) -> str:
    return "–" if value is None else (f"+{value}" if value > 0 else str(value))


def _kickoff(moment, tz: str) -> str:
    if moment is None:
        return "time unknown"
    return moment.astimezone(ZoneInfo(tz)).strftime("%a %b %-d, %-I:%M %p") + " " + _zone_label(tz)


def _zone_label(tz: str) -> str:
    from scanner.notify import ZONE_LABELS

    return ZONE_LABELS.get(tz) or tz


def _ordinal(n: int | None) -> str:
    if n is None:
        return "–"
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _logo(team: dict, klass: str = "") -> str:
    logo = team.get("logo")
    if not logo:
        return ""
    alt = html.escape(team.get("abbreviation") or "")
    klass_attr = f" class='{klass}'" if klass else ""
    return f"<img{klass_attr} src='{html.escape(logo)}' alt='{alt}'>"


def _team_card(team: dict, label: str, league_size: int) -> str:
    record = team.get("record") or {}
    chips = []
    for g in team.get("last5") or []:
        klass = "win" if g["won"] else "loss"
        where = f"{g['at']} " if g["at"] else ""
        chips.append(
            f"<span class='chip {klass}'>{html.escape(where + g['opponent'])} "
            f"{html.escape(g['score'])}</span>"
        )
    chip_html = "".join(chips) or "<span class='note'>no games in the log yet</span>"
    bits = []
    if record.get("total"):
        bits.append(f"{record['total']}")
    if record.get("home"):
        bits.append(f"{record['home']} home")
    if record.get("road"):
        bits.append(f"{record['road']} road")
    if team.get("streak"):
        bits.append(f"streak {team['streak']}")
    if team.get("rest_days") is not None:
        bits.append(f"{team['rest_days']} days rest")
    ranks = team.get("summary_ranks") or {}
    rank_html = ""
    if any(v is not None for v in ranks.values()):
        rank_html = (
            "<div class='ranks note'>"
            f"<span>Off {_ordinal(ranks.get('offense'))}</span>"
            f"<span>Def {_ordinal(ranks.get('defense'))}</span>"
            f"<span>Overall {_ordinal(ranks.get('overall'))} of {league_size}</span></div>"
        )
    poll = f" <span class='note'>#{team['poll_rank']}</span>" if team.get("poll_rank") else ""
    return (
        f"<div class='card'>{_logo(team)}<div><span class='note'>{html.escape(label)}</span>"
        f"<h2>{html.escape(team['name'])}{poll}</h2>"
        f"<div class='note'>{html.escape(' · '.join(bits))}</div>{rank_html}"
        f"<div>{chip_html}</div></div></div>"
    )


def _cents(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.1f}c"


def _strip(sheet: dict, settings: Settings) -> str:
    home, away = sheet["home"]["abbreviation"], sheet["away"]["abbreviation"]
    profile = LOGS[sheet["sport"]]
    items: list[tuple[str, str]] = [
        (profile.start_label, _kickoff(sheet.get("kickoff"), settings.TZ))
    ]
    venue = sheet.get("venue") or {}
    place = venue.get("name") or ""
    if venue.get("city"):
        place += f", {venue['city']}" + (f", {venue['state']}" if venue.get("state") else "")
    if sheet.get("neutral"):
        place += " (neutral site)"
    if place:
        items.append(("Venue", place))
    surface = []
    if venue.get("indoor"):
        surface.append("indoor")
    if venue.get("grass") is True:
        surface.append("grass")
    elif venue.get("grass") is False:
        surface.append("turf")
    if surface and profile.outdoors:
        items.append(("Surface", ", ".join(surface)))
    weather = sheet.get("weather") or {}
    if weather.get("temperature") is not None:
        text = f"{weather['temperature']}°F"
        if weather.get("precipitation") is not None:
            text += f", {weather['precipitation']}% rain"
        if weather.get("gust") is not None:
            text += f", gusts {weather['gust']} mph"
        items.append(("Weather", text))
    book: BookLine | None = sheet.get("book")
    if book is not None:
        parts = []
        if book.home_spread is not None:
            parts.append(f"{home} {book.home_spread:+g}")
        if book.home_ml is not None and book.away_ml is not None:
            parts.append(f"ML {home} {_odds(book.home_ml)} / {away} {_odds(book.away_ml)}")
        if book.total is not None:
            parts.append(f"total {book.total:g}")
        if parts:
            items.append((book.provider or "Book", ", ".join(parts)))
    implied = sheet.get("implied") or {}
    if implied.get("home") is not None:
        items.append(
            ("Market implied score", f"{away} {implied['away']:.1f} – {home} {implied['home']:.1f}")
        )
    win = sheet.get("win_probability") or {}
    if win.get("home") is not None:
        items.append(
            (
                "Book win probability",
                f"{home} {win['home'] * 100:.0f}% / {away} {win['away'] * 100:.0f}%",
            )
        )
    pm = sheet.get("polymarket") or {}
    if pm.get("home") is not None or pm.get("away") is not None:
        items.append(
            (
                "Polymarket to win",
                f"{home} {_cents(pm.get('home'))} / {away} {_cents(pm.get('away'))}",
            )
        )
    predictor = sheet.get("predictor") or {}
    if predictor.get("home") is not None:
        text = f"{home} {predictor['home']:.1f}% / {away} {predictor['away']:.1f}%"
        items.append(("ESPN projection", text))
    cells = "".join(
        f"<div><span>{html.escape(k)}</span><b>{html.escape(v)}</b></div>" for k, v in items
    )
    return f"<div class='strip'>{cells}</div>"


def _projection_block(sheet: dict, settings: Settings) -> str:
    """The model's numbers beside the book's and Polymarket's, and why they differ."""
    if not settings.PROJECTION_ENABLED:
        return ""
    home, away = sheet["home"]["abbreviation"], sheet["away"]["abbreviation"]
    proj = sheet.get("projection")
    if not proj:
        return (
            "<h2>Projection</h2><p class='note'>no projection yet: the model needs both "
            "teams in the game log.</p>"
        )
    raw, blend, book = proj["raw"], proj["blend"], proj.get("book") or {}
    implied = sheet.get("implied") or {}
    pm = sheet.get("polymarket") or {}
    profile = LOGS[sheet["sport"]]
    line_label = "Margin" if tuning_for(sheet["sport"]).margin_from_moneyline else "Spread"

    def pts(value: float | None) -> str:
        return "–" if value is None else f"{value:.1f}"

    def spread(margin: float | None) -> str:
        return "–" if margin is None else f"{-margin:+.1f}"

    def pct(value: float | None) -> str:
        return "–" if value is None else f"{value * 100:.0f}%"

    rows = [
        (f"{away} (away)", pts(raw["away"]), pts(blend["away"]), pts(implied.get("away")), "–"),
        (f"{home} (home)", pts(raw["home"]), pts(blend["home"]), pts(implied.get("home")), "–"),
        (
            f"{line_label} ({home})",
            spread(raw["margin"]),
            spread(blend["margin"]),
            spread(book.get("margin")),
            "–",
        ),
        ("Total", pts(raw["total"]), pts(blend["total"]), pts(book.get("total")), "–"),
        (
            f"{home} to win",
            pct(raw["home_win"]),
            pct(blend["home_win"]),
            pct(book.get("home_win")),
            _cents(pm.get("home")),
        ),
    ]
    body = "".join(
        f"<tr><td class='label'>{html.escape(label)}</td>"
        + "".join(f"<td>{html.escape(cell)}</td>" for cell in cells)
        + "</tr>"
        for label, *cells in rows
    )
    half = raw.get("first_half") or {}
    body += (
        f"<tr><td class='label'>{html.escape(profile.half_label)}</td><td colspan='4' "
        f"class='left'>{html.escape(away)} {pts(half.get('away'))} – {html.escape(home)} "
        f"{pts(half.get('home'))}</td></tr>"
    )
    head = (
        "<tr><th class='label'></th><th>Model</th><th>Blend</th><th>Book</th>"
        "<th>Polymarket</th></tr>"
    )
    gap = proj.get("gap") or {}
    bits = []
    if gap.get("margin") is not None:
        side = home if gap["margin"] > 0 else away
        bits.append(f"{side} by {abs(gap['margin']):.1f} {profile.unit} more than the book")
    if gap.get("total") is not None:
        bits.append(f"total {abs(gap['total']):.1f} {'higher' if gap['total'] > 0 else 'lower'}")
    if gap.get("home_win") is not None:
        side = home if gap["home_win"] > 0 else away
        bits.append(f"{side} to win {abs(gap['home_win']) * 100:.0f} points likelier")
    gap_text = f"Model vs book: {', '.join(bits)}." if bits else "No book line to compare."
    r, lg, used = proj["ratings"], proj["league"], proj["games_used"]
    site = " (neutral site, none applied)" if proj.get("neutral") else ""
    inputs = (
        f"Ratings in {profile.unit} against an average side (defence below zero allows fewer): "
        f"{home} offence {_signed(r['home']['offense'])}, defence {_signed(r['home']['defense'])}; "
        f"{away} offence {_signed(r['away']['offense'])}, defence {_signed(r['away']['defense'])}. "
        f"League {lg['points']:.1f} a side, {profile.home_label} {lg['hfa']:g}{site}; "
        f"{used['home']} and {used['away']} games used, blend {blend['weight'] * 100:.0f}% model."
    )
    thin = " <b>Thin: fewer than 3 games for a side.</b>" if proj.get("thin") else ""
    return (
        "<h2>Projection</h2><div class='wrap'>"
        f"<table class='sheet'>{head}{body}</table></div>"
        f"<p class='note'>{html.escape(gap_text)} {html.escape(inputs)}{thin}</p>"
    )


def _stake_block(sheet: dict, settings: Settings) -> str:
    """What the model would stake, shown only once its record in the sport has earned it."""
    if not settings.STAKE_ENABLED or not settings.PROJECTION_ENABLED:
        return ""
    status = gate(sheet.get("model_record"), settings)
    sport = html.escape(sheet["sport"].upper())
    if not status["open"]:
        clv = status["clv"]
        moved = (
            "no closing lines graded yet"
            if clv is None
            else f"the line has moved the model's way {clv:+.1f} points on average"
        )
        return (
            f"<h2>Stake</h2><p class='note'>Hidden until the model has earned it: "
            f"{status['graded']} of {status['needed']} {sport} games graded, {moved}. "
            "Shadow suggestions are being recorded and graded on the scorecard.</p>"
        )
    rows = sheet.get("stakes") or []
    if not rows:
        return (
            f"<h2>Stake</h2><p class='note'>No edge of {settings.STAKE_MIN_EDGE * 100:g}c or more "
            "after fees at the last Polymarket prices seen.</p>"
        )
    body = "".join(
        f"<tr><td class='label'>{html.escape(r['side_label'])}<br>"
        f"<span class='note'>{html.escape(r['market'])}</span></td>"
        f"<td class='l3'>${r['stake']:.0f}</td>"
        f"<td>{r['buy_price'] * 100:.1f}c</td><td>{r['model_prob'] * 100:.1f}%</td>"
        f"<td>{html.escape(_signed_cents(r['edge']))}</td></tr>"
        for r in rows
    )
    head = (
        "<tr><th class='label'>Side</th><th>Stake</th><th>Price</th>"
        "<th>Model</th><th>Edge</th></tr>"
    )
    seen = rows[0].get("price_at")
    when = f" ({_local(seen, settings.TZ)})" if seen else ""
    note = (
        "Quarter-Kelly of the blended probability against the last Polymarket price the "
        f"pre-game scan saw{when}, after the fee, capped at {settings.STAKE_MAX_SHARE * 100:g}% "
        f"of a ${settings.BANKROLL:,.0f} bankroll. A suggestion graded on the scorecard, not "
        "an order; nothing is placed."
    )
    return (
        "<h2>Stake</h2><div class='wrap'>"
        f"<table class='sheet'>{head}{body}</table></div><p class='note'>{html.escape(note)}</p>"
    )


LADDER_STEP = {
    "points": 2,
    "rebounds": 1,
    "assists": 1,
    "three_made": 1,
    "shots": 1,
    "goals": 1,
    "saves": 3,
    "pass_yards": 25,
    "rush_yards": 10,
    "rec_yards": 10,
    "receptions": 1,
    "pass_td": 1,
}


def _line_text(row: dict) -> str:
    return f"{row['median']} ({row['p10']}–{row['p90']})"


def _lines_table(sport: str, lines: list[dict], game_href: str, side_abbr: str) -> str:
    """One side's leaders, a row a player, a column a stat, each cell the median and band."""
    stats = list(SPORT_STATS.get(sport, {}))
    rows_by_player: dict[str, dict] = {}
    for row in lines:
        entry = rows_by_player.setdefault(
            row["player_id"], {"name": row["name"], "position": row["position"], "cells": {}}
        )
        entry["cells"][row["stat"]] = row
    ordered = sorted(
        rows_by_player.items(),
        key=lambda kv: -max((r["mean"] for r in kv[1]["cells"].values()), default=0.0),
    )
    body = ""
    for player_id, entry in ordered:
        href = f"{game_href}/{html.escape(player_id)}"
        pos = (
            f" <span class='note'>{html.escape(entry['position'])}</span>"
            if entry["position"]
            else ""
        )
        cells = ""
        for stat in stats:
            row = entry["cells"].get(stat)
            if row is None:
                cells += "<td>–</td>"
            else:
                graded = row.get("outcome") == "graded"
                text = html.escape(_line_text(row))
                if graded:
                    text += f"<br><b>{row['actual']}</b>"
                elif row.get("outcome") == "did_not_play":
                    text += "<br><span class='note'>DNP</span>"
                cells += f"<td>{text}</td>"
        body += (
            f"<tr><td class='label'><a href='{href}'>{html.escape(entry['name'])}</a>{pos}</td>"
            f"{cells}</tr>"
        )
    if not body:
        return ""
    head = (
        "<tr><th class='label'>"
        + html.escape(side_abbr)
        + "</th>"
        + "".join(f"<th>{html.escape(STAT_LABELS.get(s, s))}</th>" for s in stats)
        + "</tr>"
    )
    return f"<div class='wrap'><table class='sheet'>{head}{body}</table></div>"


def _players_block(sheet: dict, settings: Settings) -> str:
    """The game's player lines on the sheet: each side's leaders, median and 80% band."""
    if not settings.PLAYERS_ENABLED or not settings.PROJECTION_ENABLED:
        return ""
    lines = sheet.get("players") or []
    if not lines:
        return (
            "<h2>Player lines</h2><p class='note'>none yet: lines need both sides' players in "
            "the log with two games each, and are written within the hour.</p>"
        )
    href = f"/props/{html.escape(sheet['sport'])}/{html.escape(sheet['game_id'])}"
    home, away = sheet["home"]["abbreviation"], sheet["away"]["abbreviation"]
    tables = _lines_table(sheet["sport"], [r for r in lines if not r["home"]], href, away)
    tables += _lines_table(sheet["sport"], [r for r in lines if r["home"]], href, home)
    return (
        f"<h2>Player lines</h2>{tables}<p class='note'>Median and 80% band of each line, from "
        "the player's rate, his projected minutes and the game projection, graded at the final "
        f"on the scorecard. <a href='{href}'>The public page</a> has every distribution. "
        "Nothing here is a recommendation.</p>"
    )


def _metric_cells(team: dict, metric, league_size: int, reverse: bool, half_keys) -> str:
    key = metric.key
    season = _fmt(team["season"].get(key), metric.decimals, metric.percent)
    split = _fmt(team["split"].get(key), metric.decimals, metric.percent)
    half = _fmt(team["first_half"].get(key), metric.decimals) if key in half_keys else "–"
    last3 = _fmt(team["last3"].get(key), metric.decimals, metric.percent)
    if metric.higher_is_better is None:
        rank = "<td>–</td>"
    else:
        rank = _rank_cell(team["last3_rank"].get(key), league_size)
    parts = [
        f"<td>{season}</td>",
        f"<td>{split}</td>",
        f"<td>{half}</td>",
        f"<td class='l3'>{last3}</td>",
        rank,
    ]
    return "".join(reversed(parts) if reverse else parts)


def _section_table(sheet: dict, section: str) -> str:
    away, home, n = sheet["away"], sheet["home"], sheet["league_size"]
    profile = LOGS[sheet["sport"]]
    half = html.escape(profile.half_label)
    half_keys = set(profile.first_half_sources)
    away_head = (
        f"{_logo(away, 'logo-sm')}{html.escape(away['abbreviation'])} ({away['games']} games)"
    )
    home_head = (
        f"{_logo(home, 'logo-sm')}{html.escape(home['abbreviation'])} ({home['games']} games)"
    )
    columns = f"<th>Season</th><th>{{split}}</th><th>{half}</th><th>Last 3</th><th>L3 rank</th>"
    head = (
        f"<tr><th class='label'></th><th class='side' colspan='5'>{away_head}</th><th></th>"
        f"<th class='side' colspan='5'>{home_head}</th><th class='label'></th></tr>"
        f"<tr><th class='label'>Statistic</th>{columns.format(split='Away')}<th>Adv</th>"
        f"<th>L3 rank</th><th>Last 3</th><th>{half}</th><th>Home</th><th>Season</th>"
        "<th class='label'>Statistic</th></tr>"
    )
    rows = []
    for metric in profile.metrics:
        if metric.section != section:
            continue
        adv = sheet["advantages"].get(metric.key)
        if adv is None:
            adv_html = "–"
        else:
            adv_html = _logo(sheet[adv], "") or html.escape(sheet[adv]["abbreviation"])
        label = html.escape(metric.label)
        rows.append(
            f"<tr><td class='label'>{label}</td>{_metric_cells(away, metric, n, False, half_keys)}"
            f"<td class='adv'>{adv_html}</td>{_metric_cells(home, metric, n, True, half_keys)}"
            f"<td class='label'>{label}</td></tr>"
        )
    table = f"<table class='sheet'>{head}{''.join(rows)}</table>"
    return f"<h2>{html.escape(section)}</h2><div class='wrap'>{table}</div>"


def render_matchup(sheet: dict, settings: Settings) -> str:
    away, home = sheet["away"], sheet["home"]
    profile = LOGS[sheet["sport"]]
    joiner = "vs" if sheet.get("neutral") else "at"
    title = f"{away['abbreviation']} {joiner} {home['abbreviation']}"
    sections = "".join(_section_table(sheet, section) for section in SECTIONS)
    week = f", week {sheet['week']}" if sheet.get("week") else ""
    subtitle = html.escape(sheet["sport"].upper() + week)
    n = sheet["league_size"]
    split_by = "moneyline" if tuning_for(sheet["sport"]).margin_from_moneyline else "spread"
    footnote = (
        f"Per-game figures from ESPN box scores for this season's {sheet['games_in_log']} "
        f"logged games. Ranks are among the {n} teams in the log; the table ranks use each "
        "team's last 3 games, the Off, Def and Overall ranks under the names use the season "
        f"({profile.unit}, {profile.unit} allowed, margin). Green is the top third, red the "
        'bottom third. "Away" and "Home" are the team\'s own games at that venue type. '
        f"{profile.note} The market implied score is the book's total split by its "
        f"{split_by}. The projection is the scanner's own model, graded on the scorecard; the "
        "blend leans on the book while games are few. Nothing on this page is a recommendation."
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>{STYLE}</head>
<body class="wide">
<p class="note"><a href="/matchups">All matchups</a> · <a href="/health">Status</a></p>
<div class="band">
<h1 style="margin-top:0">{html.escape(title)} <span class="note">{subtitle}</span></h1>
<div class="cards">{_team_card(away, "Away", n)}{_team_card(home, "Home", n)}</div>
</div>
{_strip(sheet, settings)}
{_projection_block(sheet, settings)}
{_stake_block(sheet, settings)}
{_players_block(sheet, settings)}
{sections}
<p class="note">{footnote}</p>
</body></html>"""


def render_matchups(
    by_sport: dict[str, list[dict]],
    settings: Settings,
    status,
    projections: dict[str, dict[str, dict]] | None = None,
) -> str:
    zone = ZoneInfo(settings.TZ)
    blocks = []
    for sport in settings.gamelog_sports:
        rows = by_sport.get(sport) or []
        items = []
        for row in rows:
            slate = row["slate"]
            kickoff = datetime.fromisoformat(row["date"]).astimezone(zone)
            book = BookLine.from_dict(slate.get("book")) if slate.get("book") else None
            home_abbr, away_abbr = slate["home"]["abbreviation"], slate["away"]["abbreviation"]
            line = ""
            if book is not None and tuning_for(sport).margin_from_moneyline:
                if book.home_ml is not None and book.away_ml is not None:
                    line = f"{home_abbr} {_odds(book.home_ml)} / {away_abbr} {_odds(book.away_ml)}"
            elif book is not None and book.home_spread is not None:
                line = f"{home_abbr} {book.home_spread:+g}"
            margin = book_margin(book, sport)
            if line and book.total is not None:
                line += f", total {book.total:g}"
                if margin is not None:
                    implied_home = (book.total + margin) / 2.0
                    implied_away = (book.total - margin) / 2.0
                    implied = f"implied {implied_away:.1f} – {implied_home:.1f}"
                    line += f"<br><span class='note'>{implied}</span>"
            stored = (projections or {}).get(sport, {}).get(row["game_id"])
            if stored:
                raw = stored["projection"]["raw"]
                model = (
                    f"model {html.escape(slate['home']['abbreviation'])} {-raw['margin']:+.1f}, "
                    f"total {raw['total']:.1f}"
                )
                line += ("<br>" if line else "") + f"<span class='note'>{model}</span>"
            joiner = "vs" if slate.get("neutral") else "at"
            away_name = _logo(slate["away"], "logo-sm") + html.escape(slate["away"]["abbreviation"])
            home_name = _logo(slate["home"], "logo-sm") + html.escape(slate["home"]["abbreviation"])
            name = f"{away_name} {joiner} {home_name}"
            href = f"/matchup/{html.escape(sport)}/{html.escape(row['game_id'])}"
            items.append(
                f"<tr><td class='when'>{html.escape(kickoff.strftime('%a %-I:%M %p'))}</td>"
                f"<td class='who'><a href='{href}'>{name}</a></td>"
                f"<td class='num'>{line}</td></tr>"
            )
        empty = "<tr><td colspan='3' class='note'>no games in the next two days</td></tr>"
        blocks.append(
            f"<h2>{html.escape(sport.upper())}</h2><table>{''.join(items) or empty}</table>"
        )
    zone_label = html.escape(_zone_label(settings.TZ))
    log_note = html.escape(_gamelog_health(status, settings))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Matchups</title>{STYLE}</head>
<body>
<p class="note"><a href="/health">Status</a> · <a href="/scorecard">Scorecard</a></p>
<h1>Matchups</h1>
<p class="note">Upcoming games with a matchup sheet. Times are {zone_label}.
Game log: {log_note}.</p>
{"".join(blocks)}
</body></html>"""


def render_health(status: RuntimeStatus, settings: Settings) -> str:
    d = status.as_dict()
    feeds_ok = not d["score_feed_failing_since"] and not d["price_feed_failing_since"]
    rows = [
        ("Version", d["version"]),
        ("Build", settings.short_commit),
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
            "Period markets",
            "off"
            if not settings.PERIOD_MARKETS_ENABLED
            else ("alerts on" if settings.PERIOD_ALERTS_ENABLED else "recorded, not sent"),
        ),
        (
            "Observation window",
            f"{settings.MAX_MINUTES_LEFT:g} to {settings.OBSERVATION_MINUTES_LEFT:g} min, "
            "nothing sent"
            if settings.OBSERVATION_MINUTES_LEFT > settings.MAX_MINUTES_LEFT
            else "off",
        ),
        ("Pre-game scan", _pregame_health(status, settings)),
        ("Game log", _gamelog_health(status, settings)),
        ("Daily card", _card_health(status, settings)),
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
<p class="note"><a href="/scorecard">Scorecard</a> · <a href="/matchups">Matchups</a> ·
<a href="/card">Daily card</a> · <a href="/logout">Sign out of this device</a></p>
</body></html>"""


# -- the daily card --------------------------------------------------------------------


def _implied(odds: int) -> float:
    """The win probability a price implies, with the book's margin left in."""
    return abs(odds) / (abs(odds) + 100.0) if odds < 0 else 100.0 / (odds + 100.0)


def _value_text(pick: dict, value: float | None) -> str:
    """A margin as the home side's line, or a total, in the pick's own words."""
    if value is None:
        return "–"
    if pick["market"] == MARKET_TOTAL:
        return f"{value:.1f}"
    return f"{pick['home']} {-value:+.1f}"


def _result_chip(pick: dict) -> str:
    outcome = pick.get("outcome")
    if outcome is None:
        return "<span class='chip open'>open</span>"
    if outcome in ("win", "loss", "push"):
        return f"<span class='chip {outcome}'>{outcome.upper()} {_units(pick.get('units'))}</span>"
    return f"<span class='chip not_graded'>{html.escape(outcome.replace('_', ' '))}</span>"


def _pick_picture(pick: dict) -> str:
    """The pick's histogram: margins for a side, totals for an over or under, its side shaded."""
    sim = (pick.get("detail") or {}).get("sim")
    if not sim:
        return ""
    home = pick["home"]
    unit = LOGS[pick["sport"]].unit if pick["sport"] in LOGS else "points"
    if pick["market"] == MARKET_TOTAL:
        return svg_histogram(
            sim["total"]["hist"],
            line=pick["book_value"],
            mean=pick["model_value"],
            side_above=pick["side"] == "over",
            line_label=f"book {pick['book_value']:g}",
            mean_label=f"model {pick['model_value']:.1f}",
            axis_label=f"Total {unit}",
        )
    return svg_histogram(
        sim["margin"]["hist"],
        line=pick["book_value"],
        mean=pick["model_value"],
        side_above=pick["side"] == "home",
        line_label=f"book {home} {-pick['book_value']:+.1f}",
        mean_label=f"model {home} {-pick['model_value']:+.1f}",
        axis_label=f"Margin ({home}, {unit})",
    )


def _pick_block(pick: dict, settings: Settings, link: bool) -> str:
    """One pick as the card shows it: the call, the numbers behind it, its picture."""
    detail = pick.get("detail") or {}
    zone = settings.CARD_TZ
    when = html.escape(_kickoff(datetime.fromisoformat(pick["kickoff"]), zone))
    home_team = detail.get("home_team") or {"abbreviation": pick["home"]}
    away_team = detail.get("away_team") or {"abbreviation": pick["away"]}
    joiner = "vs" if detail.get("neutral") else "at"
    matchup = (
        f"{_logo(away_team, 'logo-sm')}{html.escape(pick['away'])} {joiner} "
        f"{_logo(home_team, 'logo-sm')}{html.escape(pick['home'])}"
    )
    unit = LOGS[pick["sport"]].unit if pick["sport"] in LOGS else "points"
    sim = detail.get("sim") or {}
    chance = detail.get("chance")
    draws = sim.get("draws") or 0
    what = {
        MARKET_SPREAD: "covers",
        MARKET_MONEYLINE: "wins",
        MARKET_TOTAL: "lands",
    }[pick["market"]]
    sim_text = (
        f"In {draws:,} simulated games {html.escape(pick['side_label'])} {what} "
        f"{chance * 100:.0f}% of the time; the price says {_implied(pick['odds']) * 100:.0f}%."
        if chance is not None and draws
        else ""
    )
    trust = detail.get("trust") or {}
    trust_text = (
        f"closing lines {trust['positive_share'] * 100:.0f}% the model's way over {trust['closed']}"
        if trust.get("weighted")
        else f"closing-line record not yet weighed, {trust.get('closed', 0)} closed"
    )
    final = ""
    if pick.get("final_home") is not None:
        final = (
            f" Final {html.escape(pick['away'])} {pick['final_away']}, "
            f"{html.escape(pick['home'])} {pick['final_home']}."
        )
        if pick.get("clv") is not None:
            final += f" Closing line moved {pick['clv']:+.1f} {unit} toward the pick."
    where = f"/card/{html.escape(pick['card_date'])}/{pick['rank']}"
    more = (
        f"<p class='note'><a href='{where}'>Full simulation: every score, every line</a> · "
        f"<a href='{where}.png'>Share image</a></p>"
        if link
        else ""
    )
    sport_chip = f"<span class='chip sport'>{html.escape(pick['sport'].upper())}</span>"
    call = (
        f"{html.escape(pick['side_label'])} <span class='odds'>({_odds(pick['odds'])})</span> "
        f"{_result_chip(pick)}"
    )
    numbers = (
        f"Model {html.escape(_value_text(pick, pick['model_value']))} · book "
        f"{html.escape(_value_text(pick, pick['book_value']))} · {abs(pick['gap']):.1f} {unit} "
        f"apart, {pick['z']:.2f} of a game's spread."
    )
    conviction = (
        f"Conviction {pick['conviction']:.2f}: trust {pick['trust']:.2f} "
        f"({html.escape(trust_text)}).{final}"
    )
    return f"""<div class="pick">
<p class="meta"><span class="rank">#{pick["rank"]}</span>{sport_chip} {when}</p>
<h2>{matchup}</h2>
<p class="call">{call}</p>
<p class="meta">{numbers}</p>
<p class="meta">{sim_text}</p>
<p class="note">{conviction}</p>
{_pick_picture(pick)}
{more}
</div>"""


def _card_head(settings: Settings) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily card</title>{STYLE}</head>
<body>"""


def _record_strip(cd: dict) -> str:
    if not cd.get("graded"):
        return ""
    clv = "–" if cd.get("clv") is None else f"{cd['clv']:+.2f}"
    return (
        "<div class='strip'>"
        f"<div><span>Record</span><b>{cd['wins']}-{cd['losses']}-{cd['pushes']}</b></div>"
        f"<div><span>Units</span><b>{_units(cd['units'])}</b></div>"
        f"<div><span>Per pick</span><b>{_pct(cd['roi'])}</b></div>"
        f"<div><span>Closing line</span><b>{clv}</b></div>"
        f"<div><span>Days</span><b>{cd['days']}</b></div>"
        "</div>"
    )


def render_card(
    card: dict | None,
    summary: dict,
    history: list[dict],
    settings: Settings,
    now: datetime,
) -> str:
    """The public card page: today's picks (or the latest), the record, the days before."""
    zone = settings.CARD_TZ
    label = _zone_label(zone)
    lock = settings.card_lock_time.strftime("%-I:%M %p")
    if card is None:
        due = card_date_for(now, settings)
        next_lock = lock_moment(due, settings)
        if next_lock <= now:
            next_lock = lock_moment(due + timedelta(days=1), settings)
        body = (
            "<p class='note'>No card yet. The first locks at "
            f"{html.escape(_kickoff(next_lock, zone))}.</p>"
        )
        title = "Daily card"
    else:
        locked = datetime.fromisoformat(card["locked_at"])
        late = " (late)" if card["late"] else ""
        title = f"Daily card, {card['card_date']}"
        body = (
            f"<p class='note'>Locked {html.escape(_kickoff(locked, zone))}{late}. "
            f"{len(card['rows'])} picks, one per game. "
            f"<a href='/card/{html.escape(card['card_date'])}.png'>Share image</a></p>"
            + "".join(_pick_block(p, settings, link=True) for p in card["rows"])
        )
    by_rank = "".join(
        f"<tr><th>Rank {html.escape(rank)}</th><td class='num'>{html.escape(_record_line(t))}</td>"
        "</tr>"
        for rank, t in (summary.get("by_rank") or {}).items()
    )
    by_sport = "".join(
        f"<tr><th>{html.escape(s.upper())}</th><td class='num'>{html.escape(_record_line(t))}</td>"
        "</tr>"
        for s, t in (summary.get("by_sport") or {}).items()
    )
    record = (
        f"<h2>Record by rank</h2><table>{by_rank}</table><h2>Record by sport</h2>"
        f"<table>{by_sport}</table>"
        if summary.get("graded")
        else ""
    )
    days = "".join(
        "<tr><td><a href='/card/{d}'>{d}</a>{late}</td><td class='num'>{picks}</td>"
        "<td class='num'>{rec}</td></tr>".format(
            d=html.escape(day["card_date"]),
            late=" <span class='note'>late</span>" if day["late"] else "",
            picks=day["picks"],
            rec=html.escape(
                f"{day['wins']}-{day['losses']}-{day['pushes']}, {_units(day['units'])}"
                if day["graded"]
                else f"{day['open']} open"
            ),
        )
        for day in history
    )
    history_table = (
        f"<h2>Every card</h2><table><tr><th>Day</th><th>Picks</th><th>Record</th></tr>{days}"
        "</table>"
        if days
        else ""
    )
    note = (
        f"<p class='note'>Every day at {lock} {html.escape(label)} the model's "
        f"{settings.CARD_PICKS} surest sides against the book are locked by a fixed rule: one per "
        "game, ranked by the gap between the model's number and the book's in that sport's "
        "usual spread of results, weighted by how often the sport's closing line has moved the "
        "model's way. The model is built from this season's box scores alone: it does not know "
        "about injuries, rest or weather, and the book does, so a big gap is as likely a missing "
        "player as an edge. That is why every pick is graded at the final, misses included, and "
        "why the record is kept rank by rank. Units are one unit a pick at the book's price. "
        "The pictures are the model's own numbers played out 100,000 times. Nothing here is "
        "advice and nothing is placed.</p>"
    )
    return f"""{_card_head(settings)}
<h1>{html.escape(title)}</h1>
{_record_strip(summary)}
{body}
{record}
{history_table}
{note}
</body></html>"""


def _sim_sections(sport: str, home: str, away: str, projection: dict, sim: dict, pick) -> str:
    """The full picture of one simulated game: margins, totals, every score, every line."""
    unit = LOGS[sport].unit if sport in LOGS else "points"
    raw, book = projection.get("raw") or {}, projection.get("book") or {}
    market = pick["market"] if pick else None
    side = pick["side"] if pick else None
    margin_side = None if market not in (MARKET_SPREAD, MARKET_MONEYLINE) else side == "home"
    total_side = None if market != MARKET_TOTAL else side == "over"
    margin_label = "Margin" if tuning_for(sport).margin_from_moneyline else "Spread"
    book_margin, book_total = book.get("margin"), book.get("total")
    margin_svg = svg_histogram(
        sim["margin"]["hist"],
        line=book_margin,
        mean=raw.get("margin"),
        side_above=margin_side,
        line_label="" if book_margin is None else f"book {home} {-book_margin:+.1f}",
        mean_label=f"model {home} {-raw['margin']:+.1f}" if raw.get("margin") is not None else "",
        axis_label=f"Margin ({home}, {unit})",
    )
    total_svg = svg_histogram(
        sim["total"]["hist"],
        line=book_total,
        mean=raw.get("total"),
        side_above=total_side,
        line_label="" if book_total is None else f"book {book_total:g}",
        mean_label=f"model {raw['total']:.1f}" if raw.get("total") is not None else "",
        axis_label=f"Total {unit}",
    )
    draws = sim["draws"]
    scores = "".join(
        f"<tr><td>{html.escape(away)} {a}, {html.escape(home)} {h}</td>"
        f"<td class='num'>{n / draws * 100:.1f}%</td></tr>"
        for h, a, n in sim["scores"]
    )
    step = 0.5 if unit == "goals" else 1.0
    rungs = []
    centre = book_margin if book_margin is not None else round(raw.get("margin") or 0.0)
    for rung in ladder(sim["margin"]["hist"], centre, step):
        rungs.append(
            f"<tr><td>{html.escape(home)} {-rung['line']:+g}</td>"
            f"<td class='num'>{rung['above'] * 100:.1f}%</td>"
            f"<td class='num'>{rung['below'] * 100:.1f}%</td>"
            f"<td class='num'>{rung['push'] * 100:.1f}%</td></tr>"
        )
    totals = []
    centre = book_total if book_total is not None else round(raw.get("total") or 0.0)
    for rung in ladder(sim["total"]["hist"], centre, step):
        totals.append(
            f"<tr><td>{rung['line']:g}</td><td class='num'>{rung['above'] * 100:.1f}%</td>"
            f"<td class='num'>{rung['below'] * 100:.1f}%</td>"
            f"<td class='num'>{rung['push'] * 100:.1f}%</td></tr>"
        )
    heat = svg_heatmap(sim.get("cells") or {}, home, away)
    tie = chances(sim["margin"]["hist"], 0)["push"]
    overtime = f"{sim['overtime'] * 100:.0f}% went to overtime" if sim.get("overtime") else ""
    r, lg, used = projection.get("ratings") or {}, projection.get("league") or {}, {}
    used = projection.get("games_used") or {}
    inputs = ""
    if r and lg:
        inputs = (
            f"<p class='note'>Ratings in {unit} against an average side (defence below zero "
            f"allows fewer): {html.escape(home)} offence {_signed(r['home']['offense'])}, defence "
            f"{_signed(r['home']['defense'])}; {html.escape(away)} offence "
            f"{_signed(r['away']['offense'])}, defence {_signed(r['away']['defense'])}. League "
            f"{lg['points']:.1f} a side, home edge {lg['hfa']:g}, spread of results "
            f"{lg['sigma']:g} on the margin and {lg['sigma_total']:g} on the total; "
            f"{used.get('home', '?')} and {used.get('away', '?')} games used.</p>"
        )
    wins = f"{html.escape(home)} wins {sim['home_win'] * 100:.0f}% of {draws:,} games"
    if overtime:
        wins += f"; {overtime}"
    if tie:
        wins += f"; {tie * 100:.1f}% ended level"
    heat_note = (
        f"{html.escape(away)} along the bottom, {html.escape(home)} up the side; darker came up "
        f"more often. Above the dotted line {html.escape(home)} wins."
    )
    rung_head = (
        f"<tr><th>Line</th><th>{html.escape(home)} covers</th><th>{html.escape(away)} covers</th>"
        "<th>Push</th></tr>"
    )
    method = (
        "Each game is a margin and a total drawn from the model's own numbers and their usual "
        "spread, turned into a score; a level game goes to overtime. The picture inherits every "
        "error in the ratings: it is the model's opinion in full, not a better model. Key "
        "numbers (3 and 7 in football) are not modelled."
    )
    return f"""<h2>Margin</h2>
<p class='note'>{wins}.</p>
{margin_svg}
<h2>Total</h2>
{total_svg}
<h2>Likeliest finals</h2>
<table class='scores'><tr><th>Score</th><th>Share</th></tr>{scores}</table>
<h2>Every score</h2>
<p class='note'>{heat_note}</p>
{heat}
<h2>{margin_label} at other lines</h2>
<table>{rung_head}{"".join(rungs)}</table>
<h2>Total at other lines</h2>
<table><tr><th>Line</th><th>Over</th><th>Under</th><th>Push</th></tr>{"".join(totals)}</table>
{inputs}
<p class='note'>{method}</p>"""


def render_card_pick(pick: dict, settings: Settings) -> str:
    """One pick's page: the card entry, then the whole simulation behind it."""
    detail = pick.get("detail") or {}
    projection = detail.get("projection") or {}
    stored = detail.get("sim") or {}
    sim = simulate(
        projection,
        pick["sport"],
        draws=stored.get("draws") or 0,
        seed=stored.get("seed"),
        with_cells=True,
    )
    sections = (
        _sim_sections(pick["sport"], pick["home"], pick["away"], projection, sim, pick)
        if sim
        else "<p class='note'>no simulation stored for this pick</p>"
    )
    day = html.escape(pick["card_date"])
    return f"""{_card_head(settings)}
<p class="note"><a href="/card/{day}">Card {day}</a> · <a href="/card">Latest card</a></p>
<h1>Pick #{pick["rank"]}, {day}</h1>
{_pick_block(pick, settings, link=False)}
{sections}
</body></html>"""


def render_sim(sport: str, upcoming: dict, projection: dict, settings: Settings) -> str:
    """The simulation page for any projected game (the owner's, behind the token)."""
    slate = upcoming["slate"]
    home, away = slate["home"]["abbreviation"], slate["away"]["abbreviation"]
    seed = seed_for(sport, upcoming["game_id"], str(projection.get("updated_at") or ""))
    sim = simulate(projection, sport, seed=seed, with_cells=True)
    kickoff = datetime.fromisoformat(slate["date"]) if slate.get("date") else None
    sections = (
        _sim_sections(sport, home, away, projection, sim, None)
        if sim
        else "<p class='note'>no projection to simulate</p>"
    )
    href = f"/matchup/{html.escape(sport)}/{html.escape(upcoming['game_id'])}"
    subtitle = html.escape(f"{sport.upper()}, {_kickoff(kickoff, settings.TZ)}")
    return f"""{_card_head(settings)}
<p class="note"><a href="{href}">Matchup sheet</a> · <a href="/matchups">All matchups</a></p>
<h1>{html.escape(f"{away} at {home}")} <span class="note">{subtitle}</span></h1>
{sections}
</body></html>"""


# -- player lines, public ------------------------------------------------------------------


def _props_head(title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>{STYLE}</head>
<body class="wide">"""


def _props_record(ps: dict) -> str:
    if not ps or not ps.get("graded"):
        return ""
    return (
        "<div class='strip'>"
        f"<div><span>Lines graded</span><b>{ps['graded']}</b></div>"
        f"<div><span>Above the median</span><b>{_pct(ps['over_median'])}</b></div>"
        f"<div><span>Inside the 80% band</span><b>{_pct(ps['in_band'])}</b></div>"
        f"<div><span>Avg miss</span><b>{ps['mae']:.1f}</b></div>"
        "</div>"
    )


def render_props_index(
    by_sport: dict[str, list[dict]],
    counts: dict[str, dict[str, int]],
    summary: dict,
    settings: Settings,
) -> str:
    """Every upcoming game with player lines, by sport, and the record so far."""
    zone = ZoneInfo(settings.CARD_TZ)
    blocks = []
    for sport in settings.gamelog_sports:
        items = []
        for row in by_sport.get(sport) or []:
            n = (counts.get(sport) or {}).get(row["game_id"])
            if not n:
                continue
            slate = row["slate"]
            kickoff = datetime.fromisoformat(row["date"]).astimezone(zone)
            joiner = "vs" if slate.get("neutral") else "at"
            name = f"{slate['away']['abbreviation']} {joiner} {slate['home']['abbreviation']}"
            href = f"/props/{html.escape(sport)}/{html.escape(row['game_id'])}"
            items.append(
                f"<tr><td class='when'>{html.escape(kickoff.strftime('%a %-I:%M %p'))}</td>"
                f"<td class='who'><a href='{href}'>{html.escape(name)}</a></td>"
                f"<td class='num'>{n} lines</td></tr>"
            )
        if items:
            blocks.append(f"<h2>{html.escape(sport.upper())}</h2><table>{''.join(items)}</table>")
    body = "".join(blocks) or "<p class='note'>No games with player lines in the next two days.</p>"
    note = (
        "<p class='note'>A line is the median of the player's projected stat, with the band "
        "the model gives an 80% chance; every line is graded at the final and the record above "
        "says whether the bands hold. The model knows the box scores and nothing else: no "
        "injury news, no rotation changes. Research, not advice; nothing is placed.</p>"
    )
    label = html.escape(_zone_label(settings.CARD_TZ))
    return f"""{_props_head("Player lines")}
<p class="note"><a href="/card">Daily card</a></p>
<h1>Player lines</h1>
{_props_record(summary)}
<p class="note">Times are {label}.</p>
{body}
{note}
</body></html>"""


def render_props_game(sport: str, upcoming: dict, lines: list[dict], settings: Settings) -> str:
    """One game's lines, both sides, each player linked to his distributions."""
    slate = upcoming["slate"]
    home, away = slate["home"]["abbreviation"], slate["away"]["abbreviation"]
    joiner = "vs" if slate.get("neutral") else "at"
    kickoff = datetime.fromisoformat(slate["date"]) if slate.get("date") else None
    href = f"/props/{html.escape(sport)}/{html.escape(upcoming['game_id'])}"
    tables = _lines_table(sport, [r for r in lines if not r["home"]], href, away)
    tables += _lines_table(sport, [r for r in lines if r["home"]], href, home)
    when = datetime.fromisoformat(lines[0]["updated_at"]) if lines else None
    written = f" Lines written {html.escape(_kickoff(when, settings.CARD_TZ))}." if when else ""
    subtitle = html.escape(f"{sport.upper()}, {_kickoff(kickoff, settings.CARD_TZ)}")
    note = (
        "Median and 80% band of each projected line; a bold figure is the actual once the game "
        "is in. Open a player for the whole distribution and the chance at any line."
        f"{written} Research, not advice; nothing is placed."
    )
    return f"""{_props_head(f"{away} {joiner} {home} player lines")}
<p class="note"><a href="/props">All games</a> · <a href="/card">Daily card</a></p>
<h1>{html.escape(f"{away} {joiner} {home}")} <span class="note">{subtitle}</span></h1>
{tables or "<p class='note'>no lines for this game</p>"}
<p class="note">{note}</p>
</body></html>"""


def _stat_order(sport: str, stat: str) -> int:
    """Each sport's own order of stats (points first), for the pages and the image."""
    stats = list(SPORT_STATS.get(sport, {}))
    return stats.index(stat) if stat in stats else len(stats)


def _player_ladder(row: dict) -> str:
    pmf = {int(x): p for x, p in row["pmf"].items()}
    step = LADDER_STEP.get(row["stat"], 1)
    centre = row["median"] + 0.5
    rungs = ""
    for k in range(-3, 4):
        line = centre + k * step
        if line < 0.5:
            continue
        split = chance_over(pmf, line)
        rungs += (
            f"<tr><td>{line:g}</td><td class='num'>{split['above'] * 100:.1f}%</td>"
            f"<td class='num'>{split['below'] * 100:.1f}%</td></tr>"
        )
    return f"<table><tr><th>Line</th><th>Over</th><th>Under</th></tr>{rungs}</table>"


def _player_stat_section(row: dict, sport: str) -> str:
    label = STAT_LABELS.get(row["stat"], row["stat"])
    stamp = f"{row['game_id']}:{row['player_id']}:{row['stat']}"
    seed = seed_for(sport, stamp, str(row.get("updated_at") or ""))
    sim = simulate_player(row["params"], seed=seed)
    mark = None
    if row.get("outcome") == "graded":
        mark = (float(row["actual"]), f"actual {row['actual']}")
    picture = svg_histogram(
        sim["hist"],
        line=None,
        mean=float(row["median"]),
        side_above=None,
        line_label="",
        mean_label=f"median {row['median']}",
        axis_label=label,
        band=(row["p10"], row["p90"]),
        mark=mark,
    )
    p = row["params"]
    if p.get("exposure_sd", 0) > 0:
        unit = "seconds" if sport == "nhl" else "minutes"
        exposure = f"{p['exposure_mu']:.0f} {unit} (±{p['exposure_sd']:.0f})"
        per = "minute on the ice" if sport == "nhl" else "minute"
        rate = f"{p['rate'] * (60 if sport == 'nhl' else 1):.3f} a {per}"
    else:
        exposure = "per game"
        rate = f"{p['rate']:.2f} a game"
    inputs = (
        f"Rate {rate} (league {p['league_rate'] * (60 if sport == 'nhl' else 1):.3f}), playing "
        f"time {exposure}, game factor {p['factor']:.2f}, {p['games']} games used, "
        f"dispersion {p['k']:g}."
    )
    result = ""
    if row.get("outcome") == "graded":
        where = "inside" if row["in_band"] else "outside"
        result = (
            f" <b>Actual {row['actual']}</b>, {where} the band, at the "
            f"{row['pit'] * 100:.0f}th percentile of the distribution."
        )
    elif row.get("outcome") == "did_not_play":
        result = " <b>Did not play.</b>"
    band = f"({row['p10']}–{row['p90']}, mean {row['mean']:.1f})"
    return f"""<h2>{html.escape(label)}: {row["median"]} <span class="note">{band}</span></h2>
<p class="note">{sim["draws"]:,} simulated games; the shaded bars are the 80% band.{result}</p>
{picture}
{_player_ladder(row)}
<p class="note">{html.escape(inputs)}</p>"""


def render_props_player(
    sport: str, upcoming: dict, lines: list[dict], recent: list[dict], settings: Settings
) -> str:
    """One player's lines for a game: each stat's distribution and ladder, his recent games."""
    slate = upcoming["slate"]
    lines = sorted(lines, key=lambda r: _stat_order(sport, r["stat"]))
    first = lines[0]
    home, away = slate["home"]["abbreviation"], slate["away"]["abbreviation"]
    kickoff = datetime.fromisoformat(slate["date"]) if slate.get("date") else None
    game_href = f"/props/{html.escape(sport)}/{html.escape(upcoming['game_id'])}"
    sections = "".join(_player_stat_section(row, sport) for row in lines)
    stats = [s for s in SPORT_STATS.get(sport, {}) if any(r["stat"] == s for r in lines)]
    exposure = "minutes" if sport in ("nba", "cbb") else ("toi" if sport == "nhl" else None)
    recent_rows = ""
    for g in recent:
        when = datetime.fromisoformat(g["date"]).astimezone(ZoneInfo(settings.CARD_TZ))
        opp = f"{'vs' if g['home'] else '@'} {g['opponent_abbr']}"
        cells = ""
        if exposure:
            value = g["stats"].get(exposure)
            if exposure == "toi" and value is not None:
                value = f"{value // 60}:{value % 60:02d}"
            cells += f"<td>{html.escape(str(value if value is not None else '–'))}</td>"
        for stat in stats:
            value = g["stats"].get(stat)
            cells += f"<td>{html.escape(str(value if value is not None else '–'))}</td>"
        played = "" if g["played"] else " <span class='note'>DNP</span>"
        recent_rows += (
            f"<tr><td class='label'>{html.escape(when.strftime('%b %-d'))} {html.escape(opp)}"
            f"{played}</td>{cells}</tr>"
        )
    head = (
        "<tr><th class='label'>Game</th>"
        + ("<th>Min</th>" if exposure else "")
        + "".join(f"<th>{html.escape(STAT_LABELS.get(s, s))}</th>" for s in stats)
        + "</tr>"
    )
    recent_table = (
        "<h2>Recent games</h2><div class='wrap'><table class='sheet'>"
        f"{head}{recent_rows}</table></div>"
        if recent_rows
        else ""
    )
    pos = (
        f" <span class='note'>{html.escape(first['position'])}</span>"
        if first.get("position")
        else ""
    )
    title = f"{first['name']}, {first['team_abbr']} v {first['opponent_abbr']}"
    share = f"{game_href}/{html.escape(first['player_id'])}.png"
    nav = (
        f"<a href='{game_href}'>{html.escape(f'{away} at {home}')}</a> · "
        f"<a href='/props'>All games</a> · <a href='{share}'>Share image</a>"
    )
    subtitle = html.escape(
        f"{first['team_abbr']} v {first['opponent_abbr']}, {sport.upper()}, "
        f"{_kickoff(kickoff, settings.CARD_TZ)}"
    )
    method = (
        "Each line is the player's rate (shrunk toward his position's league rate while his "
        "games are few) times his projected playing time, scaled by the game projection, "
        "spread as a negative binomial over the uncertainty in his minutes. Only players who "
        "played their team's last game are projected; nothing here knows about injuries. "
        "Graded at the final on the scorecard. Research, not advice; nothing is placed."
    )
    return f"""{_props_head(title)}
<p class="note">{nav}</p>
<h1>{html.escape(first["name"])}{pos} <span class="note">{subtitle}</span></h1>
{sections}
{recent_table}
<p class="note">{method}</p>
</body></html>"""


def render_home(
    card: dict | None, card_summary: dict, players_summary: dict, settings: Settings
) -> str:
    """The front door: the daily card and the player lines, each with its record, public."""
    lock = settings.card_lock_time.strftime("%-I:%M %p")
    zone = html.escape(_zone_label(settings.CARD_TZ))
    if card:
        picks = "".join(
            f"<tr><td><span class='rank'>#{p['rank']}</span>"
            f"{html.escape(p['sport'].upper())} {html.escape(p['away'])} at "
            f"{html.escape(p['home'])}</td><td>{html.escape(p['side_label'])} "
            f"({_odds(p['odds'])})</td><td>{_result_chip(p)}</td></tr>"
            for p in card["rows"]
        )
        card_block = (
            f"<p class='note'>Card {html.escape(card['card_date'])}, "
            f"{len(card['rows'])} picks.</p><table>{picks}</table>"
        )
    else:
        card_block = f"<p class='note'>No card yet; the first locks at {lock} {zone}.</p>"
    intro = (
        "A projection model built from this season's box scores, its "
        f"{settings.CARD_PICKS} surest sides each day and every leader's stat line for every "
        "game, all written down before the games and graded at the final, misses included. The "
        "model knows nothing of injuries, rest or weather. Nothing here is advice and nothing is "
        "placed."
    )
    card_note = (
        f"Locked by a fixed rule at {lock} {zone}, one side per game, ranked by the gap from "
        "the book. <a href='/card'>Every pick, every day, with its simulation</a>."
    )
    props_note = (
        "Each leader's projected line for every upcoming game, as a whole distribution drawn "
        "100,000 times. <a href='/props'>Today's games</a>."
    )
    owner = (
        "Owner's pages: <a href='/health'>status</a>, <a href='/scorecard'>scorecard</a>, "
        f"<a href='/matchups'>matchup sheets</a> (token). Build {settings.short_commit}."
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research</title>{STYLE}</head>
<body>
<h1>Research, graded</h1>
<p class="note">{intro}</p>
<h2><a href="/card">Daily card</a></h2>
{_record_strip(card_summary)}
{card_block}
<p class="note">{card_note}</p>
<h2><a href="/props">Player lines</a></h2>
{_props_record(players_summary)}
<p class="note">{props_note}</p>
<p class="note">{owner}</p>
</body></html>"""


def _jsonable(value):
    """Dataclasses and datetimes in a sheet, made plain for JSON."""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def create_app(settings: Settings, diary: Diary, status: RuntimeStatus) -> FastAPI:
    app = FastAPI(title="polymarket-scanner", docs_url=None, redoc_url=None, openapi_url=None)

    def authorised(token: str | None, header: str | None, cookie: str | None = None) -> bool:
        expected = settings.STATUS_TOKEN.get_secret_value() if settings.STATUS_TOKEN else ""
        if not expected:
            return False
        for candidate in (token, header, cookie):
            if candidate and secrets.compare_digest(candidate, expected):
                return True
        return False

    def remember(response: Response, request: Request, token: str | None) -> Response:
        """Sign the device in when the token came in the address: a cookie for a year."""
        if token and authorised(token, None):
            response.set_cookie(
                COOKIE,
                token,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=request.url.hostname not in LOCAL_HOSTS,
            )
        return response

    def denied() -> JSONResponse:
        if not settings.STATUS_TOKEN or not settings.STATUS_TOKEN.get_secret_value():
            return JSONResponse({"error": "STATUS_TOKEN is not configured"}, status_code=503)
        return JSONResponse({"error": "token required"}, status_code=401)

    @app.get("/health", response_class=HTMLResponse)
    def health(
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
        scanner_token: str | None = Cookie(default=None),
    ):
        if not authorised(token, x_status_token, scanner_token):
            return denied()
        if "application/json" in request.headers.get("accept", ""):
            page = JSONResponse({**status.as_dict(), "commit": settings.short_commit})
        else:
            page = HTMLResponse(render_health(status, settings))
        return remember(page, request, token)

    @app.get("/scorecard", response_class=HTMLResponse)
    def scorecard(
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
        scanner_token: str | None = Cookie(default=None),
    ):
        if not authorised(token, x_status_token, scanner_token):
            return denied()
        card = diary.scorecard()
        if "application/json" in request.headers.get("accept", ""):
            page = JSONResponse(card)
        else:
            page = HTMLResponse(render_scorecard(card, settings))
        return remember(page, request, token)

    @app.get("/matchups", response_class=HTMLResponse)
    def matchups(
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
        scanner_token: str | None = Cookie(default=None),
    ):
        if not authorised(token, x_status_token, scanner_token):
            return denied()
        by_sport = {sport: diary.football_upcoming(sport) for sport in settings.gamelog_sports}
        projections = {sport: diary.open_projections(sport) for sport in settings.gamelog_sports}
        page = HTMLResponse(render_matchups(by_sport, settings, status, projections))
        return remember(page, request, token)

    @app.get("/matchup/{sport}/{game_id}", response_class=HTMLResponse)
    def matchup(
        sport: str,
        game_id: str,
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
        scanner_token: str | None = Cookie(default=None),
    ):
        if not authorised(token, x_status_token, scanner_token):
            return denied()
        sheet = sheet_from_diary(diary, sport, game_id) if sport in LOGS else None
        if sheet is None:
            return JSONResponse({"error": "no sheet for this game"}, status_code=404)
        if "application/json" in request.headers.get("accept", ""):
            page = JSONResponse(_jsonable(sheet))
        else:
            page = HTMLResponse(render_matchup(sheet, settings))
        return remember(page, request, token)

    @app.get("/sim/{sport}/{game_id}", response_class=HTMLResponse)
    def sim_page(
        sport: str,
        game_id: str,
        request: Request,
        token: str | None = Query(default=None),
        x_status_token: str | None = Header(default=None),
        scanner_token: str | None = Cookie(default=None),
    ):
        if not authorised(token, x_status_token, scanner_token):
            return denied()
        upcoming = diary.football_upcoming_game(sport, game_id) if sport in LOGS else None
        stored = diary.projection(sport, game_id) if upcoming else None
        if upcoming is None or stored is None:
            return JSONResponse({"error": "no projection for this game"}, status_code=404)
        projection = {**stored["projection"], "updated_at": stored["updated_at"]}
        page = HTMLResponse(render_sim(sport, upcoming, projection, settings))
        return remember(page, request, token)

    def card_page(card_date: str | None, request: Request):
        card = diary.card(card_date) if card_date else None
        summary = diary.card_summary(picks=settings.CARD_PICKS)
        history = diary.card_history()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"card": card, "record": summary, "history": history})
        now = datetime.now(UTC)
        return HTMLResponse(render_card(card, summary, history, settings, now))

    def png(svg: str) -> Response:
        from scanner.share import to_png

        return Response(
            to_png(svg), media_type="image/png", headers={"Cache-Control": "public, max-age=300"}
        )

    def card_image(card_date: str | None):
        from scanner.share import card_svg

        card = diary.card(card_date) if card_date else None
        if card is None:
            return JSONResponse({"error": "no card for that day"}, status_code=404)
        return png(card_svg(card, diary.card_summary(picks=settings.CARD_PICKS), settings))

    # The image routes come before the page routes, which would otherwise take
    # "2026-10-11.png" for a date.
    @app.get("/card.png")
    def card_png_latest():
        return card_image(diary.latest_card_date())

    @app.get("/card/{card_date}.png")
    def card_png(card_date: str):
        return card_image(card_date)

    @app.get("/card/{card_date}/{rank}.png")
    def card_pick_png(card_date: str, rank: int):
        from scanner.share import pick_svg

        pick = diary.card_pick(card_date, rank)
        if pick is None:
            return JSONResponse({"error": "no such pick"}, status_code=404)
        return png(pick_svg(pick, settings))

    @app.get("/card", response_class=HTMLResponse)
    def card_latest(request: Request):
        return card_page(diary.latest_card_date(), request)

    @app.get("/card/{card_date}", response_class=HTMLResponse)
    def card_day(card_date: str, request: Request):
        if not diary.card_exists(card_date):
            return JSONResponse({"error": "no card for that day"}, status_code=404)
        return card_page(card_date, request)

    @app.get("/card/{card_date}/{rank}", response_class=HTMLResponse)
    def card_pick(card_date: str, rank: int, request: Request):
        pick = diary.card_pick(card_date, rank)
        if pick is None:
            return JSONResponse({"error": "no such pick"}, status_code=404)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(pick)
        return HTMLResponse(render_card_pick(pick, settings))

    @app.get("/props", response_class=HTMLResponse)
    def props_index(request: Request):
        by_sport = {sport: diary.football_upcoming(sport) for sport in settings.gamelog_sports}
        counts = {sport: diary.player_projection_counts(sport) for sport in settings.gamelog_sports}
        summary = diary.player_projection_summary()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"games": counts, "record": summary})
        return HTMLResponse(render_props_index(by_sport, counts, summary, settings))

    @app.get("/props/{sport}/{game_id}/{player_id}.png")
    def props_player_png(sport: str, game_id: str, player_id: str):
        from scanner.share import player_svg

        lines = [r for r in diary.player_projections(sport, game_id) if r["player_id"] == player_id]
        upcoming = diary.football_upcoming_game(sport, game_id) if lines else None
        if not lines or upcoming is None:
            return JSONResponse({"error": "no lines for this player"}, status_code=404)
        return png(player_svg(sport, upcoming, lines, settings))

    @app.get("/props/{sport}/{game_id}", response_class=HTMLResponse)
    def props_game(sport: str, game_id: str, request: Request):
        upcoming = diary.football_upcoming_game(sport, game_id) if sport in LOGS else None
        if upcoming is None:
            return JSONResponse({"error": "no such game"}, status_code=404)
        lines = diary.player_projections(sport, game_id)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(_jsonable(lines))
        return HTMLResponse(render_props_game(sport, upcoming, lines, settings))

    @app.get("/props/{sport}/{game_id}/{player_id}", response_class=HTMLResponse)
    def props_player(sport: str, game_id: str, player_id: str, request: Request):
        lines = [r for r in diary.player_projections(sport, game_id) if r["player_id"] == player_id]
        upcoming = diary.football_upcoming_game(sport, game_id) if lines else None
        if not lines or upcoming is None:
            return JSONResponse({"error": "no lines for this player"}, status_code=404)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(_jsonable(lines))
        recent = diary.player_recent_games(sport, player_id)
        return HTMLResponse(render_props_player(sport, upcoming, lines, recent, settings))

    @app.get("/logout", response_class=HTMLResponse)
    def logout():
        page = HTMLResponse(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Signed out</title>{STYLE}</head><body><h1>Signed out</h1>"
            "<p>This device no longer holds the token. Open any page with "
            "<code>?token=...</code> to sign in again.</p></body></html>"
        )
        page.delete_cookie(COOKIE)
        return page

    @app.get("/")
    def root(request: Request):
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "service": "polymarket-scanner",
                    "pages": ["/health", "/scorecard", "/matchups", "/card", "/props"],
                }
            )
        key = diary.latest_card_date()
        return HTMLResponse(
            render_home(
                diary.card(key) if key else None,
                diary.card_summary(picks=settings.CARD_PICKS),
                diary.player_projection_summary(),
                settings,
            )
        )

    return app
