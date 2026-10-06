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

from fastapi import Cookie, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from scanner import __version__
from scanner.books import BookLine
from scanner.config import Settings
from scanner.diary import Diary
from scanner.gamelog import FIRST_HALF_KEYS, FOOTBALL, METRICS, SECTIONS, sheet_from_diary

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
    items: list[tuple[str, str]] = [("Kickoff", _kickoff(sheet.get("kickoff"), settings.TZ))]
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
    if surface:
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


def _metric_cells(team: dict, metric, league_size: int, reverse: bool) -> str:
    key = metric.key
    season = _fmt(team["season"].get(key), metric.decimals, metric.percent)
    split = _fmt(team["split"].get(key), metric.decimals, metric.percent)
    half = _fmt(team["first_half"].get(key), metric.decimals) if key in FIRST_HALF_KEYS else "–"
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
    away_head = (
        f"{_logo(away, 'logo-sm')}{html.escape(away['abbreviation'])} ({away['games']} games)"
    )
    home_head = (
        f"{_logo(home, 'logo-sm')}{html.escape(home['abbreviation'])} ({home['games']} games)"
    )
    columns = "<th>Season</th><th>{split}</th><th>1st half</th><th>Last 3</th><th>L3 rank</th>"
    head = (
        f"<tr><th class='label'></th><th class='side' colspan='5'>{away_head}</th><th></th>"
        f"<th class='side' colspan='5'>{home_head}</th><th class='label'></th></tr>"
        f"<tr><th class='label'>Statistic</th>{columns.format(split='Away')}<th>Adv</th>"
        "<th>L3 rank</th><th>Last 3</th><th>1st half</th><th>Home</th><th>Season</th>"
        "<th class='label'>Statistic</th></tr>"
    )
    rows = []
    for metric in METRICS:
        if metric.section != section:
            continue
        adv = sheet["advantages"].get(metric.key)
        if adv is None:
            adv_html = "–"
        else:
            adv_html = _logo(sheet[adv], "") or html.escape(sheet[adv]["abbreviation"])
        label = html.escape(metric.label)
        rows.append(
            f"<tr><td class='label'>{label}</td>{_metric_cells(away, metric, n, False)}"
            f"<td class='adv'>{adv_html}</td>{_metric_cells(home, metric, n, True)}"
            f"<td class='label'>{label}</td></tr>"
        )
    table = f"<table class='sheet'>{head}{''.join(rows)}</table>"
    return f"<h2>{html.escape(section)}</h2><div class='wrap'>{table}</div>"


def render_matchup(sheet: dict, settings: Settings) -> str:
    away, home = sheet["away"], sheet["home"]
    joiner = "vs" if sheet.get("neutral") else "at"
    title = f"{away['abbreviation']} {joiner} {home['abbreviation']}"
    sections = "".join(_section_table(sheet, section) for section in SECTIONS)
    week = f", week {sheet['week']}" if sheet.get("week") else ""
    subtitle = html.escape(sheet["sport"].upper() + week)
    n = sheet["league_size"]
    footnote = (
        f"Per-game figures from ESPN box scores for this season's {sheet['games_in_log']} "
        f"logged games. Ranks are among the {n} teams in the log; the table ranks use each "
        "team's last 3 games, the Off, Def and Overall ranks under the names use the season "
        "(points, points allowed, point margin). Green is the top third, red the bottom third. "
        '"Away" and "Home" are the team\'s own games at that venue type. First-half yards '
        "come from the play-by-play. Sacks and plays are not in college box scores. The market "
        "implied score is the book's total split by its spread. Nothing on this page is a "
        "recommendation."
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
{sections}
<p class="note">{footnote}</p>
</body></html>"""


def render_matchups(by_sport: dict[str, list[dict]], settings: Settings, status) -> str:
    zone = ZoneInfo(settings.TZ)
    blocks = []
    for sport in FOOTBALL:
        rows = by_sport.get(sport) or []
        items = []
        for row in rows:
            slate = row["slate"]
            kickoff = datetime.fromisoformat(row["date"]).astimezone(zone)
            book = BookLine.from_dict(slate.get("book")) if slate.get("book") else None
            line = ""
            if book is not None and book.home_spread is not None:
                line = f"{slate['home']['abbreviation']} {book.home_spread:+g}"
                if book.total is not None:
                    line += f", total {book.total:g}"
                    implied_home = (book.total - book.home_spread) / 2.0
                    implied_away = (book.total + book.home_spread) / 2.0
                    implied = f"implied {implied_away:.1f} – {implied_home:.1f}"
                    line += f"<br><span class='note'>{implied}</span>"
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
<p class="note">Upcoming football games with a matchup sheet. Times are {zone_label}.
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
<a href="/logout">Sign out of this device</a></p>
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
        by_sport = {sport: diary.football_upcoming(sport) for sport in FOOTBALL}
        page = HTMLResponse(render_matchups(by_sport, settings, status))
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
        sheet = sheet_from_diary(diary, sport, game_id) if sport in FOOTBALL else None
        if sheet is None:
            return JSONResponse({"error": "no sheet for this game"}, status_code=404)
        if "application/json" in request.headers.get("accept", ""):
            page = JSONResponse(_jsonable(sheet))
        else:
            page = HTMLResponse(render_matchup(sheet, settings))
        return remember(page, request, token)

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
    def root():
        return JSONResponse(
            {"service": "polymarket-scanner", "pages": ["/health", "/scorecard", "/matchups"]}
        )

    return app
