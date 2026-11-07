"""Build the simulator's per-sport shape tables from game history.

    python scripts/build_sim_tables.py [--sports nfl cfb nba cbb] [--iterations 12]

Reads ``tests/fixtures/history/<sport>.csv.gz`` (see fetch_history.py) and writes the
sport's entry in ``scanner/sim_tables.json``, which ``scanner.grid`` reads:

- ``margin``: how much more or less often each regulation margin (by size)
  happens than the two bell curves say: football's 3, 7, 10 and 14, the tie
  after regulation that sends a game to overtime.
- ``score`` (football): the same for one team's regulation score (17, 20, 24...).
- ``margin_scale``, ``total_scale``: the bell curves' own widths as a share of
  the sport's, since piling chance onto a few numbers narrows the spread.
- ``overtime``: what an overtime adds to the winner and the loser, as counted.
- ``overtime_edge``: how much of the regulation favourite's edge carries into
  overtime, from how often favourites won the overtimes that happened.
- ``empty_net`` (hockey): how often a side leading by one, two or three before
  any empty-net goal went on to score one, from the league's goal records.

The fit is iterative proportional fitting: every game is laid out by the grid
at its expected margin and total with the current tables, the chances are added
up across games, and each table entry is multiplied by how much more often the
real games landed there than the grid said, until they agree. Counts are
shrunk toward no change (``PSEUDO``) so a rare score cannot carry a wild weight.
The widths are fit to the real misses around the expectation, so a table only
ever changes the shape, never how widely results spread.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner import grid as grid_module  # noqa: E402

HISTORY = ROOT / "tests" / "fixtures" / "history"
TABLES = ROOT / "scanner" / "sim_tables.json"
PSEUDO = 25.0  # games of shrinkage toward a weight of one
DAMP = 0.8  # each step goes this share of the way, in logs
# Per sport: the margins and the team scores whose weights are fit (the rest stay one),
# and how finely the expectations are rounded so games can share a grid.
SPEC = {
    "nfl": {"margins": 30, "scores": 50, "round_margin": 0.5, "round_total": 1.0},
    "cfb": {"margins": 40, "scores": 63, "round_margin": 1.0, "round_total": 2.0},
    "nba": {"margins": 3, "scores": None, "round_margin": 0.5, "round_total": 1.0},
    "cbb": {"margins": 3, "scores": None, "round_margin": 0.5, "round_total": 1.0},
    "nhl": {"margins": 6, "scores": 9, "round_margin": 0.1, "round_total": 0.2, "empty_net": 3},
}
OVERTIME_COVER = 0.995  # the overtime outcomes kept, by share of overtimes


def _set_tables(tables: dict) -> None:
    grid_module.TABLES = tables
    for cached in (
        grid_module.shape_for,
        grid_module.overtime_outcomes,
        grid_module._overtime_summary,
        grid_module._grid,
        grid_module._cells,
    ):
        cached.cache_clear()


def _lay_out(job):
    """One expectation's regulation and final picture under the given tables."""
    tables, sport, sigma, sigma_total, margins, scores, items = job
    _set_tables(tables)
    out = []
    for margin, total in items:
        reg = grid_module.regulation(sport, margin, total, sigma, sigma_total)
        g = grid_module.grid(sport, margin, total, sigma, sigma_total)
        by_margin = np.zeros(margins + 2)  # sizes 0..margins, then everything beyond
        by_score = np.zeros(scores + 2) if scores is not None else None
        ahead = behind = 0.0
        for (h, a), p in reg.items():
            by_margin[min(abs(h - a), margins + 1)] += p
            if by_score is not None:
                by_score[min(h, scores + 1)] += p / 2
                by_score[min(a, scores + 1)] += p / 2
            if h > a:
                ahead += p
            elif h < a:
                behind += p
        m_var = sum((m - margin) ** 2 * p for m, p in g.margin.items())
        t_var = sum((t - total) ** 2 * p for t, p in g.total.items())
        p_reg = ahead / (ahead + behind) if ahead + behind else 0.5
        out.append((by_margin, by_score, m_var, t_var, p_reg))
    return out


def _overtime_table(games: pd.DataFrame) -> list[list]:
    ot = games[games.ot == 1]
    level = ot.home_reg
    win = np.maximum(ot.home_final, ot.away_final) - level
    lose = np.minimum(ot.home_final, ot.away_final) - level
    counts = pd.Series(list(zip(win.astype(int), lose.astype(int), strict=True))).value_counts()
    keep, seen = [], 0
    for (w, lo), n in counts.items():
        if seen / counts.sum() >= OVERTIME_COVER:
            break
        keep.append([int(w), int(lo), round(n / counts.sum(), 5)])
        seen += n
    return keep


def _empty_net_rates(games: pd.DataFrame, leads: int) -> dict[str, float]:
    """How often a side ahead by 1..leads before any empty-net goal went on to score one,
    among games that ended regulation with it still ahead (a tie forced 6-on-5 is
    already in the bell curves, which hold the goals before the net emptied)."""
    pre = (games.home_reg - games.home_en) - (games.away_reg - games.away_en)
    leader_scored = ((pre > 0) & (games.home_en > 0)) | ((pre < 0) & (games.away_en > 0))
    rates = {}
    for k in range(1, leads + 1):
        chosen = pre.abs() == k
        rates[str(k)] = round(float(leader_scored[chosen].mean()), 4)
    return rates


def fit(sport: str, iterations: int, workers: int) -> dict:
    spec = SPEC[sport]
    games = pd.read_csv(HISTORY / f"{sport}.csv.gz")
    for column in ("home_reg", "away_reg", "home_final", "away_final", "ot"):
        games[column] = games[column].astype(int)
    if "home_en" in games:
        games["home_en"] = games["home_en"].astype(int)
        games["away_en"] = games["away_en"].astype(int)
    m_resid = (games.home_final - games.away_final) - games.exp_margin
    t_resid = (games.home_final + games.away_final) - games.exp_total
    # The widths are fit to the real misses around the expectation (any start would do).
    sigma, sigma_total = float(m_resid.std()), float(t_resid.std())
    margins, scores = spec["margins"], spec["scores"]
    emp_margin = np.bincount(
        np.minimum((games.home_reg - games.away_reg).abs(), margins + 1), minlength=margins + 2
    ).astype(float)
    emp_score = None
    if scores is not None:
        both = np.concatenate([games.home_reg.to_numpy(), games.away_reg.to_numpy()])
        emp_score = np.bincount(np.minimum(both, scores + 1), minlength=scores + 2) / 2.0
    n_games = len(games)
    keys = pd.DataFrame(
        {
            "m": (games.exp_margin / spec["round_margin"]).round() * spec["round_margin"],
            "t": (games.exp_total / spec["round_total"]).round() * spec["round_total"],
        }
    )
    combos = keys.value_counts().reset_index()
    combos.columns = ["m", "t", "n"]
    items = list(zip(combos.m.astype(float), combos.t.astype(float), strict=True))
    weights = combos.n.to_numpy(dtype=float)
    # Whether each game's expected favourite won its overtime (for the overtime edge).
    ot_games = games[(games.ot == 1) & (games.home_final != games.away_final)]
    ot_keys = keys.loc[ot_games.index]
    fav_home = ot_games.exp_margin > 0
    home_won = ot_games.home_final > ot_games.away_final
    favourite_won = (fav_home == home_won)[ot_games.exp_margin != 0]

    entry: dict = {
        "margin": {},
        "score": {},
        "margin_scale": 1.0,
        "total_scale": 1.0,
        "overtime": _overtime_table(games),
        "overtime_edge": 1.0,
    }
    if spec.get("empty_net"):
        entry["empty_net"] = _empty_net_rates(games, spec["empty_net"])
        print(f"{sport} empty net: {entry['empty_net']}", flush=True)
    chunks = max(1, math.ceil(len(items) / (workers * 4)))
    with ProcessPoolExecutor(workers) as pool:
        for step in range(iterations + 1):
            tables = {sport: entry}
            jobs = [
                (tables, sport, sigma, sigma_total, margins, scores, items[i : i + chunks])
                for i in range(0, len(items), chunks)
            ]
            laid = [row for part in pool.map(_lay_out, jobs) for row in part]
            pred_margin = sum(w * r[0] for w, r in zip(weights, laid, strict=True))
            pred_sd = math.sqrt(sum(w * r[2] for w, r in zip(weights, laid, strict=True)) / n_games)
            pred_tsd = math.sqrt(
                sum(w * r[3] for w, r in zip(weights, laid, strict=True)) / n_games
            )
            gap = np.abs(np.log((emp_margin[: margins + 1] + 1) / (pred_margin[: margins + 1] + 1)))
            print(
                f"{sport} step {step}: margin sd {pred_sd:.2f} (real {sigma:.2f}), "
                f"total sd {pred_tsd:.2f} (real {sigma_total:.2f}), "
                f"worst margin gap {gap.max():.3f}, level {pred_margin[0] / n_games:.4f} "
                f"(real {emp_margin[0] / n_games:.4f})",
                flush=True,
            )
            if step == iterations:
                break
            for k in range(margins + 1):
                ratio = (emp_margin[k] + PSEUDO) / (pred_margin[k] + PSEUDO)
                entry["margin"][str(k)] = entry["margin"].get(str(k), 1.0) * ratio**DAMP
            if scores is not None:
                pred_score = sum(w * r[1] for w, r in zip(weights, laid, strict=True))
                for s in range(scores + 1):
                    ratio = (emp_score[s] + PSEUDO) / (pred_score[s] + PSEUDO)
                    entry["score"][str(s)] = entry["score"].get(str(s), 1.0) * ratio**DAMP
            entry["margin_scale"] *= (sigma / pred_sd) ** DAMP
            entry["total_scale"] *= (sigma_total / pred_tsd) ** DAMP
            if step == iterations // 2 and len(favourite_won) >= 30:
                # The overtime edge, from the overtimes that happened: how far above a coin
                # flip the favourites did, against how far the grid had them in regulation.
                lookup = {(m, t): r[4] for (m, t), r in zip(items, laid, strict=True)}
                fav = ot_keys.loc[favourite_won.index]
                p_reg = [
                    lookup[(m, t)] if home else 1 - lookup[(m, t)]
                    for m, t, home in zip(fav.m, fav.t, fav_home.loc[fav.index], strict=True)
                ]
                expected = float(np.mean(p_reg)) - 0.5
                observed = float(favourite_won.mean()) - 0.5
                edge = min(max(observed / expected, 0.0), 1.0) if expected > 0 else 1.0
                entry["overtime_edge"] = round(edge, 3)
                print(
                    f"{sport} overtime: favourites won {favourite_won.mean():.3f} of "
                    f"{len(favourite_won)}, regulation edge {expected + 0.5:.3f}: edge {edge:.3f}",
                    flush=True,
                )
    entry["margin"] = {k: round(v, 4) for k, v in entry["margin"].items()}
    entry["score"] = {k: round(v, 4) for k, v in entry["score"].items()}
    entry["margin_scale"] = round(entry["margin_scale"], 4)
    entry["total_scale"] = round(entry["total_scale"], 4)
    if not entry["score"]:
        del entry["score"]
    entry["source"] = {
        "games": n_games,
        "seasons": f"{int(games.season.min())}-{int(games.season.max())}",
        "expected_from": str(games.exp_source.iloc[0]),
        "margin_sd": round(sigma, 2),
        "total_sd": round(sigma_total, 2),
        "built": datetime.now(UTC).date().isoformat(),
    }
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sports", nargs="+", default=list(SPEC))
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=TABLES, help="the tables file to update")
    args = parser.parse_args()
    for sport in args.sports:
        entry = fit(sport, args.iterations, args.workers)
        tables = json.loads(args.out.read_text()) if args.out.exists() else {}
        tables[sport] = entry
        args.out.write_text(json.dumps(tables, indent=1, sort_keys=True) + "\n")
        print(f"wrote {sport} to {args.out}", flush=True)


if __name__ == "__main__":
    main()
