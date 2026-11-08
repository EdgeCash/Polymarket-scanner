"""Grade the simulator's shape against game history: the report card, re-runnable.

    python scripts/grade_sim.py [--sports nfl cfb nba cbb nhl] [--json OUT] [--sample N]

Every game in ``tests/fixtures/history/<sport>.csv.gz`` is laid out by ``scanner.grid``
at its expected margin and total with the sport's own widths from
``projection.TUNING``, and the grid is compared with what happened:

- width: how widely real margins and totals land around the expectation, beside
  the grid's (against the close for the NFL, against earlier-games ratings for
  the others, so those widths are what the scanner itself would see);
- shape: how often games land on each margin, how much of the margin chance
  sits on the wrong numbers, how often a game is level after regulation;
- tails: the share of real results outside the grid's middle 90% (target 10%);
- calibration: how often a side the grid gives 60-70% wins, by 10% bin, and
  the Brier score; for hockey, how often the winner wins by two or more.

``tests/test_sim_history.py`` runs the same grading on a fixed sample and fails
when a number drifts past its tolerance.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.grid import grid, regulation  # noqa: E402
from scanner.projection import tuning_for  # noqa: E402

HISTORY = ROOT / "tests" / "fixtures" / "history"
# Expectations are rounded this finely (margin, total) so nearby games share a grid;
# far finer than any line moves.
ROUNDING = {"nhl": (0.05, 0.1)}
DEFAULT_ROUNDING = (0.25, 0.5)
# Football's key numbers by size of expected margin: the tables are one for every
# spread unless margin_by_spread passes build_sim_tables.py's held-out check.
SPREAD_BANDS = {
    "nfl": ((0, 2.5), (2.5, 4), (4, 7.5), (7.5, 11), (11, 99)),
    "cfb": ((0, 3.5), (3.5, 7.5), (7.5, 14), (14, 21), (21, 99)),
}
KEY_MARGINS = {
    "nfl": (1, 2, 3, 4, 6, 7, 10, 14),
    "cfb": (1, 2, 3, 4, 7, 10, 14, 17, 21),
    "nba": (1, 2, 3),
    "cbb": (1, 2, 3),
    "nhl": (1, 2, 3, 4),
}


def load(sport: str, sample: int | None = None) -> list[dict]:
    import csv
    import gzip

    with gzip.open(HISTORY / f"{sport}.csv.gz", "rt") as handle:
        rows = list(csv.DictReader(handle))
    if sample and len(rows) > sample:
        step = len(rows) / sample
        rows = [rows[int(i * step)] for i in range(sample)]
    return rows


def _cdf_position(pmf: dict[int, float], actual: int) -> tuple[float, float]:
    below = sum(p for v, p in pmf.items() if v < actual)
    return below, below + pmf.get(actual, 0.0)


def grade(sport: str, rows: list[dict]) -> dict:
    tuning = tuning_for(sport)
    sigma, sigma_total = tuning.sigma, tuning.sigma_total
    n = len(rows)
    real_margin: dict[int, int] = {}
    sim_margin: dict[int, float] = {}
    outside_m = outside_t = 0
    sq_m = sq_t = var_m = var_t = 0.0
    level_real = level_sim = 0.0
    bands = SPREAD_BANDS.get(sport, ())
    by_band = [[0, 0.0, 0.0, 0.0, 0.0] for _ in bands]  # games, sim 3, real 3, sim 7, real 7
    bins = [[0.0, 0.0, 0] for _ in range(10)]  # (chance sum, wins, games) by favourite chance
    brier = 0.0
    puck_real = puck_sim = 0.0
    for row in rows:
        step_m, step_t = ROUNDING.get(sport, DEFAULT_ROUNDING)
        margin = round(float(row["exp_margin"]) / step_m) * step_m
        total = round(float(row["exp_total"]) / step_t) * step_t
        g = grid(sport, margin, total, sigma, sigma_total)
        if g is None:
            continue
        home, away = int(float(row["home_final"])), int(float(row["away_final"]))
        actual_m, actual_t = home - away, home + away
        real_margin[abs(actual_m)] = real_margin.get(abs(actual_m), 0) + 1
        for band, (lo, hi) in zip(by_band, bands, strict=True):
            if lo <= abs(margin) < hi:
                band[0] += 1
                band[1] += g.margin.get(3, 0.0) + g.margin.get(-3, 0.0)
                band[2] += abs(actual_m) == 3
                band[3] += g.margin.get(7, 0.0) + g.margin.get(-7, 0.0)
                band[4] += abs(actual_m) == 7
        for m, p in g.margin.items():
            sim_margin[abs(m)] = sim_margin.get(abs(m), 0.0) + p
        sq_m += (actual_m - margin) ** 2
        sq_t += (actual_t - total) ** 2
        var_m += sum((m - margin) ** 2 * p for m, p in g.margin.items())
        var_t += sum((t - total) ** 2 * p for t, p in g.total.items())
        for pmf, actual, which in ((g.margin, actual_m, "m"), (g.total, actual_t, "t")):
            lo, hi = _cdf_position(pmf, actual)
            if hi <= 0.05 or lo >= 0.95:
                if which == "m":
                    outside_m += 1
                else:
                    outside_t += 1
        level_real += int(float(row["ot"]))
        level_sim += g.overtime
        if actual_m != 0:
            fav_home = g.home_win >= 0.5
            p_fav = g.home_win if fav_home else 1 - g.home_win
            won = (actual_m > 0) == fav_home
            b = min(int(p_fav * 10), 9)
            bins[b][0] += p_fav
            bins[b][1] += won
            bins[b][2] += 1
            brier += (g.home_win - (actual_m > 0)) ** 2
        if sport == "nhl":
            puck_real += abs(actual_m) >= 2
            puck_sim += sum(p for m, p in g.margin.items() if abs(m) >= 2)
    keys = KEY_MARGINS.get(sport, ())
    misplaced = 0.5 * sum(
        abs(real_margin.get(k, 0) / n - sim_margin.get(k, 0.0) / n)
        for k in set(real_margin) | set(sim_margin)
    )
    out = {
        "sport": sport,
        "games": n,
        "width": {
            "margin": {
                "sim": round(math.sqrt(var_m / n), 2),
                "real": round(math.sqrt(sq_m / n), 2),
            },
            "total": {"sim": round(math.sqrt(var_t / n), 2), "real": round(math.sqrt(sq_t / n), 2)},
        },
        "margins": {
            str(k): {
                "sim": round(sim_margin.get(k, 0.0) / n, 4),
                "real": round(real_margin.get(k, 0) / n, 4),
            }
            for k in keys
        },
        "misplaced": round(misplaced, 4),
        "level_after_regulation": {
            "sim": round(level_sim / n, 4),
            "real": round(level_real / n, 4),
        },
        "outside_middle_90": {"margin": round(outside_m / n, 4), "total": round(outside_t / n, 4)},
        "calibration": [
            {
                "bin": f"{b * 10}-{b * 10 + 10}%",
                "sim": round(s / g_, 3),
                "real": round(w / g_, 3),
                "games": g_,
            }
            for b, (s, w, g_) in enumerate(bins)
            if g_ >= 25
        ],  # fmt: skip
        "brier": round(brier / n, 4),
    }
    if bands:
        out["by_spread"] = [
            {
                "band": f"{lo}-{hi}",
                "games": n_b,
                "three": {"sim": round(s3 / n_b, 4), "real": round(r3 / n_b, 4)},
                "seven": {"sim": round(s7 / n_b, 4), "real": round(r7 / n_b, 4)},
            }
            for (lo, hi), (n_b, s3, r3, s7, r7) in zip(bands, by_band, strict=True)
            if n_b
        ]
    if sport == "nhl":
        out["win_by_two"] = {"sim": round(puck_sim / n, 4), "real": round(puck_real / n, 4)}
    return out


def regulation_level(sport: str, rows: list[dict]) -> float:
    """The grid's chance of a level game after regulation, averaged (a check on overtime)."""
    tuning = tuning_for(sport)
    total = 0.0
    for row in rows:
        reg = regulation(
            sport,
            float(row["exp_margin"]),
            float(row["exp_total"]),
            tuning.sigma,
            tuning.sigma_total,
        )
        total += sum(p for (h, a), p in reg.items() if h == a)
    return total / len(rows)


def report(card: dict) -> str:
    w = card["width"]
    lines = [
        f"## {card['sport'].upper()}: {card['games']} games",
        f"width: margin {w['margin']['sim']} sim vs {w['margin']['real']} real, "
        f"total {w['total']['sim']} vs {w['total']['real']}",
        "margins: "
        + ", ".join(
            f"{k}: {v['sim'] * 100:.1f}% vs {v['real'] * 100:.1f}%"
            for k, v in card["margins"].items()
        ),
        f"misplaced margin chance: {card['misplaced'] * 100:.1f}%",
        f"level after regulation: {card['level_after_regulation']['sim'] * 100:.1f}% sim vs "
        f"{card['level_after_regulation']['real'] * 100:.1f}% real",
        f"outside the middle 90%: margin {card['outside_middle_90']['margin'] * 100:.1f}%, "
        f"total {card['outside_middle_90']['total'] * 100:.1f}% (target 10%)",
        "calibration: "
        + ", ".join(f"{c['bin']} {c['sim']:.2f}->{c['real']:.2f}" for c in card["calibration"]),
        f"brier: {card['brier']}",
    ]
    for band in card.get("by_spread", []):
        lines.append(
            f"  |spread| {band['band']} ({band['games']}): 3 {band['three']['sim'] * 100:.1f}% vs "
            f"{band['three']['real'] * 100:.1f}%, 7 {band['seven']['sim'] * 100:.1f}% vs "
            f"{band['seven']['real'] * 100:.1f}%"
        )
    if "win_by_two" in card:
        lines.append(
            f"winner by 2+: {card['win_by_two']['sim'] * 100:.1f}% sim vs "
            f"{card['win_by_two']['real'] * 100:.1f}% real"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sports", nargs="+", default=["nfl", "cfb", "nba", "cbb", "nhl"])
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    cards = []
    for sport in args.sports:
        if not (HISTORY / f"{sport}.csv.gz").exists():
            print(f"## {sport.upper()}: no history file")
            continue
        card = grade(sport, load(sport, args.sample))
        cards.append(card)
        print(report(card), "\n", flush=True)
    if args.json:
        args.json.write_text(json.dumps(cards, indent=1) + "\n")


if __name__ == "__main__":
    main()
