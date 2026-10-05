# Win probability model, version 1: calibration report

Generated 2026-10-05 14:11 UTC by `scripts/train_winprob.py`.

## What it is

A logistic regression on sign-symmetric features of the 4th-quarter game state (score margin, margin over the square root of seconds left, one/two/three-score steps and their time interactions, possession with field position, down and distance, timeouts, the pre-game spread and home field). Every coefficient is constrained to be non-negative, so a bigger lead, less time for the leader and having the ball can never lower the leading team's price. An isotonic (non-decreasing) calibration map fitted on separate seasons is applied on top.

Trained on NFL play-by-play from nflverse. College games use the same model with `CFB_EXTRA_MARGIN` subtracted from the fair price.

- Training seasons: [2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021] (86,987 plays, each seen from both sides)
- Calibration seasons: [2022, 2023] (22,695 plays)
- Held-out seasons: [2024, 2025] (22,607 plays)

## Held-out results (seasons never used for fitting or calibration)

- Brier score: 0.0959 (always-0.5 baseline 0.2500)
- Log loss: 0.3036

| Predicted at least | Plays | Mean predicted | Actual win rate | Gap |
| --- | --- | --- | --- | --- |
| 90% | 11,428 | 97.21% | 98.00% | +0.79% |
| 93% | 9,493 | 98.39% | 99.06% | +0.67% |
| 95% | 8,782 | 98.75% | 99.26% | +0.51% |
| 97% | 7,332 | 99.40% | 99.69% | +0.29% |
| 99% | 4,934 | 99.91% | 100.00% | +0.09% |

The merge gate from the brief: teams priced 95% or higher must actually have won within 1 percentage point of the predicted rate on held-out seasons. Result: predicted 98.75%, actual 99.26%, gap +0.51%.

## Calibration table (held-out seasons)

| Predicted range | Plays | Mean predicted | Actual win rate |
| --- | --- | --- | --- |
| 0.0% to 50.0% | 22,633 | 15.58% | 13.58% |
| 50.0% to 80.0% | 8,022 | 66.00% | 69.21% |
| 80.0% to 90.0% | 3,131 | 85.48% | 88.87% |
| 90.0% to 93.0% | 1,935 | 91.42% | 92.76% |
| 93.0% to 95.0% | 711 | 94.02% | 96.62% |
| 95.0% to 97.0% | 1,450 | 95.44% | 97.10% |
| 97.0% to 98.0% | 283 | 97.50% | 98.59% |
| 98.0% to 99.0% | 2,115 | 98.47% | 99.10% |
| 99.0% to 99.5% | 396 | 99.26% | 100.00% |
| 99.5% to 100.0% | 4,538 | 99.96% | 100.00% |

## Coefficients

| Feature | Weight |
| --- | --- |
| `margin` | 1.4914 |
| `margin_per_sqrt_time` | 1.4044 |
| `one_score_step` | 0.1442 |
| `two_score_step` | 0.0404 |
| `three_score_step` | 0.2973 |
| `one_score_step_x_time` | 0.2242 |
| `two_score_step_x_time` | 0.4100 |
| `possession` | 0.0000 |
| `possession_x_field` | 0.5535 |
| `possession_x_down` | 0.2590 |
| `possession_x_distance` | 0.0000 |
| `timeout_edge` | 0.0901 |
| `pregame_advantage` | 0.7180 |
| `home_field` | 0.0000 |

## Known limits

- One row per play; plays inside one drive are correlated, so the effective sample is smaller than the play count.
- Trained on NFL games only. College clock rules and scoring pace differ; the scanner subtracts `CFB_EXTRA_MARGIN` (default 1 cent) for college games and a college-specific model is a later upgrade.
- Overtime is not priced. The scanner sends nothing in overtime.
- The pre-game spread enters without a time interaction, so it keeps a small, constant influence through the 4th quarter.
