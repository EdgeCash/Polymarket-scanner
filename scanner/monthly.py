"""The monthly check: the model's month against the finals and the closing lines.

A month is long enough to say something and short enough to catch a drift. This
reads every projection graded in a calendar month back from the diary and sets
it against what happened and against the line the book closed at: how often a
side the model gave 60-70% went on to win, its Brier score beside the close's,
how far the finals landed from the model's margin and total and from the
close's, how many finals fell outside the model's own middle 90% (ten percent
should), the record of the side the model took in each market against the
close, and how often the close moved the model's way. It is the scorecard's
"Monthly check" section, ``python -m scanner --monthly-check`` prints it, and
``--send`` sends it. Nothing here places anything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from scanner.books import BookLine
from scanner.grid import cover, grid
from scanner.projection import tuning_for

log = logging.getLogger(__name__)

BINS = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))
TAIL = 0.05  # each side: the middle 90%
MARKETS = ("moneyline", "spread", "total")


def month_bounds(label: str | None, now: datetime, tz: str) -> tuple[datetime, datetime, str]:
    """(start, end, 'YYYY-MM') of a calendar month in ``tz``: the one named, or the last
    full month before ``now``."""
    zone = ZoneInfo(tz)
    if label:
        year, month = (int(part) for part in label.split("-"))
    else:
        local = now.astimezone(zone)
        year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
    start = datetime(year, month, 1, tzinfo=zone)
    end = (
        datetime(year + 1, 1, 1, tzinfo=zone)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=zone)
    )
    return start.astimezone(UTC), end.astimezone(UTC), f"{year:04d}-{month:02d}"


def _record(results: list[str]) -> dict:
    wins = sum(1 for r in results if r == "win")
    losses = sum(1 for r in results if r == "loss")
    pushes = sum(1 for r in results if r == "push")
    decided = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "rate": wins / decided if decided else None,
    }


def _mean(values: list) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _tail_position(pmf: dict[int, float], actual: int) -> bool:
    """Whether ``actual`` falls outside the distribution's middle 90%."""
    below = sum(p for v, p in pmf.items() if v < actual)
    above = sum(p for v, p in pmf.items() if v > actual)
    return below < TAIL or above < TAIL


def _settle(side_home: bool, margin: float, line: float) -> str:
    """How the home side's handicap ``line`` settled for the side taken."""
    if margin + line == 0:
        return "push"
    covered = margin + line > 0
    return "win" if covered == side_home else "loss"


def check(rows: list[dict], sport: str) -> dict:
    """One sport's month. Each row carries ``projection``, ``grades`` (with ``final``) and
    ``close`` (the book's line as the game finished, as a dict, or None)."""
    tuning = tuning_for(sport)
    bins = [{"low": lo, "high": hi, "chance": 0.0, "wins": 0, "games": 0} for lo, hi in BINS]
    brier = {"model": [], "blend": [], "close": []}
    miss = {
        "margin": {"model": [], "blend": [], "close": []},
        "total": {"model": [], "blend": [], "close": []},
    }
    outside = {"margin": 0, "total": 0, "games": 0}
    results: dict[str, list[str]] = {m: [] for m in MARKETS}
    if sport == "nhl":
        results["puck_line"] = []
    clv_margin, clv_total = [], []
    for row in rows:
        projection, grades = row["projection"], row["grades"]
        raw, blend = projection.get("raw") or {}, projection.get("blend") or {}
        final = grades["final"]
        actual_margin = final["home"] - final["away"]
        actual_total = final["home"] + final["away"]
        outcome = 1.0 if actual_margin > 0 else 0.0 if actual_margin < 0 else 0.5
        close = BookLine.from_dict(row.get("close"))
        fair = close.fair_moneyline() if close else None
        p_home = raw.get("home_win")
        if p_home is not None:
            brier["model"].append((p_home - outcome) ** 2)
            favourite = max(p_home, 1 - p_home)
            for b in bins:
                if b["low"] <= favourite < b["high"] and actual_margin != 0:
                    b["chance"] += favourite
                    b["wins"] += int((actual_margin > 0) == (p_home >= 0.5))
                    b["games"] += 1
        if blend.get("home_win") is not None:
            brier["blend"].append((blend["home_win"] - outcome) ** 2)
        if fair:
            brier["close"].append((fair[0] - outcome) ** 2)
        for key, actual in (("margin", actual_margin), ("total", actual_total)):
            if raw.get(key) is not None:
                miss[key]["model"].append(abs(raw[key] - actual))
            if blend.get(key) is not None:
                miss[key]["blend"].append(abs(blend[key] - actual))
        if close and close.home_spread is not None:
            miss["margin"]["close"].append(abs(-close.home_spread - actual_margin))
        if close and close.total is not None:
            miss["total"]["close"].append(abs(close.total - actual_total))
        league = projection.get("league") or {}
        sigma = float(league.get("sigma") or tuning.sigma)
        sigma_total = float(league.get("sigma_total") or tuning.sigma_total)
        g = None
        if raw.get("margin") is not None and raw.get("total") is not None:
            g = grid(sport, raw["margin"], raw["total"], sigma, sigma_total)
        if g is not None:
            outside["games"] += 1
            outside["margin"] += _tail_position(g.margin, actual_margin)
            outside["total"] += _tail_position(g.total, actual_total)
        # The side the model took against the close, settled at the final.
        if fair and p_home is not None and p_home != fair[0]:
            side_home = p_home > fair[0]
            if actual_margin == 0:
                results["moneyline"].append("push")
            else:
                results["moneyline"].append("win" if (actual_margin > 0) == side_home else "loss")
        if close and close.home_spread is not None and raw.get("margin") is not None:
            if sport == "nhl":
                fair_spread = close.fair_spread()
                if g is not None and fair_spread:
                    chance = cover(g, close.home_spread)["above"]
                    if chance != fair_spread[0]:
                        results["puck_line"].append(
                            _settle(chance > fair_spread[0], actual_margin, close.home_spread)
                        )
            elif raw["margin"] != -close.home_spread:
                results["spread"].append(
                    _settle(raw["margin"] > -close.home_spread, actual_margin, close.home_spread)
                )
        if close and close.total is not None and raw.get("total") is not None:
            if raw["total"] != close.total:
                over = raw["total"] > close.total
                if actual_total == close.total:
                    results["total"].append("push")
                else:
                    results["total"].append(
                        "win" if (actual_total > close.total) == over else "loss"
                    )
        clv = grades.get("clv") or {}
        clv_margin.append(clv.get("margin"))
        clv_total.append(clv.get("total"))
    closed = [v for v in clv_margin if v is not None]
    return {
        "games": len(rows),
        "brier": {k: _mean(v) for k, v in brier.items()},
        "calibration": [
            {
                "bin": f"{int(b['low'] * 100)}-{min(int(b['high'] * 100), 100)}%",
                "games": b["games"],
                "chance": b["chance"] / b["games"] if b["games"] else None,
                "won": b["wins"] / b["games"] if b["games"] else None,
            }
            for b in bins
        ],
        "miss": {
            key: {source: _mean(values) for source, values in parts.items()}
            for key, parts in miss.items()
        },
        "outside_middle_90": {
            "margin": outside["margin"] / outside["games"] if outside["games"] else None,
            "total": outside["total"] / outside["games"] if outside["games"] else None,
            "games": outside["games"],
        },
        "markets": {market: _record(found) for market, found in results.items()},
        "clv": {
            "margin": _mean(clv_margin),
            "total": _mean(clv_total),
            "closed": len(closed),
            "positive_share": (sum(1 for v in closed if v > 0) / len(closed)) if closed else None,
        },
    }


def summary(diary, start: datetime, end: datetime, label: str) -> dict:
    """Every sport's check for the projections graded in [start, end)."""
    rows = diary.graded_projections_between(start, end)
    sports = sorted({r["sport"] for r in rows})
    return {
        "month": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "games": len(rows),
        "sports": {
            sport: check([r for r in rows if r["sport"] == sport], sport) for sport in sports
        },
    }


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _record_text(record: dict) -> str:
    text = f"{record['wins']}-{record['losses']}-{record['pushes']}"
    return text if record["rate"] is None else f"{text} ({record['rate'] * 100:.0f}%)"


def report(month: dict, tz: str) -> str:
    """The month as text, for the terminal and the phone."""
    lines = [f"Monthly check, {month['month']}: {month['games']} graded projections"]
    if not month["games"]:
        lines.append("Nothing graded this month.")
        return "\n".join(lines)
    for sport, part in month["sports"].items():
        unit = tuning_for(sport)
        del unit
        b, m, o, c = part["brier"], part["miss"], part["outside_middle_90"], part["clv"]
        lines.append(f"{sport.upper()}: {part['games']} games")
        lines.append(
            f"  Brier, model / blend / close: {_num(b['model'], 3)} / {_num(b['blend'], 3)} / "
            f"{_num(b['close'], 3)}"
        )
        lines.append(
            f"  Margin miss, model / blend / close: {_num(m['margin']['model'])} / "
            f"{_num(m['margin']['blend'])} / {_num(m['margin']['close'])}; total "
            f"{_num(m['total']['model'])} / {_num(m['total']['blend'])} / "
            f"{_num(m['total']['close'])}"
        )
        lines.append(
            f"  Outside the model's middle 90% (10% expected): margin {_pct(o['margin'])}, "
            f"total {_pct(o['total'])} of {o['games']}"
        )
        bins = ", ".join(
            f"{x['bin']} {_pct(x['chance'])} -> won {_pct(x['won'])} ({x['games']})"
            for x in part["calibration"]
            if x["games"]
        )
        lines.append(f"  Favourites by model chance: {bins or 'none'}")
        lines.append(
            "  Against the close: "
            + ", ".join(
                f"{k.replace('_', ' ')} {_record_text(v)}" for k, v in part["markets"].items()
            )
        )
        clv = "–" if c["margin"] is None else f"{c['margin']:+.2f}"
        lines.append(
            f"  Closing line moved the model's way {_pct(c['positive_share'])} of {c['closed']} "
            f"(mean {clv})"
        )
    return "\n".join(lines)


def send_month(diary, settings, label: str | None, now: datetime, sender) -> dict:
    """Build a month's check and hand it to ``sender``: what the endpoint and the CLI do."""
    start, end, month_label = month_bounds(label, now, settings.TZ)
    month = summary(diary, start, end, month_label)
    text = report(month, settings.TZ)
    sent = bool(sender.send(text))
    log.info(
        "monthly check for %s (%d graded) %s",
        month_label,
        month["games"],
        "sent" if sent else "not sent",
    )
    return {"month": month_label, "games": month["games"], "sent": sent, "text": text}


def send_remote(
    url: str, token: str, label: str | None = None, client: httpx.Client | None = None
) -> dict:
    """Ask the running service to build and send a month: the cron's job.

    The cron runs in its own container with no diary, so it posts to the service that
    has one. Returns the service's answer; raises on an HTTP error.
    """
    own = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        response = client.post(
            url.rstrip("/") + "/monthly-check",
            params={"month": label} if label else None,
            headers={"x-status-token": token, "accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()
    finally:
        if own:
            client.close()
