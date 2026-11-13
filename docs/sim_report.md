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
scanner does), with each sport's shrinkage and home edge as fit on the same
history (`scripts/fit_shrinkage.py`). An earlier grading predicted each game from folds of its whole
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
| College football margin exactly 7 | 4.3% | 8.9% | 8.6% |
| College football level after regulation | 2.1% | 4.7% | 4.6% |
| College football margin chance on the wrong numbers | 22.0% | 1.6% | |
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
| College football | 11.3% / **20.4%** | 9.5% / 9.0% |
| NBA | **12.7%** / 7.9% | 9.3% / 8.8% |
| College basketball | **11.6%** / 8.2% | 9.1% / 9.2% |
| NHL | 10.1% / 6.2% | 6.2% / 6.2% |

The NFL is graded against the close, which misses by 13.0 and 13.2; the
scanner's own projections miss by more, so its 13.5 sits a little wide of the
close on purpose. Hockey's widths are its own misses with shots in the ratings
(2.55 and 2.30); its tails are light because a low-scoring game's grid is coarse.

## Win chances (a side the grid gives X wins Y of the time)

| Sport | 60-70% | 70-80% | 80-90% | 90-100% |
|---|---|---|---|---|
| NFL (vs the close) | 0.65 → 0.69 | 0.74 → 0.78 | 0.84 → 0.89 | |
| College football | 0.65 → 0.66 | 0.75 → 0.79 | 0.85 → 0.87 | 0.94 → 0.97 |
| NBA | 0.65 → 0.62 | 0.74 → 0.75 | 0.84 → 0.85 | 0.92 → 0.95 |
| College basketball | 0.65 → 0.66 | 0.75 → 0.74 | 0.84 → 0.87 | 0.94 → 0.95 |
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
earlier report's hockey overconfidence came from. The home edges are fit the same
way: hockey's 0.2 holds, college football's is 2.5 (2.8 was a touch high once
the empty-stadium 2020 season is set aside; 2.9 over 2023-2025 alone), the
NBA's 2 (home sides fell 0.44 a game short of 2.5) and college basketball's 4
(0.65 short of 3.5). The NFL is graded against the close, so its ratings are
not tested here.

## Known limits

- One table serves every spread in both football codes. NFL games spread 2.5
  to 4 land on 3 at 17.0% against the grid's 15.4%, and games spread 7.5 to 11
  at 11.0% against 12.8%, but tables that move with the expected margin must
  beat the single table on held-out seasons by two standard errors of the
  paired per-game log loss to ship (`build_sim_tables.py --spread-only
  --holdout-from YEAR`), and neither sport's do: the NFL's are 1.9 standard
  errors worse (2020-2026, 3.8276 against 3.8260), college football's a fifth
  of a standard error better (2023-2025), which is noise. Each check's result
  lives in the tables under `source.spread_check` and `tests/test_sim_history.py`
  rescores its fingerprint and the rule, so a stale record fails the suite.
  `grade_sim.py` prints the key numbers by spread for each rerun.
- nflverse has no quarter scores, so an NFL overtime game is taken as level at
  the lower final score (a loser's points in overtime are missed).
- Player props were not graded: Polymarket US lists no per-game player markets.

## The gap against the close: who is right when the model and the book disagree

November 2026. Re-run with `python scripts/grade_gaps.py`; `tests/test_gap_history.py`
holds the numbers.

The grids above are calibrated against the model's own projections, which says
nothing about beating the book. The daily card ranks sides by the model's chance
against the book's price, so the question that matters is whether a side the
model takes against the closing spread covers more often than the price needs
(52.4% at -110). `scripts/fetch_history.py --spreads` attaches each game's
closing line (`tests/fixtures/history/<sport>_lines.csv.gz`: college basketball,
the NBA and college football from ESPN's game summaries, which keep lines for
some seasons only, 5,110, 2,365 and 2,691 games; the NFL from nflverse, 2010 on,
3,495 games, rated on points alone since nflverse carries no yards) and
`scripts/grade_gaps.py` grades the raw model's side, the blend's side (the
scanner's own blend, the model weighted by games over games plus six) and the
underdog against the close.

Ratings fit only on each season's earlier games, both sides with three or more.

### The model's side against the close, by how far the raw model sat from it

| Sport | Gap | Games | Model's side covered | Grid gave it | Blend's grid gave it |
|---|---|---|---|---|---|
| College basketball | 0-5 | 4,077 | 50.7% | 56.5% | 54.6% |
| College basketball | 5-10 | 796 | 52.1% | 71.6% | 63.3% |
| College basketball | 10-15 | 181 | 62.4% | 83.8% | 69.2% |
| College basketball | 15-20 | 40 | 56.4% | 92.0% | 74.9% |
| College basketball | 20+ | 16 | 75.0% | 97-100% | 78-90% |
| NBA | 0-3 | 1,311 | 47.9% | 53.8% | 53.1% |
| NBA | 3-6 | 713 | 51.3% | 61.5% | 59.3% |
| NBA | 6-9 | 251 | 47.5% | 69.2% | 65.5% |
| NBA | 9-15 | 88 | 58.6% | 78.0% | 72.6% |
| College football | 0-5 | 1,856 | 49.3% | 55.6% | 53.0% |
| College football | 5-10 | 675 | 48.3% | 66.3% | 58.2% |
| College football | 10-15 | 135 | 51.9% | 76.4% | 62.0% |
| College football | 15+ | 25 | 48.0% | 85-96% | 64-75% |
| NFL | 0-3 | 2,226 | 50.8% | 54.2% | 52.5% |
| NFL | 3-6 | 1,008 | 48.4% | 62.7% | 57.3% |
| NFL | 6-9 | 213 | 50.0% | 71.7% | 62.7% |
| NFL | 9-12 | 35 | 38.2% | 78.0% | 66.6% |

The raw model's side is a coin toss in every sport up to ten points from the
close, and in the NFL and college football at every gap; college basketball's
10-15 band (62% of 181 games) and the NBA's 9-15 (59% of 88) are one or two
seasons' worth and well short of what the grid gave them. A side the grid gives
70-80% comes in 53% of the time in college basketball (466 sides), 51% in the
NBA (159), 49% in college football (225) and 50% in the NFL (191). The blend's
chances sit nearer the truth and reach those bins a third as often or less. The
close misses the final margin by less than the raw model in every sport (11.2
against 11.7 in college basketball, 13.0 against 13.7 in the NBA, 15.4 against
16.3 in college football, 13.0 against 13.5 in the NFL), with the blend between.

### Big spreads

College basketball spreads of 24 or more (two of the sport's spread; 87 games):
the underdog covered 49.4%, the raw model's side, nearly always the dog, 52.9%.
With under eight games a side (42 games) the model's side covered 47.6% and the
raw model missed those margins by 16.8 points to the close's 12.5 and the
blend's 13.1; from eight games (45) it covered 57.8%, a difference well inside
noise. Early-season spreads of 30 or more: the dog covered 3 of 10. Nothing here
says a big college underdog is worth more than its price, and the raw model's
number on those games is the worst of the three, which is the card's big-line
gate (`CARD_BIG_LINE_SIGMAS`, `CARD_BIG_LINE_GAMES`) and why it ranks on the
blend. College football says the same more loudly: spreads of 32.5 or more with
under eight games a side (39 games), the underdog covered 39.5% and the raw model
missed those margins by 21.2 points to the close's 15.9. No NBA spread reaches
28.

### What this means for the card

Edge on the card is the blend's chance less the price's break-even. On this
history that chance is still too generous once it passes 60%, so an edge of ten
points or more is not ten points; the record by rank and the closing-line share
on the card page are the live test. The NFL card record (1-5 at the time of
writing) is what a coin toss at -110 looks like over six picks.
