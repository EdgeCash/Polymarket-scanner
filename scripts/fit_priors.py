"""Fit each sport's prior toward last season and its uncertainty numbers on history.

    python scripts/fit_priors.py --raw DIR --sports cbb nba nhl nfl cfb

``--raw`` holds the season files ``scripts/build_priors.py`` writes (``<sport>_<season>.jsonl``,
every finished game with its date, the two ESPN team ids and the score); two full
seasons at least, since each season is rated with the one before it as the prior.

For each pair of seasons and each (r, k) on a grid, every game is rated as the
scanner rates it, on points alone: ratings fit on the season's earlier days,
shrunk toward r times last season's final rating with k pseudo-games (toward
average at r = 0, as the scanner did before). Out come the root-mean-square miss
of the margin over the season and over its first 45 days, and from how the
squared miss grows with 2(1/(games + k) + 1/(games + k)) the intercept (the
model's miss once its ratings have settled) and the slope (``performance_sd``
squared, the scatter of one performance about the ratings). The recommendation
is the (r, k) with the smallest miss; ``projection.TUNING`` holds what was chosen
and why. The running scanner never imports this script; it needs the ``train``
extras.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.projection import tuning_for  # noqa: E402

MIN_GAMES = 3
EARLY_DAYS = 45


def load(raw: Path, sport: str) -> pd.DataFrame:
    rows = []
    for path in sorted(raw.glob(f"{sport}_*.jsonl")):
        for line in open(path):
            g = json.loads(line)
            rows.append(
                dict(
                    id=g["id"],
                    season=int(g["season"]),
                    date=g["date"],
                    neutral=g["neutral"],
                    home=str(g["home"]["id"]),
                    away=str(g["away"]["id"]),
                    hf=g["home"]["score"],
                    af=g["away"]["score"],
                )  # fmt: skip
            )
    df = pd.DataFrame(rows).drop_duplicates("id")
    df["date"] = pd.to_datetime(df["date"])
    df["day"] = df.groupby("season")["date"].transform(lambda s: (s - s.min()).dt.days)
    return df.sort_values("date").reset_index(drop=True)


def fit(games, teams, pseudo, hfa, prior=None):
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    oi, di, ht, pts = [], [], [], []
    for g in games.itertuples():
        h = 0.0 if g.neutral else hfa / 2
        hi, ai = index[g.home], index[g.away]
        oi += [hi, ai]
        di += [ai, hi]
        ht += [h, -h]
        pts += [float(g.hf), float(g.af)]
    oi, di = np.array(oi, dtype=int), np.array(di, dtype=int)
    ht, pts = np.array(ht), np.array(pts)
    league = pts.mean() if len(pts) else 0.0
    resid = pts - league - ht
    a = np.zeros((2 * n, 2 * n))
    b = np.zeros(2 * n)
    np.add.at(a, (oi, oi), 1.0)
    np.add.at(a, (n + di, n + di), 1.0)
    np.add.at(a, (oi, n + di), 1.0)
    np.add.at(a, (n + di, oi), 1.0)
    np.add.at(b, oi, resid)
    np.add.at(b, n + di, resid)
    a[np.diag_indices(2 * n)] += pseudo
    if prior is not None:
        b = b + pseudo * prior
    x = np.linalg.solve(a, b)
    played = np.zeros(n)
    np.add.at(played, oi, 1)
    return league, x[:n], x[n:], played, index


def walk(sg, teams, pseudo, hfa, prior):
    """Each game's projected margin/total and both sides' game counts, from earlier days only."""
    out = {}
    for day, todays in sg.groupby("date", sort=True):
        earlier = sg[sg.date < day]
        if len(earlier) == 0 and prior is None:
            continue
        league, off, dfn, played, index = fit(earlier, teams, pseudo, hfa, prior)
        for g in todays.itertuples():
            hi, ai = index[g.home], index[g.away]
            if prior is None and (played[hi] < MIN_GAMES or played[ai] < MIN_GAMES):
                continue
            h = 0.0 if g.neutral else hfa / 2
            hp = league + off[hi] + dfn[ai] + h
            ap = league + off[ai] + dfn[hi] - h
            out[g.id] = (hp - ap, hp + ap, played[hi], played[ai])
    return out


def prior_vector(prev, teams, r, hfa, unknown_q=0.1):
    pteams = sorted(set(prev.home) | set(prev.away))
    league, off, dfn, played, pindex = fit(prev, pteams, 2.0, hfa)
    ok = [t for t in pteams if played[pindex[t]] >= 10]
    net = np.array([off[pindex[t]] - dfn[pindex[t]] for t in ok])
    low = np.quantile(net, unknown_q)
    prior = np.zeros(2 * len(teams))
    known = 0
    for j, t in enumerate(teams):
        if t in pindex and played[pindex[t]] >= 10:
            prior[j] = r * off[pindex[t]]
            prior[len(teams) + j] = r * dfn[pindex[t]]
            known += 1
        else:
            prior[j] = r * low / 2
            prior[len(teams) + j] = -r * low / 2
    return prior, known


def run(raw: Path, sport: str) -> None:
    df = load(raw, sport)
    T = tuning_for(sport)
    hfa = T.hfa
    seasons = sorted(df.season.unique())
    print(
        f"\n## {sport}: {len(df)} games, seasons {seasons}, hfa {hfa}, "
        f"k now {T.pseudo_games}, sigma now {T.sigma}",
        flush=True,
    )
    full = [s for s in seasons if len(df[df.season == s]) > 0.6 * df.groupby("season").size().max()]
    tests = [(p, c) for p, c in zip(seasons, seasons[1:], strict=False) if p in full and c in full]
    grid_r = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9]
    grid_k = [0.5, 1, 2, 3, 4, 6, 8, 12] if sport in ("cfb", "nfl") else [2, 4, 6, 8, 12, 16]
    rows = []
    for r in grid_r:
        for k in grid_k:
            if r == 0.0 and k != T.pseudo_games:
                continue
            e_all, e_early, u_all = [], [], []
            for p, c in tests:
                sg = df[df.season == c]
                teams = sorted(set(sg.home) | set(sg.away))
                prior = None if r == 0.0 else prior_vector(df[df.season == p], teams, r, hfa)[0]
                w = walk(sg, teams, k, hfa, prior)
                for g in sg.itertuples():
                    if g.id not in w:
                        continue
                    m, t, nh, na = w[g.id]
                    if nh < MIN_GAMES or na < MIN_GAMES:
                        continue
                    err = (g.hf - g.af) - m
                    e_all.append(err)
                    u_all.append(2 * (1 / (nh + k) + 1 / (na + k)))
                    if g.day < EARLY_DAYS:
                        e_early.append(err)
            e_all, e_early, u_all = np.array(e_all), np.array(e_early), np.array(u_all)
            rm, re = math.sqrt(np.mean(e_all**2)), math.sqrt(np.mean(e_early**2))
            # squared error on u: intercept = sigma_game^2, slope = s^2
            slope, icept = np.polyfit(u_all, e_all**2, 1)
            rows.append((r, k, len(e_all), rm, re, icept, slope))
            print(
                f"  r={r:.1f} k={k:4.1f} n={len(e_all):5d} rmse {rm:6.2f} early {re:6.2f}  "
                f"sigma_game {math.sqrt(max(icept, 1)):5.2f} s {math.sqrt(max(slope, 0)):5.2f}",
                flush=True,
            )
    best = min(rows, key=lambda x: x[3])
    print(
        f"{sport}: best r={best[0]} k={best[1]} rmse {best[3]:.2f} early {best[4]:.2f} "
        f"sigma_game {math.sqrt(max(best[5], 1)):.2f} s {math.sqrt(max(best[6], 0)):.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["cbb", "nba", "nhl", "nfl", "cfb"])
    args = parser.parse_args()
    for sport in args.sports:
        run(args.raw, sport)


if __name__ == "__main__":
    main()
