"""Fit what wind and cold do to a football total, against a weather-blind projection.

    python scripts/fit_weather.py --raw DIR [--holdout-from 2020]

nflverse's games file carries each NFL game's roof, temperature and sustained
wind (stadium reports, 94% of outdoor games since 2010). Every game is given a
weather-blind expected total from team ratings fit only on its season's earlier
games, as the scanner does, and the miss (real total less expected) is fit on
the conditions: points per mph of wind above a knee, points per degree of cold
below a knee, with domes and closed roofs at zero. Forms are compared on
seasons held out, so an effect ships only when it lowers the miss on games it
was not fit to. The sample mean of the fitted effect is the baseline: a game
is adjusted by its effect less the baseline, since the ratings already carry
average conditions, so a dome game moves up a little and a gale moves down.

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

FORMS = {
    "wind from 0": {"wind_knee": 0.0, "cold_knee": None},
    "wind over 5": {"wind_knee": 5.0, "cold_knee": None},
    "wind over 10": {"wind_knee": 10.0, "cold_knee": None},
    "wind over 10, cold under 45": {"wind_knee": 10.0, "cold_knee": 45.0},
    "wind over 10, cold under 32": {"wind_knee": 10.0, "cold_knee": 32.0},
    "wind over 5, cold under 45": {"wind_knee": 5.0, "cold_knee": 45.0},
}


def load(raw: Path) -> pd.DataFrame:
    g = pd.read_csv(raw / "nfl_games.csv")
    g = g[(g.game_type == "REG") & (g.season >= 2010) & g.home_score.notna()].copy()
    games = pd.DataFrame(
        {
            "id": g.game_id,
            "season": g.season,
            "date": g.gameday,
            "neutral": g.location.ne("Home"),
            "home": g.home_team,
            "away": g.away_team,
            "home_reg": g.home_score.astype(int),
            "away_reg": g.away_score.astype(int),
            "home_final": g.home_score.astype(int),
            "away_final": g.away_score.astype(int),
            "ot": g.overtime.astype(int),
            "roof": g.roof,
            "temp": g.temp,
            "wind": g.wind,
            "total_line": g.total_line,
        }
    ).reset_index(drop=True)
    rated = history.rate_before(games, "nfl")
    rated["resid"] = rated.home_final + rated.away_final - rated.exp_total
    rated["indoors"] = rated.roof.isin(["dome", "closed"])
    # An outdoor game with no report is left out of the fit; indoors counts as calm.
    rated = rated[rated.indoors | (rated.wind.notna() & rated.temp.notna())]
    return rated


def features(frame: pd.DataFrame, wind_knee: float, cold_knee: float | None) -> np.ndarray:
    wind = np.where(frame.indoors, 0.0, np.maximum(frame.wind.fillna(0) - wind_knee, 0.0))
    cols = [np.ones(len(frame)), wind]
    if cold_knee is not None:
        cold = np.where(frame.indoors, 0.0, np.maximum(cold_knee - frame.temp.fillna(cold_knee), 0))
        cols.append(cold)
    return np.column_stack(cols)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--holdout-from", type=int, default=2020)
    args = parser.parse_args()
    rated = load(args.raw)
    train = rated[rated.season < args.holdout_from]
    test = rated[rated.season >= args.holdout_from]
    base = float(np.sqrt(np.mean(test.resid**2)))
    print(
        f"{len(rated)} games ({int(rated.indoors.sum())} indoors); fit on {len(train)}, "
        f"held out {len(test)} from {args.holdout_from}; held-out total miss with no "
        f"weather: {base:.3f}"
    )
    print(f"{'form':30} {'per mph':>8} {'per deg':>8} {'baseline':>9} {'held-out':>9} {'gain':>7}")
    held_out: dict[str, float] = {}
    for name, form in FORMS.items():
        x_train = features(train, **form)
        beta, *_ = np.linalg.lstsq(x_train, train.resid.to_numpy(), rcond=None)
        baseline = float((x_train[:, 1:] @ beta[1:]).mean())  # what the ratings already carry
        x_test = features(test, **form)
        adjusted = test.resid.to_numpy() - (x_test[:, 1:] @ beta[1:] - baseline)
        held_out[name] = float(np.sqrt(np.mean(adjusted**2)))
        per_deg = f"{beta[2]:+8.3f}" if len(beta) > 2 else f"{'-':>8}"
        print(
            f"{name:30} {beta[1]:+8.3f} {per_deg} {baseline:+9.2f} {held_out[name]:9.3f} "
            f"{base - held_out[name]:+7.3f}"
        )
    # The best form on every season, for the numbers that ship.
    name = min(held_out, key=held_out.get)
    form = FORMS[name]
    x_all = features(rated, **form)
    beta, *_ = np.linalg.lstsq(x_all, rated.resid.to_numpy(), rcond=None)
    baseline = float((x_all[:, 1:] @ beta[1:]).mean())
    cold = f", per degree {beta[2]:+.3f}" if len(beta) > 2 else ""
    print(
        f"\nbest held out: {name}; fit on every season: per mph {beta[1]:+.3f}{cold}, "
        f"baseline {baseline:+.2f} (wind knee {form['wind_knee']:g}, cold knee {form['cold_knee']})"
    )
    # How much the close already prices: the same fit on the miss against the closing total.
    close = rated[rated.total_line.notna()].copy()
    close["resid"] = close.home_final + close.away_final - close.total_line
    beta_close, *_ = np.linalg.lstsq(features(close, **form), close.resid.to_numpy(), rcond=None)
    print(
        f"the close still misses: per mph {beta_close[1]:+.3f}"
        + (f", per degree {beta_close[2]:+.3f}" if len(beta_close) > 2 else "")
    )


if __name__ == "__main__":
    main()
