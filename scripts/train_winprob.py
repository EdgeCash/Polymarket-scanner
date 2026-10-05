"""Train the version-1 win probability model and save it as JSON.

    python scripts/train_winprob.py [--data-dir DIR] [--out scanner/winprob_model.json]

Data: nflverse play-by-play parquet files, one per season, from
https://github.com/nflverse/nflverse-data/releases/tag/pbp (the files the
``nflreadpy`` package downloads). They are fetched into ``--data-dir`` when
missing. The running scanner never imports this script or its dependencies.

Model: a logistic regression on hand-built, sign-symmetric features of the 4th
quarter game state, fitted with every coefficient constrained to be
non-negative so that, by construction, a bigger lead, less time for the leader
and having the ball never lower the leading team's price. An isotonic
(non-decreasing) calibration map fitted on separate seasons is applied on top.

Seasons: train 2014-2021, calibrate 2022-2023, hold out 2024-2025. The report
in docs/winprob_report.md and the numbers embedded in the model file come from
the held-out seasons only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scanner.winprob_features import FEATURE_NAMES, features  # noqa: E402

PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{year}.parquet"
)
TRAIN_SEASONS = list(range(2014, 2022))
CALIBRATION_SEASONS = [2022, 2023]
HOLDOUT_SEASONS = [2024, 2025]
COLUMNS = [
    "game_id",
    "season",
    "season_type",
    "home_team",
    "away_team",
    "posteam",
    "qtr",
    "game_seconds_remaining",
    "total_home_score",
    "total_away_score",
    "down",
    "ydstogo",
    "yardline_100",
    "posteam_timeouts_remaining",
    "defteam_timeouts_remaining",
    "spread_line",
    "result",
    "play_type",
]


def fetch(year: int, data_dir: Path) -> Path:
    path = data_dir / f"play_by_play_{year}.parquet"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"downloading {year} ...", flush=True)
        urllib.request.urlretrieve(PBP_URL.format(year=year), path)
    return path


def load_season(year: int, data_dir: Path) -> pd.DataFrame:
    df = pd.read_parquet(fetch(year, data_dir), columns=COLUMNS)
    df = df[(df["qtr"] == 4)]
    df = df[df["posteam"].notna() & df["result"].notna()]
    df = df[df["posteam"].isin(df["home_team"]) | df["posteam"].isin(df["away_team"])]
    df = df[(df["posteam"] == df["home_team"]) | (df["posteam"] == df["away_team"])]
    df = df[df["down"].between(1, 4) & df["ydstogo"].between(1, 50)]
    df = df[df["yardline_100"].between(1, 99)]
    df = df[df["posteam_timeouts_remaining"].between(0, 3)]
    df = df[df["defteam_timeouts_remaining"].between(0, 3)]
    df = df[df["game_seconds_remaining"].between(0, 900)]
    df = df[df["spread_line"].notna()]
    return df.reset_index(drop=True)


def rows_from(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Feature matrix, soft labels and season per row, from the HOME team's view."""
    home_has_ball = (df["posteam"] == df["home_team"]).to_numpy()
    diff = (df["total_home_score"] - df["total_away_score"]).to_numpy(dtype=float)
    secs = df["game_seconds_remaining"].to_numpy(dtype=float)
    down = df["down"].to_numpy(dtype=float)
    ydstogo = df["ydstogo"].to_numpy(dtype=float)
    ytg = df["yardline_100"].to_numpy(dtype=float)
    pos_to = df["posteam_timeouts_remaining"].to_numpy(dtype=float)
    def_to = df["defteam_timeouts_remaining"].to_numpy(dtype=float)
    home_to = np.where(home_has_ball, pos_to, def_to)
    away_to = np.where(home_has_ball, def_to, pos_to)
    # nflverse spread_line: positive means the home team was favoured by that much.
    home_adv = df["spread_line"].to_numpy(dtype=float)
    result = df["result"].to_numpy(dtype=float)
    label = np.where(result > 0, 1.0, np.where(result < 0, 0.0, 0.5))
    season = df["season"].to_numpy()

    rows = []
    for i in range(len(df)):
        rows.append(
            features(
                diff=diff[i],
                seconds_left=secs[i],
                has_ball=1 if home_has_ball[i] else -1,
                down=int(down[i]),
                distance=int(ydstogo[i]),
                yards_to_endzone=int(ytg[i]),
                own_timeouts=int(home_to[i]),
                opp_timeouts=int(away_to[i]),
                advantage_points=home_adv[i],
                is_home=1,
            )
        )
    return np.asarray(rows, dtype=float), label, season


def augment(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Add every row seen from the away team's side so the model is symmetric."""
    return np.vstack([x, -x]), np.concatenate([y, 1.0 - y])


def fit(x: np.ndarray, y: np.ndarray, l2: float = 1e-3) -> np.ndarray:
    n, k = x.shape

    def objective(w: np.ndarray) -> tuple[float, np.ndarray]:
        z = x @ w
        p = 1.0 / (1.0 + np.exp(-z))
        eps = 1e-12
        nll = -np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))
        grad = x.T @ (p - y) / n
        return nll + l2 * np.sum(w * w), grad + 2 * l2 * w

    result = minimize(
        objective,
        np.full(k, 0.1),
        jac=True,
        method="L-BFGS-B",
        bounds=[(0.0, None)] * k,
        options={"maxiter": 2000},
    )
    if not result.success:
        print("warning: optimiser did not report success:", result.message)
    return result.x


def predict_raw(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-(x @ w)))


def isotonic(p: np.ndarray, y: np.ndarray, knots: int = 200) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators on binned predictions. Returns (x knots, y knots)."""
    order = np.argsort(p)
    p, y = p[order], y[order]
    edges = np.linspace(0, len(p), knots + 1).astype(int)
    xs, ys, ws = [], [], []
    for a, b in zip(edges[:-1], edges[1:], strict=True):
        if b > a:
            xs.append(float(p[a:b].mean()))
            ys.append(float(y[a:b].mean()))
            ws.append(float(b - a))
    # PAVA
    blocks: list[list[float]] = []  # [x_sum_weighted, y_mean, weight, x_min, x_max]
    for xv, yv, wv in zip(xs, ys, ws, strict=True):
        blocks.append([xv * wv, yv, wv, xv, xv])
        while len(blocks) > 1 and blocks[-2][1] > blocks[-1][1]:
            b1 = blocks.pop()
            b0 = blocks.pop()
            weight = b0[2] + b1[2]
            blocks.append(
                [
                    b0[0] + b1[0],
                    (b0[1] * b0[2] + b1[1] * b1[2]) / weight,
                    weight,
                    min(b0[3], b1[3]),
                    max(b0[4], b1[4]),
                ]
            )
    kx = [b[0] / b[2] for b in blocks]
    ky = [b[1] for b in blocks]
    # Flat beyond the outermost knots, so interpolation covers [0, 1] without
    # ever extrapolating past what the calibration seasons showed.
    return [0.0, *kx, 1.0], [ky[0], *ky, ky[-1]]


def apply_calibration(p: np.ndarray, kx: list[float], ky: list[float]) -> np.ndarray:
    return np.interp(p, kx, ky)


def calibration_table(p: np.ndarray, y: np.ndarray) -> list[dict[str, float]]:
    bins = [0.0, 0.5, 0.8, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99, 0.995, 1.0001]
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:], strict=True):
        mask = (p >= lo) & (p < hi)
        if mask.sum() == 0:
            continue
        rows.append(
            {
                "from": lo,
                "to": min(hi, 1.0),
                "n": int(mask.sum()),
                "predicted": float(p[mask].mean()),
                "actual": float(y[mask].mean()),
            }
        )
    return rows


def bucket(p: np.ndarray, y: np.ndarray, lo: float) -> dict[str, float]:
    mask = p >= lo
    return {
        "threshold": lo,
        "n": int(mask.sum()),
        "predicted": float(p[mask].mean()) if mask.any() else float("nan"),
        "actual": float(y[mask].mean()) if mask.any() else float("nan"),
    }


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    eps = 1e-12
    return float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))


def write_report(path: Path, model: dict, table: list[dict], extra: dict) -> None:
    lines = [
        "# Win probability model, version 1: calibration report",
        "",
        f"Generated {model['trained_at']} by `scripts/train_winprob.py`.",
        "",
        "## What it is",
        "",
        "A logistic regression on sign-symmetric features of the 4th-quarter game state "
        "(score margin, margin over the square root of seconds left, one/two/three-score "
        "steps and their time interactions, possession with field position, down and "
        "distance, timeouts, the pre-game spread and home field). Every coefficient is "
        "constrained to be non-negative, so a bigger lead, less time for the leader and "
        "having the ball can never lower the leading team's price. An isotonic "
        "(non-decreasing) calibration map fitted on separate seasons is applied on top.",
        "",
        "Trained on NFL play-by-play from nflverse. College games use the same model with "
        "`CFB_EXTRA_MARGIN` subtracted from the fair price.",
        "",
        f"- Training seasons: {model['train_seasons']} ({extra['n_train']:,} plays, "
        "each seen from both sides)",
        f"- Calibration seasons: {model['calibration_seasons']} ({extra['n_cal']:,} plays)",
        f"- Held-out seasons: {model['holdout_seasons']} ({extra['n_test']:,} plays)",
        "",
        "## Held-out results (seasons never used for fitting or calibration)",
        "",
        f"- Brier score: {extra['brier']:.4f} (always-0.5 baseline 0.2500)",
        f"- Log loss: {extra['log_loss']:.4f}",
        "",
        "| Predicted at least | Plays | Mean predicted | Actual win rate | Gap |",
        "| --- | --- | --- | --- | --- |",
    ]
    for b in model["holdout"]["buckets"]:
        gap = b["actual"] - b["predicted"]
        lines.append(
            f"| {b['threshold']:.0%} | {b['n']:,} | {b['predicted']:.2%} | {b['actual']:.2%} "
            f"| {gap:+.2%} |"
        )
    gate = model["holdout"]["buckets"][2]
    lines += [
        "",
        "The merge gate from the brief: teams priced 95% or higher must actually have won "
        "within 1 percentage point of the predicted rate on held-out seasons. "
        f"Result: predicted {gate['predicted']:.2%}, actual {gate['actual']:.2%}, "
        f"gap {gate['actual'] - gate['predicted']:+.2%}.",
        "",
        "## Calibration table (held-out seasons)",
        "",
        "| Predicted range | Plays | Mean predicted | Actual win rate |",
        "| --- | --- | --- | --- |",
    ]
    for row in table:
        lines.append(
            f"| {row['from']:.1%} to {row['to']:.1%} | {row['n']:,} | {row['predicted']:.2%} "
            f"| {row['actual']:.2%} |"
        )
    lines += [
        "",
        "## Coefficients",
        "",
        "| Feature | Weight |",
        "| --- | --- |",
    ]
    for name, weight in zip(model["feature_names"], model["weights"], strict=True):
        lines.append(f"| `{name}` | {weight:.4f} |")
    lines += [
        "",
        "## Known limits",
        "",
        "- One row per play; plays inside one drive are correlated, so the effective "
        "sample is smaller than the play count.",
        "- Trained on NFL games only. College clock rules and scoring pace differ; the "
        "scanner subtracts `CFB_EXTRA_MARGIN` (default 1 cent) for college games and a "
        "college-specific model is a later upgrade.",
        "- Overtime is not priced. The scanner sends nothing in overtime.",
        "- The pre-game spread enters without a time interaction, so it keeps a small, "
        "constant influence through the 4th quarter.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.environ.get("NFLVERSE_DIR", "data/nflverse"))
    parser.add_argument("--out", default="scanner/winprob_model.json")
    parser.add_argument("--report", default="docs/winprob_report.md")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    frames = {
        year: load_season(year, data_dir)
        for year in TRAIN_SEASONS + CALIBRATION_SEASONS + HOLDOUT_SEASONS
    }
    train = pd.concat([frames[y] for y in TRAIN_SEASONS], ignore_index=True)
    cal = pd.concat([frames[y] for y in CALIBRATION_SEASONS], ignore_index=True)
    test = pd.concat([frames[y] for y in HOLDOUT_SEASONS], ignore_index=True)
    print(f"plays: train {len(train):,}  calibrate {len(cal):,}  holdout {len(test):,}")

    x_train, y_train, _ = rows_from(train)
    x_train, y_train = augment(x_train, y_train)
    weights = fit(x_train, y_train)
    for name, weight in zip(FEATURE_NAMES, weights, strict=True):
        print(f"  {name:<14} {weight:8.4f}")

    x_cal, y_cal, _ = rows_from(cal)
    x_cal, y_cal = augment(x_cal, y_cal)
    kx, ky = isotonic(predict_raw(x_cal, weights), y_cal)

    x_test, y_test, _ = rows_from(test)
    x_test, y_test = augment(x_test, y_test)
    p_raw = predict_raw(x_test, weights)
    p_cal = apply_calibration(p_raw, kx, ky)
    table = calibration_table(p_cal, y_test)
    buckets = [bucket(p_cal, y_test, lo) for lo in (0.90, 0.93, 0.95, 0.97, 0.99)]
    for b in buckets:
        print(
            f"  >= {b['threshold']:.0%}: n={b['n']:6d} "
            f"predicted {b['predicted']:.4f} actual {b['actual']:.4f}"
        )
    print(
        f"  brier {brier(p_cal, y_test):.4f} (raw {brier(p_raw, y_test):.4f})  "
        f"logloss {log_loss(p_cal, y_test):.4f}"
    )

    model = {
        "version": 1,
        "trained_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "nflverse play-by-play (github.com/nflverse/nflverse-data, release 'pbp')",
        "train_seasons": TRAIN_SEASONS,
        "calibration_seasons": CALIBRATION_SEASONS,
        "holdout_seasons": HOLDOUT_SEASONS,
        "feature_names": list(FEATURE_NAMES),
        "weights": [float(w) for w in weights],
        "calibration": {"x": [float(v) for v in kx], "y": [float(v) for v in ky]},
        "holdout": {
            "n": int(len(y_test)),
            "brier": brier(p_cal, y_test),
            "log_loss": log_loss(p_cal, y_test),
            "buckets": buckets,
            "table": table,
        },
    }
    Path(args.out).write_text(json.dumps(model, indent=1), encoding="utf-8")
    write_report(
        Path(args.report),
        model,
        table,
        {
            "n_train": len(train),
            "n_cal": len(cal),
            "n_test": len(test),
            "brier": brier(p_cal, y_test),
            "log_loss": log_loss(p_cal, y_test),
        },
    )
    print(f"wrote {args.out} and {args.report}")
    gap = abs(buckets[2]["actual"] - buckets[2]["predicted"])
    if gap > 0.01 or math.isnan(gap):
        print(f"FAIL: >=95% bucket is off by {gap:.2%}, more than 1 point")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
