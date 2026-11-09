"""Fit what rest does to a game: the back-to-back in the NBA and the NHL.

    python scripts/fit_rest.py --raw DIR [--sports nba nhl] [--holdout-from 2024]

Every game is given a rest-blind expected margin and total from team ratings
fit only on its season's earlier games, as the scanner does (shots included in
hockey), and each side's rest is counted from the season's whole schedule: the
days since its last game by the local date (a 7 pm Eastern tip is the next day
in UTC, so the start time is converted). The misses are fit on whether each
side is on a back-to-back (played the day before) and on a long rest (three
days or more), for the margin (home less away, so a home back-to-back shows as
a negative number) and for the total. Forms are compared on the seasons held
out, so an effect ships only when it lowers the miss on games it was not fit
to. The scanner's own schedule gives it the same counts live.

The running scanner never imports this script; it needs the ``train`` extras.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_history as history  # noqa: E402

LONG_REST = 3  # days since the last game that count as a long rest
FORMS = {
    "back-to-back, one coefficient": ("b2b_edge",),  # away's less home's: the rested side's edge
    "back-to-back only": ("home_b2b", "away_b2b"),
    "back-to-back and long rest": ("home_b2b", "away_b2b", "home_long", "away_long"),
    "rest difference": ("rest_diff",),
}


def with_rest(games: pd.DataFrame, dates: dict[str, str]) -> pd.DataFrame:
    """fetch_history's rest counts, with the fitted forms' columns."""
    games = history.with_rest(games, dates)
    games["home_b2b"] = (games.home_rest == 1).astype(float)
    games["away_b2b"] = (games.away_rest == 1).astype(float)
    games["home_long"] = (games.home_rest >= LONG_REST).astype(float)
    games["away_long"] = (games.away_rest >= LONG_REST).astype(float)
    games["rest_diff"] = (games.home_rest.clip(upper=4) - games.away_rest.clip(upper=4)).fillna(0)
    games["b2b_edge"] = games.away_b2b - games.home_b2b
    return games


def fit_form(train, test, columns, target):
    x_train = np.column_stack([np.ones(len(train))] + [train[c].to_numpy() for c in columns])
    beta, *_ = np.linalg.lstsq(x_train, train[target].to_numpy(), rcond=None)
    x_test = np.column_stack([np.ones(len(test))] + [test[c].to_numpy() for c in columns])
    adjusted = test[target].to_numpy() - (x_test[:, 1:] @ beta[1:])
    return beta, float(np.sqrt(np.mean(adjusted**2)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["nba", "nhl"])
    parser.add_argument("--holdout-from", type=int, default=2024)
    args = parser.parse_args()
    for sport in args.sports:
        games = history.load_games(args.raw, sport)
        rated = history.graded_subset(args.raw, sport, history.rate_before(games, sport))
        rated = with_rest(rated, history.local_dates(args.raw, sport))
        rated = rated[rated.home_rest.notna() & rated.away_rest.notna()]
        rated["margin_resid"] = rated.home_final - rated.away_final - rated.exp_margin
        rated["total_resid"] = rated.home_final + rated.away_final - rated.exp_total
        train = rated[rated.season < args.holdout_from]
        test = rated[rated.season >= args.holdout_from]
        print(
            f"## {sport}: {len(rated)} games with both rests known; home on a back-to-back "
            f"{rated.home_b2b.mean() * 100:.1f}%, away {rated.away_b2b.mean() * 100:.1f}%; "
            f"fit on {len(train)}, held out {len(test)} from {args.holdout_from}"
        )
        for target in ("margin_resid", "total_resid"):
            base = float(np.sqrt(np.mean(test[target] ** 2)))
            print(f"  {target.replace('_resid', '')}: held-out miss with no rest {base:.3f}")
            for name, columns in FORMS.items():
                beta, rmse = fit_form(train, test, columns, target)
                coefs = ", ".join(f"{c} {b:+.2f}" for c, b in zip(columns, beta[1:], strict=True))
                print(f"    {name:28} {coefs:60} held-out {rmse:.3f} gain {base - rmse:+.3f}")
        # Means by situation, the plain view.
        for label, mask in (
            ("home on a back-to-back, away rested", (rated.home_b2b == 1) & (rated.away_b2b == 0)),
            ("away on a back-to-back, home rested", (rated.away_b2b == 1) & (rated.home_b2b == 0)),
            ("both on a back-to-back", (rated.home_b2b == 1) & (rated.away_b2b == 1)),
            ("neither", (rated.home_b2b == 0) & (rated.away_b2b == 0)),
        ):
            part = rated[mask]
            print(
                f"  {label:40} n={len(part):5d} margin miss {part.margin_resid.mean():+.2f} "
                f"total miss {part.total_resid.mean():+.2f}"
            )
        print()


if __name__ == "__main__":
    main()
