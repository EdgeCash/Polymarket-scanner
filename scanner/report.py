"""The shadow report: what the scanner would have sent, read back from the diary."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scanner.card import basis_word
from scanner.diary import Diary


def shadow_report(diary: Diary, now: datetime, tz: str, days: int = 7) -> str:
    since = now - timedelta(days=days)
    zone = ZoneInfo(tz)
    alerts = diary.alerts_since(since)
    misses = diary.near_misses_by_reason(since)
    miss_split = diary.near_misses_by_type(since)
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
        f"winner {by_type.get('winner', 0)}, clinched over {by_type.get('clinched_over', 0)}, "
        f"period {by_type.get('period', 0)}; "
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
        for kind, title in (("winner", "winner"), ("clinched_over", "over"), ("period", "period")):
            reasons = miss_split.get(kind, {})
            if reasons:
                ranked = sorted(reasons.items(), key=lambda kv: -kv[1])
                lines.append(f"Near misses ({title}): " + ", ".join(f"{r} {n}" for r, n in ranked))
    else:
        lines.append("Near misses: none")
    obs = diary.observation_summary(since)
    if obs["rows"]:
        lines.append(
            f"Observation window (nothing sent): {obs['rows']} checks on {obs['games']} games, "
            f"{obs['picks']} would have alerted; graded {obs['graded']}: {obs['wins']}W "
            f"{obs['losses']}L {obs['ties']}T, ${obs['profit_per_100']:,.2f} per 100 contracts"
        )
    pg = diary.pregame_summary(since)
    if pg["gaps"]:
        sports = ", ".join(f"{k.upper()} {v}" for k, v in sorted(pg["by_sport"].items()))
        clv = "n/a" if pg["avg_clv"] is None else f"{pg['avg_clv'] * 100:+.1f}c"
        lines.append(
            f"Pre-game gaps (nothing sent): {pg['gaps']} ({sports}); graded {pg['graded']}: "
            f"{pg['wins']}W {pg['losses']}L {pg['pushes']}P, ${pg['profit_per_100']:,.2f} per "
            f"100 contracts; edge at the close {clv} over {pg['closed']} closed"
        )
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


def card_report(diary: Diary, settings) -> str:
    """The latest daily card and the whole record, as plain text."""
    zone = ZoneInfo(settings.CARD_TZ)
    key = diary.latest_card_date()
    card = diary.card(key) if key else None
    lines = []
    if card is None:
        lines.append(f"No daily card yet; the first locks at {settings.CARD_LOCK_TIME} {zone.key}.")
    else:
        locked = datetime.fromisoformat(card["locked_at"]).astimezone(zone)
        late = " (late)" if card["late"] else ""
        lines.append(
            f"Daily card {card['card_date']}, locked {locked.strftime('%a %b %-d %-I:%M %p')}{late}"
        )
        for r in card["rows"]:
            when = datetime.fromisoformat(r["kickoff"]).astimezone(zone).strftime("%a %-I:%M %p")
            chance = r["detail"].get("chance")
            sim = "" if chance is None else f", sim {chance * 100:.0f}%"
            edge = r["detail"].get("edge")
            rank_by = (
                f"conviction {r['conviction']:.2f}"
                if edge is None
                else f"edge {edge * 100:+.1f}, conviction {r['conviction'] * 100:+.1f}"
            )
            result = r["outcome"] or "open"
            if r["units"] is not None:
                result += f" {r['units']:+.2f}u"
            lines.append(
                f"#{r['rank']} {r['sport'].upper()} {r['away']} at {r['home']} {when}: "
                f"{r['side_label']} ({r['odds']:+d}), {basis_word(r)} {r['model_value']:.1f} "
                f"v book {r['book_value']:.1f}, {r['z']:.2f} sd{sim}, {rank_by} "
                f"-> {result}"
            )
    s = diary.card_summary(picks=settings.CARD_PICKS)
    if s["graded"]:
        roi = "n/a" if s["roi"] is None else f"{s['roi'] * 100:+.1f}%"
        clv = "n/a" if s["clv"] is None else f"{s['clv']:+.2f}"
        lines.append(
            f"Record over {s['days']} days: {s['wins']}W {s['losses']}L {s['pushes']}P, "
            f"{s['units']:+.2f} units, {roi} per pick, closing line {clv} toward the pick"
        )
        for rank, part in s["by_rank"].items():
            if part["graded"]:
                lines.append(
                    f"  rank {rank}: {part['wins']}W {part['losses']}L {part['pushes']}P, "
                    f"{part['units']:+.2f} units"
                )
    return "\n".join(lines)
