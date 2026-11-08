# Simulator report card

November 2026. Re-run with `python scripts/grade_sim.py`; `tests/test_sim_history.py`
fails if any sport drifts back.

Every game in `tests/fixtures/history/` is laid out by the score grid
(`scanner/grid.py`) at its expected margin and total with the sport's `TUNING`
widths, and the grid is compared with what happened. "Before" is the simulator
as it stood: its original widths, two independent bell curves rounded to whole
scores, no key numbers, overtime at the full pregame edge. "After" is the grid
with the shape tables in `scanner/sim_tables.json` and the new widths.

| Sport | Games | Seasons | Expected margin and total from |
|---|---|---|---|
| NFL | 4,297 | 2010-2026 regular season | Closing spread and total (nflverse) |
| College football | 4,528 | 2018-2025, FBS vs FBS | Ratings fit only on that season's earlier games |
| NBA | 4,730 | 2021-22 to 2024-25 | Ratings fit only on earlier games |
| College basketball | 15,460 | 2022-23 to 2024-25, Division I | Ratings fit only on earlier games |
| NHL | 5,030 | 2021-22 to 2024-25 | Ratings fit only on earlier games |

The ratings are the scanner's own model, fit as the scanner sees a game before
it starts: half the score and, in college football and hockey, half what the
yards or shots say (the NBA and college basketball use points alone, as the
scanner does). An earlier grading predicted each game from folds of its whole
season, later games included, which made college basketball look better than
the scanner can be (an 11.2 miss where it is 12.0); another left the shots and
yards out, which made hockey look overconfident when it is not (below).

## Shape

| | Before | After | Real |
|---|---|---|---|
| **NFL** margin exactly 3 | 6.6% | 14.4% | 14.6% |
| NFL margin exactly 7 | 5.5% | 8.6% | 8.7% |
| NFL level after regulation | 2.7% | 5.9% | 6.0% |
| NFL margin chance on the wrong numbers | 18.9% | 1.6% | |
| **College football** margin exactly 3 | 5.0% | 10.6% | 10.6% |
| College football margin exactly 7 | 4.3% | 8.6% | 8.6% |
| College football level after regulation | 2.1% | 4.7% | 4.6% |
| College football margin chance on the wrong numbers | 22.0% | 1.5% | |
| **NBA** one-point margin | 6.0% | 4.0% | 4.0% |
| NBA level after regulation | 2.8% | 5.1% | 5.1% |
| **College basketball** one-point margin | 6.2% | 4.5% | 4.5% |
| College basketball level after regulation | 2.9% | 6.2% | 6.2% |
| **NHL** one-goal margin | 47.5% | 39.3% | 39.1% |
| NHL two-goal margin | 23.8% | 19.9% | 19.8% |
| NHL three-goal margin | 15.4% | 23.9% | 23.9% |
| NHL winner by two or more (puck line) | 52.5% | 60.7% | 60.9% |
| NHL level after regulation | 16.8% | 21.8% | 21.8% |
| NHL margin chance on the wrong numbers | 12.4% | 0.3% | |

## Width and tails

Real results outside the grid's middle 90% (target 10%), margin / total:

| Sport | Before | After |
|---|---|---|
| NFL | 9.7% / **17.5%** | 8.6% / 8.6% |
| College football | 11.3% / **20.4%** | 9.4% / 8.9% |
| NBA | **12.7%** / 7.9% | 9.2% / 8.8% |
| College basketball | **11.6%** / 8.2% | 9.0% / 9.2% |
| NHL | 10.1% / 6.2% | 6.2% / 6.2% |

The NFL is graded against the close, which misses by 13.0 and 13.2; the
scanner's own projections miss by more, so its 13.5 sits a little wide of the
close on purpose. Hockey's widths are its own misses with shots in the ratings
(2.55 and 2.30); its tails are light because a low-scoring game's grid is coarse.

## Win chances (a side the grid gives X wins Y of the time)

| Sport | 60-70% | 70-80% | 80-90% | 90-100% |
|---|---|---|---|---|
| NFL (vs the close) | 0.65 → 0.69 | 0.74 → 0.78 | 0.84 → 0.89 | |
| College football | 0.65 → 0.66 | 0.75 → 0.79 | 0.85 → 0.87 | 0.94 → 0.96 |
| NBA | 0.64 → 0.63 | 0.74 → 0.75 | 0.84 → 0.84 | 0.92 → 0.94 |
| College basketball | 0.65 → 0.66 | 0.75 → 0.75 | 0.84 → 0.88 | 0.94 → 0.95 |
| NHL | 0.64 → 0.65 | 0.73 → 0.74 | | |

Basketball and hockey are calibrated; college football's heavy favourites
still win a little more than priced (0.79 for 0.75), down from 0.83 before its
shrinkage was fit. The shrinkage (`scripts/fit_shrinkage.py`, every game rated
on its season's earlier games with the scanner's half-score, half-volume
performance) found college football's four pseudo-games left the projected
margin 1.66 times too timid once the yards half was in; at a half the slope is
1.10, the margin miss 16.1 instead of 16.9 and the Brier score 0.187 instead of
0.196. Hockey's shots make its miss flat from 1.5 to 5 pseudo-games, so it
keeps four; on goals alone it would have wanted sixteen, which is where the
earlier report's hockey overconfidence came from. College football's intercept
(real margin 0.6 points under the projection at an even game) says its home
edge of 2.8 is a little high; the NFL is graded against the close, so its
ratings are not tested here.

## Known limits

- One table serves every spread. NFL games spread 2.5 to 4 land on 3 at 17.0%
  against the grid's 15.4%, and games spread 7.5 to 11 at 11.0% against 12.8%.
  Tables that move with the expected margin close that in-sample but do no
  better on held-out seasons (NFL 2020-2026 log loss 3.8276 against 3.8260;
  college 2023-2025 4.0265 against 4.0264), so they are built and checked
  (`build_sim_tables.py --spread-only --holdout-from YEAR`) but not shipped.
  The check's result lives in the tables under `source.spread_check` and
  `tests/test_sim_history.py` rescores its fingerprint, so a stale record fails
  the suite. `grade_sim.py` prints the key numbers by spread for each rerun.
- nflverse has no quarter scores, so an NFL overtime game is taken as level at
  the lower final score (a loser's points in overtime are missed).
- Player props were not graded: Polymarket US lists no per-game player markets.
