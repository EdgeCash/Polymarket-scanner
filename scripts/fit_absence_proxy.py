"""Fit the stand-in for an injury report where there is none: who sat out last time.

    python scripts/fit_absence_proxy.py --raw DIR [--sports cbb]

College publishes no injury report, so the nearest thing the scanner has is the
last box score: a regular who did not play his side's last game may well miss the
next. This script measures how often that is so and what it is worth. Every game
of the seasons in ``DIR`` (scripts/bulk_box_scores.py) is projected by the
scanner's own ratings from the season's earlier days with the season before as
its prior, and each side's regulars who sat out its last game are found and
valued exactly as scanner.injuries does before a game (``missed_last_game`` and
``absentees``, with the status weight and the coefficient set to one, so the
units are bare; in football the stand-in is the usual quarterback, when he did
not throw in the side's last game). Out come:

- how often such a man is still out tonight, which is the status's weight;
- the margin's miss fit on the two sides' units (away less home), which is the
  weight times the coefficient a unit, so the coefficient is that over the weight;
- the miss on the last season, which chose nothing, with and without it.

The running scanner never imports this script; it needs the ``train`` extras.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_injuries  # noqa: E402
import fit_ratings  # noqa: E402

import scanner.injuries as injuries  # noqa: E402
import scanner.projection as projection  # noqa: E402


def build_table(sport: str, records) -> tuple[list[dict], int, int]:
    by_season = defaultdict(list)
    for record in records:
        by_season[record.season].append(record)
    table: list[dict] = []
    sat = still = 0
    prior = None
    for season in sorted(by_season, key=lambda s: s or 0):
        games = by_season[season]
        team_rows: dict[str, list[dict]] = defaultdict(list)
        earlier = []
        ratings = rated_through = None
        for index, record in enumerate(games):
            day = record.date.date()
            if rated_through != day:
                ratings = projection.fit(earlier, sport, prior)
                rated_through = day
            home, away = ratings.team(record.home.team_id), ratings.team(record.away.team_id)
            if (
                home is not None
                and away is not None
                and min(home["games"], away["games"]) >= projection.MIN_GAMES
            ):
                p = projection.project(
                    ratings,
                    record.home.team_id,
                    record.away.team_id,
                    record.neutral,
                    None,
                    {"conference_game": record.conference_game},
                )
                units = {}
                for side, team in (("home", record.home.team_id), ("away", record.away.team_id)):
                    context = injuries.SideContext.build(team, team_rows[team], [], {})
                    entries = injuries.absentees(sport, None, context, has_report=False)
                    playing = {
                        q.player_id for q in record.players if q.team_id == team and q.played
                    }
                    sat += len(entries)
                    still += sum(1 for e in entries if e["player_id"] not in playing)
                    units[side] = sum(
                        (e["units"].get("production", 0.0) + e["units"].get("quarterback", 0.0))
                        * e["played_share"]
                        for e in entries
                    )
                table.append(
                    {
                        "season": season,
                        "index": index / len(games),
                        "miss": record.home_score - record.away_score - p["raw"]["margin"],
                        "lost": units["away"] - units["home"],
                    }
                )
            earlier.append(record)
            for row in fit_injuries.player_rows(record):
                team_rows[row["team_id"]].append(row)
        prior = fit_ratings.final_prior(games, sport, season)
        print(f"{sport} {season}: {len(games)} games, {len(table)} rated so far", flush=True)
    return table, sat, still


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["cbb"])
    args = parser.parse_args()
    for sport in args.sports:
        records = fit_injuries.load(args.raw, sport)
        if not records:
            print(f"{sport}: no history in {args.raw}")
            continue
        saved = injuries.PROXY_SPORTS
        injuries.PROXY_SPORTS = (*saved, sport)  # a sport under test stands in for itself
        try:
            table, sat, still = build_table(sport, records)
        finally:
            injuries.PROXY_SPORTS = saved
        last = max(r["season"] for r in table)
        train = [r for r in table if r["season"] != last]
        test = [r for r in table if r["season"] == last]
        weight = still / sat if sat else float("nan")
        x = np.array([r["lost"] for r in train])
        y = np.array([r["miss"] for r in train])
        slope = float((x @ y) / (x @ x))
        se = float(np.sqrt(((y - slope * x) ** 2).mean() / (x @ x)))
        xt = np.array([r["lost"] for r in test])
        yt = np.array([r["miss"] for r in test])
        moved = sum(1 for r in table if r["lost"])
        print(f"\n{sport}: {len(table)} rated games, {moved} with a regular who sat out last time")
        print(f"  still out tonight: {still} of {sat} = {weight:.3f} (the status's weight)")
        print(f"  margin per unit sat out last time: {slope:.3f} (se {se:.3f}), fit before {last}")
        print(f"  so a unit actually lost is worth {slope / weight:.3f} (the coefficient)")
        print(
            f"  held out {last}: {np.sqrt(np.mean(yt**2)):.3f} -> "
            f"{np.sqrt(np.mean((yt - slope * xt) ** 2)):.3f}"
        )
        unit = "quarterback" if sport in injuries.FOOTBALL else "production"
        coefficient = injuries.RULES[sport].coefficients.get(unit, 0.0)
        shipped = injuries.STATUS_WEIGHT["missed_last_game"] * coefficient
        print(
            f"  shipped now: weight {injuries.STATUS_WEIGHT['missed_last_game']}, coefficient "
            f"{coefficient} a {unit} unit; held out "
            f"{np.sqrt(np.mean((yt - shipped * xt) ** 2)):.3f}"
        )


if __name__ == "__main__":
    main()
