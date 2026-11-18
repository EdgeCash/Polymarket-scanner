# Why the daily card is all massive underdogs, and what to change

18 November 2026. A review of the simulator and the card after 14 days of cards
(33-32-0, -2.78u; college basketball 15-14-0 while every pick claimed a 95-98%
cover). Everything below was reproduced from the repository's own code and from
ESPN scoreboards for the 2024-25, 2025-26 and 2026-27 seasons; the scripts are
noted at the end.

## The short version

The simulator's shape (key numbers, overtime, tails) is fine. The problem is the
number it is centred on. In college basketball every team starts the season
rated "average" and is shrunk toward average by four pseudo-games, so with four
games played a team's rating is about half of what its results say. A 25-point
favourite therefore comes out as a 3-point favourite, the grid lays a 12-point
bell curve around that wrong centre, and the 25-point underdog "covers 97% of
the time". The card ranks by exactly that number, so it is a machine for finding
the games where the model is most wrong. The record is a coin flip because the
market's line was right and the model's was not.

## What the code does

- `projection.fit` rates every team from this season's games only, shrunk toward
  zero with `pseudo_games` (4 in college basketball). With 4 games a rating is
  `residual / 8`: half of the evidence. There is no prior from last season.
- `card.candidates_for` takes the side the **raw** model favours against the
  book and `side_chance` reads the cover probability off the grid with the
  sport's `sigma` (12) around the raw margin, as if the margin were known
  exactly. The blend with the book (`games / (games + 6)`) exists but the card
  does not use it.
- The ranking multiplies the edge by `trust = 0.5 + share of closing lines that
  moved the model's way`, floored at 0.5. College basketball's share is 11%
  over 449 games, which is as strong a disconfirmation as a sport can give (the
  no-information rate is about 50%), yet the sport keeps 61% of its nominal
  edge, and a nominal edge of +43 points dwarfs anything another sport can show.
- Reproduced for 17 November: the card's five claimed chances are exactly what
  `side_chance` gives for the model's margin against the book's (96.3, 98.4,
  96.9, 96.1, 95.2%). Adding projection uncertainty in quadrature (an 8-point
  standard error on the margin) only brings them down to 92-96%: the point
  estimate is biased toward zero, not merely noisy, so a wider curve cannot fix
  it.

## The evidence

### History: the scanner's own early-season ratings against an informed line

Every Division I game of 2024-25 and 2025-26 was rated as the scanner rates it
(points only, four pseudo-games, home edge 4, both sides with 3+ games), and set
against a line from ratings fit on the rest of the season (day 21 on, one
pseudo-game, 8+ games each), standing in for the book. Bucketed by how far the
model sat from that line:

First three weeks of the season (856 games; the model misses the final margin
by 14.2, the line by 11.9):

| Model vs line | Games | Model's side is the dog | Card would claim | Model's side covered | Final vs line | Final vs model |
|---|---|---|---|---|---|---|
| 0-5 | 390 | 242 | 58% | 54% | +0.5 | -1.9 |
| 5-10 | 285 | 225 | 73% | 50% | +0.1 | -7.2 |
| 10-15 | 120 | 111 | 85% | 60% | +2.7 | -9.8 |
| 15-20 | 39 | 39 | 92% | 54% | -0.8 | -18.3 |
| 20-30 | 20 | 20 | 97% | 60% | +2.9 | -19.9 |

Weeks four to six (1,849 games):

| Model vs line | Games | Dog | Claimed | Covered | Final vs line | Final vs model |
|---|---|---|---|---|---|---|
| 0-5 | 943 | 535 | 58% | 54% | +1.0 | -1.5 |
| 5-10 | 616 | 470 | 72% | 61% | +2.8 | -4.4 |
| 10-15 | 227 | 202 | 84% | 70% | +6.4 | -5.7 |
| 15-20 | 56 | 55 | 92% | 68% | +5.5 | -11.4 |

Three things to read off it. Beyond 15 points of disagreement the model's side is
always the underdog, as on the card. The final lands on the line, not on the
model (the "final vs model" column is the whole gap). And the claimed 92-97%
realises at 54-60% in the first three weeks: the card's record is what the
history says it should be. (By weeks four to six the model's side does cover
60-70% at big gaps, so the ratings carry some signal once they have a dozen
games; the stand-in line is not a real book line, so treat those as upper
bounds.)

### The ratings are not timid on average, only where the card looks

On the repository's own fixtures the slope of the real margin on the projected
one is 0.99 in the first two weeks and 1.02 after day 60, and the home edge is
right. Shrinkage toward zero is unbiased for a typical game and badly biased
for the mismatches, which is why `fit_shrinkage.py`, scoring the average miss,
cannot see it and why the record by rank is the first thing that did.

### A last-season prior

Rating 2025-26 with each team shrunk toward `r` times its 2024-25 final rating
instead of toward zero (`k` pseudo-games), scored as the scanner sees each
game:

| Days | Prior | Model miss | No prior | Line | Games 15+ from the line |
|---|---|---|---|---|---|
| 0-21 | none | 13.93 | | 11.32 | 23 of 417 |
| 0-21 | r 0.7, k 8 | 12.72 | 13.93 | 11.32 | 8 |
| 0-21 | r 0.6, k 4 | 12.77 | 13.93 | 11.32 | 11 |
| 21-46 | none | 13.42 | | 13.39 | 31 of 930 |
| 21-46 | r 0.7, k 8 | 12.90 | 13.42 | 13.39 | 26 |

The prior takes 1.2 points off the early miss and removes two thirds of the
15-point disagreements. It does not close them: for 17 November it moves
Alabama from -3.1 to -9.8 against a book of -25.5, SMU from -6.0 to -13.4
against -22.5, Providence from -4.8 to -11.0 against -30.5. The market also
knows the rosters; a prior from last season's games alone does not.

## What the research says

Three research passes (preseason priors, big-spread market evidence, and
uncertainty-aware edge estimation) agree with the data. The parts worth acting
on:

- **No serious system starts flat.** KenPom's preseason prior is worth "a little
  less than five games" and only reaches zero after a team's 27th game; Torvik
  phases his out after about 13; ESPN's BPI, Haslametrics, EvanMiya, Sagarin and
  TeamRankings all start from last season, returning minutes, transfers and
  recruits. The one open, fully published backtest of exactly this model
  structure (ridge ratings inside an opponent-adjustment loop,
  sportsdataverse-py PR 1084) found a prior of 0.9 times last season's rating at
  4 pseudo-games cut men's college basketball log loss over the first six weeks
  from 0.729 to 0.581 on a 2023-26 holdout; for the NBA 0.8 at about 8 games.
  Year-over-year carryover elsewhere: college football about 0.7 (SP+), NHL
  about 0.6 (goal differential), NBA 0.75-0.8.
- **Big dogs are not mispriced.** Favourites of 20+ in college basketball have
  covered 51.0% since 2005 (Action Network, 2,225 games) and 53.2% in a team's
  first nine games; 30+ favourites in college football 52.3% over 904 games
  (2016-2025). The 1990s academic "big dog" edge was 52-54% and is gone. If
  anything the book's November mismatch lines are a touch low on the favourite,
  the opposite of what the card is betting. Garbage time compresses blowout
  margins by 2-4 percentage points of cover rate, not 45.
- **The market deserves most of the weight.** A well-specified Serie A model got
  a log-pool weight of exactly zero against the closing price; FiveThirtyEight's
  NBA model was least accurate in the games where it disagreed most with Vegas;
  the market's miss is as small in November as in March while a flat-start
  model's is at its worst. The practitioner answer is the same everywhere: the
  biggest disagreements are the model's errors, not its edges.
- **The ranking has a winner's curse.** Choosing the largest estimated edge
  selects the largest estimation error (Smith and Winkler's optimizer's curse).
  The fix is to shrink each estimated edge by its reliability, the slope of the
  realised margin on the estimated edge, which can be fit from the graded
  record per sport and per games-played bucket, and will be near zero in the
  first weeks.
- **11% is a verdict.** Under no edge about half of lines move your way; 11% over
  449 games is some 16 standard errors below that. A floor of 0.5 on trust
  cannot express it.

## What to change, in order

1. **Stop the bleeding on the card.** Until the model carries a prior, rank by
   the blend's probability rather than the raw model's, and let trust fall to
   zero: `trust = share` (or the fitted reliability slope below), no floor. A
   sport whose closing lines move against it nine times in ten should not be
   able to fill the card. Also cap the credible disagreement: a side more than
   about one game-sigma from the book (12 in college basketball, 13.5 in the
   NFL) is a diagnostic flag, not a candidate.
2. **Give the ratings a prior.** Shrink toward `r` times last season's final
   rating with `k` pseudo-games instead of toward zero, fit `r` and `k` per
   sport on history the way `fit_shrinkage.py` fits `pseudo_games` (college
   basketball around 0.7 and 4-8; the NBA 0.8 and 8; hockey 0.6; college
   football 0.7 at 1-2 games; new programmes at a low quantile). The log
   already stores every past season, so last season's ratings are a `fit` away
   with no new data source; roster-aware priors (returning minutes, transfers)
   would be a second step and need an outside source.
3. **Carry the ratings' uncertainty into the grid.** The ridge solve gives a
   posterior covariance for free; a game's projection variance is the contrast
   through it. Early in a college season it is comparable to the game noise.
   Use `sqrt(sigma^2 + var_proj)` in the grid, or average the grid over draws of
   the centre, so a four-game rating cannot claim 97%.
4. **Combine with the market properly.** Replace the fixed `games / (games + 6)`
   blend with weights fit on history: regress the final margin on the model's
   margin and the book's (Granger-Ramanathan), per sport and games-played
   bucket, and price the cover from that combined mean and residual spread. In
   November the model's weight in college basketball will come out well under
   a fifth, which is the honest answer.
5. **Calibrate what the card claims.** Fit `cover ~ z` (z = gap over sigma) on
   the graded picks and projections, per sport; the slope is the temperature
   correction and the test of honesty. Keep a reliability table on the card
   page beside the record by rank.
6. **Fit `fit_shrinkage.py` on the right target.** It minimises the average
   miss, which the mismatches barely move. Add the slope by |projected margin|
   bucket and the cover rate against the rest-of-season line so this failure
   mode shows up in the fit, and add a real-line backtest once historical
   closing lines are on hand (ESPN's past scoreboards and summaries carry
   none; sportsbookreviewsonline's season files or Torvik's game CSVs would,
   from a network that can reach them).

## Reproduction

Scratch scripts from this review (not committed): `fixture_slope.py` (slope and
residual by season stage and spread size on `tests/fixtures/history`),
`fetch_cbb.py` (ESPN scoreboards for three seasons), `backtest.py` (the tables
above) and `current.py` (the 17 November games with and without a prior). The
replication reproduces the scanner's own numbers for those games to within a
tenth of a point (Alabama -3.1, Vanderbilt -21.1, Miami -20.3, Providence -4.8,
SMU -6.0 against the card's -5.5).

## What was done (18 November 2026)

Steps 1 to 4 above are in, plus the player lines, in the same change as this
note. In order:

- **Prior.** `scanner/priors.json` (built by `scripts/build_priors.py`) holds
  last season's final ratings by ESPN team id; `projection.fit` shrinks toward
  a share of them (college basketball 0.9 at four games, college football 0.8
  at three, the NBA 0.6 at six, hockey 0.5 at eight, the NFL 0.4 at four), fit
  by `scripts/fit_priors.py` on the 2024-25 and 2025-26 seasons. A team the
  file does not know starts about 25 points below average in the college
  sports, where that is what a visitor from outside the division plays like.
- **Uncertainty.** Each projection carries its own spread: the sport's average
  miss split into the game's noise (from the book's miss) and the model's
  settled error, plus the variance a few games leave, so `league.sigma` is 14
  for a college game between four-game sides and 12 by mid-season. The grid,
  the card and the pictures read it.
- **Blend.** Precision-weighted: the book's error variance over the two
  together. The model carries 6-13% in college basketball, 8% in the NBA, 6% in
  hockey, 3-4% in the NFL, 7-8% in college football. The blend's win chance
  comes from its own margin through the grid.
- **Card.** Priced from the blend at the blend's spread; trust is twice the
  share of moved closing lines that moved the model's way, with no floor; a
  raw number more than one game's spread from the book rules its side out.
  Run through the scanner's own code on 17 November, the five college picks
  come out with edges of -0.1 to -2.2 points instead of +43 to +45, and Alabama
  at -15.8 (model) and -24.9 (blend) against the book's -25.5 instead of -3.1.
- **Player lines.** Football rates are shrunk toward the role's rate (each
  team's leaders), which took 10 to 60 yards of bias out of the yardage lines;
  a projected rout trims the heavy-minutes players' time in basketball; the
  dispersions were refit by maximum likelihood per sport on 41,000 graded
  lines from a walk-forward over 2025-26 box scores (`scripts/fit_players.py`).

Not done, and worth doing next: a real-line backtest of the card rule once
historical closing lines are reachable; a roster-aware prior (returning
minutes, transfers) for the college sports; and fitting the blend's weights by
regression on the diary's own graded record once a season of it exists, which
is also where the assumed book numbers in `TUNING` should be checked.
