"""The simulator against history: fails when a change undoes the shape the tables fit.

Real rates come from every game in ``tests/fixtures/history`` (cheap: no grid). The
grid's rates are its average chances over a fixed, evenly spaced sample of the
same games at their expected margin and total with the sport's ``TUNING``
widths, which pins them closely without laying out thousands of grids. The
full report card is ``python scripts/grade_sim.py``.

Football's tables also carry the result of the held-out check on expected-margin
adjustments (``source.spread_check``, from ``build_sim_tables.py --spread-only
--holdout-from YEAR``). The test rescores its fingerprint with the grid, the widths
and the tables as they stand, so a change that leaves the record stale fails here
until the check is rerun.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

from scanner.grid import grid, regulation
from scanner.projection import tuning_for

HISTORY = Path(__file__).resolve().parent / "fixtures" / "history"
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = 120

# (margins checked, their tolerance in points of share, level-after-regulation
# tolerance, most margin chance allowed on the wrong numbers)
CHECKS = {
    "nfl": ((1, 2, 3, 4, 6, 7, 10, 14), 0.012, 0.012, 0.05),
    "cfb": ((1, 2, 3, 4, 7, 10, 14, 17, 21), 0.012, 0.012, 0.06),
    "nba": ((0, 1, 2, 3), 0.008, 0.01, 0.06),
    "cbb": ((0, 1, 2, 3), 0.008, 0.01, 0.04),
    "nhl": ((0, 1, 2, 3, 4), 0.02, 0.02, 0.03),
}


def _rows(sport: str) -> list[dict]:
    path = HISTORY / f"{sport}.csv.gz"
    if not path.exists():
        pytest.skip(f"no history for {sport}")
    with gzip.open(path, "rt") as handle:
        return list(csv.DictReader(handle))


def _int(value: str) -> int:
    return int(float(value))


def _real(rows: list[dict]) -> dict:
    n = len(rows)
    final: dict[int, float] = {}
    for row in rows:
        m = abs(_int(row["home_final"]) - _int(row["away_final"]))
        final[m] = final.get(m, 0.0) + 1 / n
    level = sum(_int(r["ot"]) for r in rows) / n
    two = sum(abs(_int(r["home_final"]) - _int(r["away_final"])) >= 2 for r in rows) / n
    return {"final": final, "level": level, "two": two}


def _sim(sport: str, rows: list[dict]) -> dict:
    step = len(rows) / SAMPLE
    chosen = [rows[int(i * step)] for i in range(min(SAMPLE, len(rows)))]
    tuning = tuning_for(sport)
    n = len(chosen)
    final: dict[int, float] = {}
    level = two = 0.0
    for row in chosen:
        margin, total = float(row["exp_margin"]), float(row["exp_total"])
        g = grid(sport, margin, total, tuning.sigma, tuning.sigma_total)
        for m, p in g.margin.items():
            final[abs(m)] = final.get(abs(m), 0.0) + p / n
        level += g.overtime / n
        two += sum(p for m, p in g.margin.items() if abs(m) >= 2) / n
    return {"final": final, "level": level, "two": two}


@pytest.mark.parametrize("sport", sorted(CHECKS))
def test_the_grid_lands_where_real_games_land(sport):
    margins, tolerance, level_tolerance, misplaced_most = CHECKS[sport]
    rows = _rows(sport)
    real, sim = _real(rows), _sim(sport, rows)
    for k in margins:
        got, want = sim["final"].get(k, 0.0), real["final"].get(k, 0.0)
        assert got == pytest.approx(want, abs=tolerance), f"{sport} margin {k}"
    assert sim["level"] == pytest.approx(real["level"], abs=level_tolerance)
    keys = set(real["final"]) | set(sim["final"])
    misplaced = 0.5 * sum(abs(real["final"].get(k, 0) - sim["final"].get(k, 0)) for k in keys)
    assert misplaced < misplaced_most
    if sport == "nhl":  # the puck line: the winner by two or more
        assert sim["two"] == pytest.approx(real["two"], abs=0.02)


def test_regulation_is_the_grid_before_overtime():
    tuning = tuning_for("nfl")
    reg = regulation("nfl", 3.0, 44.0, tuning.sigma, tuning.sigma_total)
    g = grid("nfl", 3.0, 44.0, tuning.sigma, tuning.sigma_total)
    assert sum(reg.values()) == pytest.approx(1.0)
    assert sum(p for (h, a), p in reg.items() if h == a) == pytest.approx(g.overtime)


def _grade_sim():
    """scripts/grade_sim.py, which holds the held-out check (stdlib only)."""
    spec = importlib.util.spec_from_file_location("grade_sim", ROOT / "scripts" / "grade_sim.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("sport", ["nfl", "cfb"])
def test_the_held_out_check_on_spread_tables_is_recorded_and_still_holds(sport):
    entry = json.loads((ROOT / "scanner" / "sim_tables.json").read_text())[sport]
    check = (entry.get("source") or {}).get("spread_check")
    rerun = (
        f"rerun: python scripts/build_sim_tables.py --sports {sport} --spread-only "
        f"--holdout-from YEAR"
    )
    assert check, f"{sport}: the held-out check on margin_by_spread has not been run ({rerun})"
    # The decision the check reached is the state that shipped.
    assert check["kept"] == (check["shaped"] < check["plain"])
    assert ("margin_by_spread" in entry) == check["kept"], f"{sport}: tables disagree; {rerun}"
    # And it was reached with the grid, the widths and the tables as they are now.
    grade_sim = _grade_sim()
    assert check["fingerprint"]["games"] == grade_sim.FINGERPRINT_SAMPLE
    plain = grade_sim.without_spread(entry)
    assert grade_sim.fingerprint(sport, plain, check["holdout_from"]) == pytest.approx(
        check["fingerprint"]["plain"], abs=1e-4
    ), f"{sport}: the plain table or the grid has changed since the check; {rerun}"
    if check["kept"]:
        assert grade_sim.fingerprint(sport, entry, check["holdout_from"]) == pytest.approx(
            check["fingerprint"]["shaped"], abs=1e-4
        ), f"{sport}: the adjustments have changed since the check; {rerun}"
