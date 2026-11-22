"""Fit what the goalie in net is worth: the numbers in scanner/goalies.py.

    python scripts/fit_goalies.py --raw DIR [--holdout-share 0.3]

The history is the NHL seasons scripts/fetch_box_scores.py gathered. Every game is
given an expected margin from team ratings fit only on its season's earlier games,
with and without the scanner's back-to-back adjustment, and the miss is fit on
three things, each side's less the other's:

- the goaltending edge as scanner.goalies reckons it before the game: the expected
  save rate of whoever is likely to start (by recent starts and by the
  back-to-back rule; history cannot say who was hurt on the night, so the fit goes
  without the injury report the running scanner also reads), less the side's
  usual, times the shots it allows;
- the same edge for the goalie who did start, which nobody knows beforehand: the
  ceiling on what a confirmed starter would be worth;
- the back-to-back itself, fit beside the edge, to see how much of the tired
  side's loss was its second goalie all along.

A goalie's save rate carries his earlier seasons at scanner.goalies.CARRY, as the
running scanner's does from scanner/goalie_priors.json, so the first season in the
history is only the memory for the next and is not scored. It also counts who
starts when (the repeat rate on a back-to-back). Forms are compared on the last
share of each season, held out. The running scanner never
imports this script; it needs the ``train`` extras.
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

import scanner.goalies as goalies  # noqa: E402
import scanner.projection as projection  # noqa: E402
from scanner.rest import BACK_TO_BACK_EDGE, days_since_last  # noqa: E402


def build_table(records) -> tuple[list[dict], dict]:
    by_season = defaultdict(list)
    for record in records:
        by_season[record.season].append(record)
    table: list[dict] = []
    counts = {"b2b": 0, "b2b_repeat": 0, "rested": 0, "rested_repeat": 0}
    career: dict[str, tuple[float, float]] = {}  # earlier seasons, faded among themselves
    first = min(by_season, key=lambda s: s or 0)
    for season in sorted(by_season, key=lambda s: s or 0):
        games = by_season[season]
        schedule = defaultdict(list)
        for g in games:
            schedule[g.home.team_id].append(g.date)
            schedule[g.away.team_id].append(g.date)
        goalie_rows: dict[str, list[dict]] = defaultdict(list)
        every_goalie_row: list[dict] = []
        earlier = []
        ratings = rated_through = None
        for index, record in enumerate(games):
            day = record.date.date()
            if rated_through != day:
                ratings = projection.fit(earlier, "nhl")
                rated_through = day
            league = goalies.league_save_rate(every_goalie_row)
            sides = {}
            for side, team in (("home", record.home), ("away", record.away)):
                played = [
                    p
                    for p in record.players
                    if p.team_id == team.team_id and p.position == goalies.GOALIE and p.played
                ]
                state = goalies.team_goalies(goalie_rows[team.team_id], team.team_id, career)
                rest = days_since_last(record.date, schedule[team.team_id])
                b2b = rest == 1
                before = goalies.expected_edge(state, league, b2b)
                actual = None
                if played and state.games >= goalies.MIN_TEAM_GAMES:
                    starter = max(played, key=lambda p: p.stats.get("toi") or 0).player_id
                    usual = (
                        sum(goalies.quality(state, pid, league) for pid in state.starts)
                        / state.games
                    )
                    actual = (
                        goalies.quality(state, starter, league) - usual
                    ) * state.shots_per_game
                    if state.starts:
                        key = "b2b" if b2b else "rested"
                        counts[key] += 1
                        counts[f"{key}_repeat"] += int(state.starts[-1] == starter)
                sides[side] = {"before": before, "actual": actual, "b2b": b2b}
            home, away = ratings.team(record.home.team_id), ratings.team(record.away.team_id)
            rated = (
                home is not None
                and away is not None
                and min(home["games"], away["games"]) >= projection.MIN_GAMES
            )
            known = sides["home"]["actual"] is not None and sides["away"]["actual"] is not None
            # The first season has no earlier one to carry: it is only the next one's memory.
            if rated and known and (season != first or len(by_season) == 1):
                p = projection.project(
                    ratings, record.home.team_id, record.away.team_id, record.neutral, None, None
                )
                b2b_edge = int(sides["away"]["b2b"]) - int(sides["home"]["b2b"])
                before = (sides["home"]["before"] or {}).get("edge", 0.0) - (
                    sides["away"]["before"] or {}
                ).get("edge", 0.0)
                table.append(
                    {
                        "season": season,
                        "index": index / len(games),
                        "miss": record.home_score - record.away_score - p["raw"]["margin"],
                        "b2b": float(b2b_edge),
                        "before": before,
                        "actual": sides["home"]["actual"] - sides["away"]["actual"],
                    }
                )
            earlier.append(record)
            for row in fit_injuries.player_rows(record):
                if row["position"] == goalies.GOALIE:
                    goalie_rows[row["team_id"]].append(row)
                    every_goalie_row.append(row)
        career = season_career(every_goalie_row, career)
        print(f"nhl {season}: {len(games)} games, {len(table)} rated so far", flush=True)
    return table, counts


def season_career(rows: list[dict], before: dict) -> dict[str, tuple[float, float]]:
    """Every goalie's saves and shots after a season: the season's own, and what he had
    before it at the carry (so two seasons back counts a quarter)."""
    out = {pid: (goalies.CARRY * s, goalies.CARRY * n) for pid, (s, n) in before.items()}
    for row in rows:
        if row.get("played"):
            saves, shots = out.get(row["player_id"], (0.0, 0.0))
            out[row["player_id"]] = (
                saves + float(row["stats"].get("saves") or 0),
                shots + float(row["stats"].get("shots_against") or 0),
            )
    return out


def fit(train, test, columns, fixed=None):
    """Least squares through the origin; ``fixed`` holds coefficients not to fit."""
    fixed = fixed or {}

    def target(rows):
        y = np.array([r["miss"] for r in rows])
        for column, value in fixed.items():
            y = y - value * np.array([r[column] for r in rows])
        return y

    free = [c for c in columns if c not in fixed]
    if free:
        x = np.column_stack([[r[c] for r in train] for c in free])
        beta, *_ = np.linalg.lstsq(x, target(train), rcond=None)
        resid = target(train) - x @ beta
        se = np.sqrt(np.diag(np.linalg.inv(x.T @ x)) * resid.var(ddof=len(free)))
        held = target(test) - np.column_stack([[r[c] for r in test] for c in free]) @ beta
    else:
        beta, se, held = np.array([]), np.array([]), target(test)
    return dict(zip(free, zip(beta, se, strict=True), strict=True)), float(
        np.sqrt(np.mean(held**2))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--holdout-share", type=float, default=0.3)
    parser.add_argument("--prior-shots", type=float, default=None, help="try another shrinkage")
    args = parser.parse_args()
    if args.prior_shots:
        goalies.PRIOR_SHOTS = args.prior_shots
    records = fit_injuries.load(args.raw, "nhl")
    table, counts = build_table(records)
    train = [r for r in table if r["index"] < 1 - args.holdout_share]
    test = [r for r in table if r["index"] >= 1 - args.holdout_share]
    print(f"\n{len(table)} rated games, {len(train)} fit, {len(test)} held out")
    if counts["b2b"]:
        print(
            f"last game's starter starts again: {counts['b2b_repeat'] / counts['b2b']:.3f} of "
            f"{counts['b2b']} back-to-backs, {counts['rested_repeat'] / counts['rested']:.3f} of "
            f"{counts['rested']} rested games"
        )
    moved = [r for r in table if abs(r["before"]) > 0.005]
    print(
        f"the edge known beforehand is non-zero in {len(moved)} games, "
        f"mean size {np.mean([abs(r['before']) for r in moved]) if moved else 0:.3f} goals"
    )
    shipped_rest = BACK_TO_BACK_EDGE["nhl"]
    forms = [
        ("nothing", [], {}),
        (f"back-to-back at the shipped {shipped_rest}", ["b2b"], {"b2b": shipped_rest}),
        ("back-to-back, fit", ["b2b"], {}),
        ("back-to-back fit + the edge beforehand at 1", ["b2b", "before"], {"before": 1.0}),
        ("back-to-back fit + the edge beforehand, fit", ["b2b", "before"], {}),
        (
            f"back-to-back at {shipped_rest} + the edge beforehand at 1",
            ["b2b", "before"],
            {"b2b": shipped_rest, "before": 1.0},
        ),
        ("back-to-back fit + the actual starter's edge, fit", ["b2b", "actual"], {}),
    ]
    for name, columns, fixed in forms:
        coefs, held = fit(train, test, columns, fixed)
        every, _ = fit(table, table, columns, fixed)
        text = ", ".join(f"{c} {b:+.3f} (se {s:.3f})" for c, (b, s) in every.items())
        print(f"  {name:52} held out {held:.4f}   every game: {text or '-'}")


if __name__ == "__main__":
    main()
