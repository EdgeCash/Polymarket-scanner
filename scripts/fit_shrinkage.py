"""Fit how hard each sport's team ratings should be shrunk toward average, and its home edge.

    python scripts/fit_shrinkage.py --raw DIR [--sports cfb nhl] [--grid 1 2 4 8 16]
    python scripts/fit_shrinkage.py --raw DIR --sports cfb --hfa-grid 1.5 2 2.5 3

The scanner rates a team by its residual over (games + pseudo_games): the
pseudo-games are the shrinkage (``SportTuning.pseudo_games``). Too few and the
ratings swing on a handful of results, so the projected margin overshoots the
real one; too many and they are timid, so it undershoots. Each game here is
rated only on its season's earlier games, as the scanner sees it, with the
scanner's own half-score, half-volume performance where the volume stat has
been fetched (fetch_history.py --volumes), for every shrinkage in the grid.

For each: the slope of the real margin on the projected one (1 when the
projection is as bold as it should be; early-season games separately, where
shrinkage matters most), the root-mean-square miss of the margin and the total,
and the Brier score of the home win chance under a normal of that miss. The
recommendation is the shrinkage with the smallest margin miss.

The running scanner never imports this script; it needs the ``train`` extras.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_history as history  # noqa: E402

from scanner.projection import tuning_for  # noqa: E402

DEFAULT_GRID = (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0)
EARLY = 0.33  # the share of a season counted as early


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def home_residuals(rated) -> dict[int, tuple[float, int]]:
    """Mean real-less-projected margin in home games, by season (0 when the edge is right)."""
    home = rated[~rated.neutral.astype(bool)] if "neutral" in rated else rated
    out = {}
    for season, part in home.groupby("season"):
        residual = (part.home_final - part.away_final - part.exp_margin).mean()
        out[int(season)] = (float(residual), len(part))
    return out


def evaluate(rated, sport: str) -> dict:
    margin = (rated.home_final - rated.away_final).to_numpy(dtype=float)
    total = (rated.home_final + rated.away_final).to_numpy(dtype=float)
    exp_m, exp_t = rated.exp_margin.to_numpy(), rated.exp_total.to_numpy()
    slope, intercept = np.polyfit(exp_m, margin, 1)
    frac = rated.groupby("season").date.rank(pct=True).to_numpy()
    early = frac <= EARLY
    early_slope = (
        np.polyfit(exp_m[early], margin[early], 1)[0] if early.sum() > 50 else float("nan")
    )
    rmse_m = math.sqrt(np.mean((margin - exp_m) ** 2))
    rmse_t = math.sqrt(np.mean((total - exp_t) ** 2))
    decided = margin != 0
    p_home = np.array([_norm_cdf(m / rmse_m) for m in exp_m[decided]])
    brier = float(np.mean((p_home - (margin[decided] > 0)) ** 2))
    return {
        "games": len(rated),
        "slope": slope,
        "early_slope": early_slope,
        "intercept": intercept,
        "rmse_margin": rmse_m,
        "rmse_total": rmse_t,
        "brier": brier,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["cfb", "nhl"])
    parser.add_argument("--grid", nargs="+", type=float, default=list(DEFAULT_GRID))
    parser.add_argument(
        "--hfa-grid", nargs="+", type=float, default=None, help="fit the home edge instead"
    )
    args = parser.parse_args()
    for sport in args.sports:
        games = history.load_games(args.raw, sport)
        volume = "home_vol" in games and games.home_vol.notna().mean() > 0.5
        tuning = tuning_for(sport)
        print(
            f"## {sport}: {len(games)} games, {'with' if volume else 'without'} the volume half "
            f"(now pseudo_games={tuning.pseudo_games:g}, hfa={tuning.hfa:g})",
            flush=True,
        )
        fitting = "hfa" if args.hfa_grid else "pseudo"
        grid = args.hfa_grid or args.grid
        print(f"{fitting:>6}  games   slope  early   icept  rmse_m  rmse_t   brier")
        best = None
        for value in grid:
            rated = history.rate_before(
                games,
                sport,
                tuning.pseudo_games if fitting == "hfa" else value,
                value if fitting == "hfa" else None,
            )
            rated = history.graded_subset(args.raw, sport, rated)
            e = evaluate(rated, sport)
            print(
                f"{value:6g} {e['games']:6d}  {e['slope']:6.3f} {e['early_slope']:6.3f} "
                f"{e['intercept']:+7.2f}  {e['rmse_margin']:6.3f}  {e['rmse_total']:6.3f}  "
                f"{e['brier']:.4f}",
                flush=True,
            )
            if best is None or e["rmse_margin"] < best[1]["rmse_margin"]:
                best = (value, e)
        print(
            f"{sport}: smallest margin miss at {fitting}={best[0]:g} "
            f"(slope {best[1]['slope']:.3f}, early {best[1]['early_slope']:.3f}, "
            f"intercept {best[1]['intercept']:+.2f})",
            flush=True,
        )
        if fitting == "hfa":
            rated = history.graded_subset(args.raw, sport, history.rate_before(games, sport))
            by_season = home_residuals(rated)
            print(
                f"{sport}: mean home residual at hfa={tuning.hfa:g} by season: "
                + ", ".join(f"{y} {r:+.2f} ({n})" for y, (r, n) in by_season.items()),
                flush=True,
            )
        print(flush=True)


if __name__ == "__main__":
    main()
