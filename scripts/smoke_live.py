"""Live smoke test. Hits the real Polymarket US and ESPN services. Run by hand.

    python scripts/smoke_live.py [--all-games] [--section polymarket|scores|all]

Never run this in CI. It makes a few dozen requests, stays under the request
budget, and prints what it sees so the numbers can be checked against the apps.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta

from scanner.models import League
from scanner.polymarket import PolymarketReader, buy_levels, walk_book


def today_window(now: datetime) -> tuple[datetime, datetime]:
    start = now - timedelta(hours=6)
    end = now + timedelta(hours=30)
    return start, end


def smoke_polymarket(all_games: bool) -> int:
    reader = PolymarketReader()
    t0 = time.monotonic()
    leagues = reader.discover_leagues()
    print(f"leagues discovered: {leagues}")
    rule_checked = rule_ok = 0
    now = datetime.now(UTC)
    start, end = today_window(now)
    priced_games = 0
    for league, slug in leagues.items():
        games = reader.list_games(league, slug)
        todays = [g for g in games if g.start_time is not None and start <= g.start_time <= end]
        print(f"\n== {league.value.upper()} ({slug}): {len(games)} listed, {len(todays)} today")
        shown = games if all_games else todays
        for game in shown[:40]:
            when = game.start_time.strftime("%a %H:%M UTC") if game.start_time else "?"
            print(f"\n{game.title}  [{when}] live={game.live} ended={game.ended}")
            if not game.moneyline_slug:
                print("   no moneyline market")
                continue
            quotes = reader.quotes_for_game(game)
            for team in game.teams:
                q = quotes[team.abbreviation]
                side = "long " if team.is_long else "short"
                price = f"{q.buy_price:.4f}" if q.buy_price is not None else "none"
                print(
                    f"   buy {team.abbreviation:<5} ({side}) {price}  bid={q.best_bid} "
                    f"ask={q.best_ask} state={q.state}"
                )
                if q.buy_price is not None and q.best_bid is not None and q.best_ask is not None:
                    rule_checked += 1
                    expected = q.best_ask if team.is_long else 1 - q.best_bid
                    rule_ok += abs(expected - q.buy_price) < 1e-6
            book = reader.book(game.moneyline_slug)
            for team in game.teams:
                levels = buy_levels(book, team.is_long)
                if levels:
                    avail = walk_book(levels, levels[0].price + 0.01)
                    print(
                        f"   book {team.abbreviation:<5}: best {levels[0].price:.4f} x "
                        f"{levels[0].quantity:.0f}, ${avail.dollars:,.0f} within 1c"
                    )
            if game.totals:
                lines = ", ".join(f"{t.line}" for t in game.totals)
                print(f"   totals: {len(game.totals)} lines ({lines})")
                # Price the Over on the middle line only, to keep the request count low.
                mid = game.totals[len(game.totals) // 2]
                q = reader.over_quote(mid)
                price = f"{q.buy_price:.4f}" if q.buy_price is not None else "none"
                side = "long" if mid.over_is_long else "short"
                print(f"   over {mid.line}: buy {price} ({side} side)")
            priced_games += 1
    elapsed = time.monotonic() - t0
    rps = reader.request_count / elapsed if elapsed > 0 else 0
    print(
        f"\n{reader.request_count} requests in {elapsed:.1f}s ({rps:.2f}/s); "
        f"pricing rule held on {rule_ok}/{rule_checked} sides; {priced_games} games priced"
    )
    return 0 if rule_ok == rule_checked else 1


def smoke_scores() -> int:
    try:
        from scanner.scores import ScoreFeed
    except ImportError:
        print("scores.py not built yet (milestone 3)")
        return 0
    feed = ScoreFeed()
    for league in (League.NFL, League.CFB):
        states = feed.fetch(league)
        live = [s for s in states if s.status.value in ("live", "halftime")]
        print(f"\n== ESPN {league.value.upper()}: {len(states)} games, {len(live)} live")
        for s in states:
            if s.status.value in ("live", "halftime", "delayed", "unknown"):
                print(
                    f"   {s.away.abbreviation} {s.away_score} at {s.home.abbreviation} "
                    f"{s.home_score}  {s.status.value} Q{s.period} {s.clock_seconds}s "
                    f"poss={s.possession} down={s.down}&{s.distance} "
                    f"ytg={s.yards_to_endzone} to={s.home_timeouts}/{s.away_timeouts} "
                    f"espn_wp={s.espn_home_win_probability} {s.unknown_reason or ''}"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-games", action="store_true", help="price every listed game")
    parser.add_argument("--section", default="all", choices=("polymarket", "scores", "all"))
    args = parser.parse_args(argv)
    code = 0
    if args.section in ("polymarket", "all"):
        code |= smoke_polymarket(args.all_games)
    if args.section in ("scores", "all"):
        code |= smoke_scores()
    return code


if __name__ == "__main__":
    sys.exit(main())
