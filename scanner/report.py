"""The shadow report: what the scanner would have sent, read back from the diary."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scanner.diary import Diary


def shadow_report(diary: Diary, now: datetime, tz: str, days: int = 7) -> str:
    since = now - timedelta(days=days)
    zone = ZoneInfo(tz)
    alerts = diary.alerts_since(since)
    misses = diary.near_misses_by_reason(since)
    events = [
        e for e in diary.events(limit=500) if datetime.fromisoformat(e["created_at"]) >= since
    ]
    kinds = Counter(e["kind"] for e in events)

    stamp = now.astimezone(zone).strftime("%a %b %-d %-I:%M %p")
    lines = [f"Shadow report, last {days} days (to {stamp})"]
    by_type = Counter(a["alert_type"] for a in alerts)
    by_league = Counter(a["league"].upper() for a in alerts)
    sent = sum(1 for a in alerts if a["sent"])
    lines.append(
        f"Would-be alerts: {len(alerts)} ({sent} actually sent); "
        f"winner {by_type.get('winner', 0)}, clinched over {by_type.get('clinched_over', 0)}; "
        + ", ".join(f"{k} {v}" for k, v in sorted(by_league.items()))
    )
    graded = [a for a in alerts if a["outcome"] in ("win", "loss", "tie")]
    if graded:
        wins = sum(1 for a in graded if a["outcome"] == "win")
        profit = sum(100 * a["result_per_contract"] for a in graded)
        lines.append(
            f"Graded: {len(graded)}, wins {wins}, paper result ${profit:,.2f} per 100 contracts"
        )
    checked = [a for a in alerts if a["followup_30_at"] is not None]
    if checked:
        still = sum(
            1
            for a in checked
            if a["followup_30"] is not None and a["followup_30"] <= a["buy_price"] + 1e-9
        )
        lines.append(f"Price still there 30s later: {still} of {len(checked)}")
    for a in alerts[-10:]:
        when = datetime.fromisoformat(a["created_at"]).astimezone(zone).strftime("%a %-I:%M %p")
        lines.append(
            f"- {when} {a['league'].upper()} {a['away']} at {a['home']}: {a['pick']} "
            f"fair {a['fair_price'] * 100:.1f}c buy {a['buy_price'] * 100:.1f}c "
            f"edge {a['edge'] * 100:.1f}c "
            f"${a['dollars_available']:,.0f} -> {a['outcome'] or 'ungraded'}"
        )
    if misses:
        lines.append(
            "Near misses: "
            + ", ".join(f"{r} {n}" for r, n in sorted(misses.items(), key=lambda kv: -kv[1]))
        )
    else:
        lines.append("Near misses: none")
    lines.append(
        f"Feed failures: score {kinds.get('score_feed_failure', 0)}, "
        f"price {kinds.get('price_feed_failure', 0)}; "
        f"heartbeats {kinds.get('heartbeat', 0)}; paused {kinds.get('paused', 0)}"
    )
    lag = [e for e in events if e["kind"] == "score_lag"]
    if lag:
        lines.append(f"Score timing notes: {len(lag)} (see the diary's events table)")
    lines.append("ESPN delay and wrong matches are checked by hand from the diary; see README.")
    return "\n".join(lines)
