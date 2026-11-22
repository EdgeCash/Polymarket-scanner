"""Fit what an NFL game's situation does to it: division, rest, body clock and rain.

    python scripts/fit_nfl_situations.py --raw DIR [--holdout-from 2020]

nflverse's games file says, for every game since 1999, whether it was inside a
division, each side's days of rest, the kickoff time and the closing spread and
total; nflfastR's play-by-play carries the stadium's weather note, which names rain
and snow (``--raw DIR/pbp/pbp_<year>.csv.gz``; left out when absent). Every game
since 2010 is given a situation-blind expected margin and total from team ratings
fit only on its season's earlier games, as the scanner does, with the scanner's
own wind adjustment on the total, and the miss is fit on each situation in turn:

- a divisional game, on the total and the margin;
- coming off a bye (13 days or more), a short week (under six) and the rest
  difference, on the margin;
- a Pacific-time side kicking off at night in the East, and an Eastern side
  playing the early window out West, on the margin;
- rain and snow in the weather note, outdoors, on the total.

A form ships only when it lowers the miss on the seasons held out. Each is also
fit against the closing line, which says how much of it the market already
prices: the scanner blends its number with the book's, so an effect the close
carries in full needs no help, and one it over-prices is a correction to the
blend, not to the model.

The running scanner never imports this script; it needs the ``train`` extras.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_history as history  # noqa: E402

from scanner.weather import BASELINE, WIND_PER_MPH  # noqa: E402

PACIFIC = {"SEA", "SF", "LA", "LAC", "SD", "OAK", "LV", "STL_NEVER"}
MOUNTAIN = {"DEN", "ARI"}
EASTERN = {
    "BUF", "MIA", "NE", "NYJ", "NYG", "PHI", "WAS", "BAL", "PIT", "CLE", "CIN", "ATL", "CAR",
    "TB", "JAX", "IND", "DET",
}  # fmt: skip


def weather_notes(raw: Path) -> dict[str, str]:
    """Each game's weather note from the play-by-play files, where they have been fetched."""
    notes: dict[str, str] = {}
    for path in sorted((raw / "pbp").glob("pbp_*.csv.gz")):
        try:
            frame = pd.read_csv(path, usecols=["game_id", "weather"], low_memory=False)
        except (ValueError, OSError):
            continue
        for game_id, note in frame.drop_duplicates("game_id").itertuples(index=False):
            if isinstance(note, str):
                notes[game_id] = note
    return notes


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
            "wind": g.wind,
            "div": g.div_game.astype(float),
            "home_rest": g.home_rest,
            "away_rest": g.away_rest,
            "gametime": g.gametime,
            "weekday": g.weekday,
            "spread_line": g.spread_line,  # the home side's expected margin
            "total_line": g.total_line,
        }
    ).reset_index(drop=True)
    rated = history.rate_before(games, "nfl")
    indoors = rated.roof.isin(["dome", "closed"])
    wind = np.where(indoors, 0.0, rated.wind.fillna(6.0))
    rated["indoors"] = indoors
    rated["exp_total"] = rated.exp_total + WIND_PER_MPH * wind - BASELINE
    rated["margin_miss"] = rated.home_final - rated.away_final - rated.exp_margin
    rated["total_miss"] = rated.home_final + rated.away_final - rated.exp_total
    rated["margin_close"] = rated.home_final - rated.away_final - rated.spread_line
    rated["total_close"] = rated.home_final + rated.away_final - rated.total_line
    hour = pd.to_numeric(rated.gametime.fillna("13:00").str.slice(0, 2), errors="coerce").fillna(13)
    night = hour >= 19
    early = hour <= 13
    for side, other in (("home", "away"), ("away", "home")):
        rest = rated[f"{side}_rest"]
        rated[f"{side}_bye"] = (rest >= 13).astype(float)
        rated[f"{side}_short"] = (rest <= 5).astype(float)
        # A Pacific side at night against a side from the East, wherever it is played.
        rated[f"{side}_late_body"] = (
            night & rated[side].isin(PACIFIC) & rated[other].isin(EASTERN)
        ).astype(float)
    rated["bye_edge"] = rated.home_bye - rated.away_bye
    rated["short_edge"] = rated.home_short - rated.away_short
    rated["rest_diff"] = (rated.home_rest - rated.away_rest).clip(-7, 7)
    rated["late_body_edge"] = rated.home_late_body - rated.away_late_body
    # A Pacific side on the road in the East's early window: the classic body-clock spot.
    rated["west_early"] = (
        early & rated.away.isin(PACIFIC) & rated.home.isin(EASTERN) & ~rated.neutral
    ).astype(float) * -1.0
    notes = weather_notes(raw)
    note = rated["id"].map(notes).fillna("").str.lower()
    rated["has_note"] = rated["id"].isin(notes)
    wet = note.apply(lambda t: bool(re.search(r"\brain|shower|drizzle|thunder", t)))
    snow = note.apply(lambda t: bool(re.search(r"\bsnow|flurr|sleet|wintry", t)))
    rated["rain"] = (wet & ~indoors).astype(float)
    rated["snow"] = (snow & ~indoors).astype(float)
    return rated


def test_form(rated, columns, target, close, holdout_from, label, subset=None):
    frame = rated if subset is None else rated[subset]
    train, test = frame[frame.season < holdout_from], frame[frame.season >= holdout_from]

    def design(part):
        return np.column_stack([np.ones(len(part))] + [part[c].to_numpy() for c in columns])

    beta, *_ = np.linalg.lstsq(design(train), train[target].to_numpy(), rcond=None)
    base = float(np.sqrt(np.mean(test[target] ** 2)))
    centred = design(test)[:, 1:] - design(train)[:, 1:].mean(axis=0)
    adjusted = test[target].to_numpy() - centred @ beta[1:]
    held = float(np.sqrt(np.mean(adjusted**2)))
    beta_all, *_ = np.linalg.lstsq(design(frame), frame[target].to_numpy(), rcond=None)
    priced = frame[frame[close].notna()]
    beta_close, *_ = np.linalg.lstsq(design(priced), priced[close].to_numpy(), rcond=None)
    # Standard errors of the all-season fit, to say how firm each number is.
    x = design(frame)
    resid = frame[target].to_numpy() - x @ beta_all
    cov = np.linalg.inv(x.T @ x) * resid.var(ddof=x.shape[1])
    se = np.sqrt(np.diag(cov))[1:]
    counts = [int((frame[c] != 0).sum()) for c in columns]
    print(f"\n{label}  ({len(train)} fit, {len(test)} held out)")
    for c, b, s, bc, n in zip(columns, beta_all[1:], se, beta_close[1:], counts, strict=True):
        print(f"  {c:16} {b:+6.2f} (se {s:.2f}, {n} games); the close still misses {bc:+5.2f}")
    print(f"  held-out miss {base:.3f} -> {held:.3f} ({base - held:+.3f})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--holdout-from", type=int, default=2020)
    args = parser.parse_args()
    rated = load(args.raw)
    h = args.holdout_from
    print(f"{len(rated)} rated games, 2010 on; held out from {h}")
    test_form(rated, ["div"], "total_miss", "total_close", h, "divisional game, total")
    test_form(rated, ["div"], "margin_miss", "margin_close", h, "divisional game, margin")
    test_form(rated, ["bye_edge"], "margin_miss", "margin_close", h, "off a bye, margin")
    post = rated.season >= 2011
    test_form(
        rated, ["bye_edge"], "margin_miss", "margin_close", h, "off a bye, 2011 on", subset=post
    )
    test_form(rated, ["short_edge"], "margin_miss", "margin_close", h, "short week, margin")
    test_form(rated, ["rest_diff"], "margin_miss", "margin_close", h, "rest difference, margin")
    test_form(
        rated, ["late_body_edge"], "margin_miss", "margin_close", h, "Pacific side at night, margin"
    )
    test_form(
        rated, ["west_early"], "margin_miss", "margin_close", h, "Pacific side early East, margin"
    )
    noted = rated.has_note
    if noted.any():
        print(f"\nweather notes for {int(noted.sum())} games")
        test_form(
            rated, ["rain"], "total_miss", "total_close", h, "rain outdoors, total", subset=noted
        )
        test_form(
            rated, ["snow"], "total_miss", "total_close", h, "snow outdoors, total", subset=noted
        )
        test_form(
            rated,
            ["rain", "snow"],
            "total_miss",
            "total_close",
            h,
            "rain and snow, total",
            subset=noted,
        )
    else:
        print("\nno play-by-play files: rain and snow not fit")


if __name__ == "__main__":
    main()
