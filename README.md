# Polymarket Football Scanner

An alert-only helper that watches live NFL and college football games, compares
each team's real chance of winning to its Polymarket US price, and sends a phone
alert when a price looks too cheap. It also alerts when a game's total has
already gone over its line but the Over is still selling cheap.

It never places a trade. The owner reads the alert, opens the Polymarket app,
and decides.

Ground rules:

- **Alert-only.** No code places, changes or cancels orders. No trading keys.
- **Read-only data.** Public Polymarket US market data and ESPN's public scoreboard.
- **Near-certain only.** Winner alerts fire only late in the 4th quarter.
  Over alerts fire only once the Over is already decided.
- **Quiet by default.** Missing, stale or contradictory input sends nothing.
- **Everything is graded.** Every alert goes in a diary and is scored after the game.
- **Phone-operable.** Deploys from GitHub, configured by environment variables.

## Layout

```text
scanner/
  config.py        settings from environment variables
  models.py        GameState, Quote, FairPrice, Alert
  polymarket.py    read-only Polymarket US client          (milestone 2)
  scores.py        ESPN score feed client                   (milestone 3)
  matching.py      pairs each Polymarket game with ESPN     (milestone 3)
  tracker.py       freshness, two-polls-in-a-row, score age (milestone 3)
  winprob.py       win probability model                    (milestone 4)
  winprob_features.py  feature vector shared with training  (milestone 4)
  winprob_model.json   the trained model                    (milestone 4)
  fees.py          fee formula                              (milestone 5)
  rules.py         alert rules                              (milestone 5)
  diary.py         SQLite log and grading                   (milestone 6)
  web.py           status page and scorecard                (milestone 6)
  notify.py        phone alerts                             (milestone 7)
  loop.py          the scan loop and game-window scheduler  (milestone 8)
tests/             pytest suite; fixtures/ holds saved API responses
scripts/           smoke_live.py (by hand), train_winprob.py
docs/              winprob_report.md
```

## Running locally

```bash
cd polymarket-scanner
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # edit if needed; never commit .env
python -m scanner --dry-run     # logs settings (secrets hidden) and exits
pytest -q && ruff check . && ruff format --check .
```

Unit tests never call live services. The live smoke test is run by hand before
a merge and never in CI:

```bash
python scripts/smoke_live.py
```

## Settings

All settings are environment variables. Defaults are from the build brief.

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALERTS_ENABLED` | `false` | Master switch for sending alerts |
| `CLINCHED_OVERS_ENABLED` | `true` | Switch for clinched-over alerts only |
| `LEAGUES` | `nfl,cfb` | Which leagues to watch |
| `SCORE_POLL_SECONDS` | `5` | How often to read ESPN |
| `PRICE_POLL_SECONDS` | `3` | How often to read prices for candidates |
| `MAX_MINUTES_LEFT` | `8` | Late-game filter for winner alerts |
| `MIN_FAIR` | `0.93` | Lowest fair price that can trigger a winner alert |
| `MIN_EDGE` | `0.03` | Lowest edge after fee for a winner alert |
| `MIN_EDGE_CLINCHED` | `0.02` | Lowest edge after fee for a clinched-over alert |
| `MIN_DOLLARS_AVAILABLE` | `50` | Lowest size for sale |
| `SCORE_COOLDOWN_SECONDS` | `20` | Quiet time after a score change, winner alerts |
| `CLINCH_COOLDOWN_SECONDS` | `60` | How long a score must stand before a clinched-over alert |
| `REPEAT_ALERT_MINUTES` | `5` | Gap between repeat alerts |
| `MAX_ALERTS_PER_DAY` | `10` | Daily cap, both alert types together |
| `CFB_EXTRA_MARGIN` | `0.01` | Extra caution for college winner alerts |
| `DATABASE_PATH` | `/data/diary.db` | Diary location |
| `TZ` | `America/Chicago` | Time zone for messages |
| `PORT` | `8080` | Status page port |
| `TELEGRAM_BOT_TOKEN` | none | Secret |
| `TELEGRAM_CHAT_ID` | none | Secret |
| `STATUS_TOKEN` | none | Secret that unlocks the status and scorecard pages |

Secrets live only in the host's secrets store. `.env` is git-ignored.

## Data sources

- **Polymarket US**, through the official `polymarket-us` SDK (2.1.0), created
  with no arguments. Public endpoints only, no keys. The game list comes from
  `GET /v2/leagues/{slug}/events`; prices from `markets.bbo` and `markets.book`.
- **ESPN scoreboard** (unofficial, undocumented) for the real game situation.
  All ESPN parsing is isolated in `scores.py` and every field is validated.

## Polymarket findings (milestone 2)

Confirmed against the live gateway on 5 October 2026 and frozen in
`tests/fixtures/pm_*.json`:

- **Long and short pricing rule.** A game's moneyline is one instrument. The
  side with `marketSides[].long == true` is bought at the **best ask**; the
  other side is bought at **1 minus the best bid**. The event feed's own per-side
  `quote` fields equal exactly those values (ATL Falcons long: quote 0.4650 =
  best ask; NO Saints short: quote 0.5375 = 1 - 0.4625 best bid). The reader
  computes the price from the BBO and refuses to price a side if the feed's
  `longQuote`/`shortQuote` disagrees.
- **Market types.** The moneyline is `sportsMarketType ==
  "football_team_full_game_winner"`. The full-game total is
  `"football_team_full_game_total"`; its long side is described `"Over"` and
  its short side `"Under"`, and the reader identifies the Over by that label,
  not by assuming long. Team totals (`football_team_points_full_game_total`),
  half and quarter totals, spreads and props are ignored.
- **College football slug.** Discovered at startup by paging `GET /v2/leagues`
  and picking the league in the NFL's sport whose slug, name or abbreviation
  is CFB/NCAAF/college football. Today it resolves to `cfb`, on the second page.
  Polymarket US lists 182 college games for the coming week, FCS included.
- **Request volume.** The reader spaces requests at 5 per second at most; the
  live smoke test ran at about 2 per second. The events endpoint pages with
  `limit`/`offset` (default page size 10, so the reader asks for 100).
- **Order book.** `markets.book` returns `bids` and `offers` as
  `{px: {value}, qty}` levels. Buying the short side means hitting bids, so the
  short side's buy levels are each bid at `1 - price` for the same quantity.
- **Not-found.** A bad slug returns HTTP 404 with `{"code": 5, ...}`; the SDK
  raises and the reader treats it as "price feed unavailable".

## ESPN findings (milestone 3)

- **Yard lines.** ESPN's `situation.yardLine` runs from the home end zone (0) to
  the away end zone (100). Home possession: yards to go = `100 - yardLine`;
  away possession: `yardLine`. Confirmed against play-by-play on 5 October
  2026 and cross-checked on every read against `possessionText` ("CLE 16").
  When the two disagree the yard line is dropped, not guessed.
- **Spread.** `odds[0].spread` is the home team's spread; it is cross-checked
  against `details` ("NO -1.5") and dropped if they disagree.
- **ESPN win probability** is read from `situation.lastPlay.probability.
  homeWinPercentage` when present.
- **Status names** seen so far are mapped explicitly (scheduled, in progress,
  end of period, halftime, final, delayed, rain delay, suspended, postponed,
  canceled, forfeit). Any other name makes the game "unknown".
- **Kickoff times can differ by an hour** between the two feeds (Hawai'i at
  Arizona State: 01:30Z on ESPN, 02:30Z on Polymarket), so matching allows a
  two-hour gap and insists on both teams matching.
- **Matching** on the saved full-Saturday fixture pairs all 58 FBS games with
  zero wrong pairs; the other 124 Polymarket games are FCS games that the
  `groups=80` scoreboard does not carry.
- **Open item still open.** Whether every FBS game carries a `situation` block
  can only be checked on a live day; `scripts/smoke_live.py --section scores`
  prints it for every live game.

## Win probability model (milestone 4)

`scanner/winprob_model.json` is trained by `scripts/train_winprob.py` on
nflverse play-by-play (the files `nflreadpy` downloads; the script fetches
them directly, so the scanner has no dependency on that package). It is a
logistic regression on sign-symmetric 4th-quarter features with every weight
constrained to be non-negative, so by construction a bigger lead, less time
for the leader and having the ball never lower the leading team's price. An
isotonic calibration map fitted on separate seasons sits on top, and the
runtime caps the model at 99.5 cents. Results on the held-out 2024 and 2025
seasons are in [`docs/winprob_report.md`](docs/winprob_report.md); plays
priced at 95% or higher won 99.3% of the time against a 98.8% prediction.

The running scanner loads the JSON in milliseconds and evaluates it in pure
Python. It never trains. To retrain:

```bash
pip install -e ".[train]"
python scripts/train_winprob.py --data-dir data/nflverse   # downloads ~240 MB once
```

Fair price rules (`scanner/winprob.py`): the lower of our model and ESPN's
live number; our model minus 2 cents when ESPN has none; nothing when they
differ by more than 5 cents; nothing in overtime; 99.9 cents when the leader
can kneel the clock out; college games get `CFB_EXTRA_MARGIN` subtracted.

## Fees and rules (milestone 5)

`scanner/fees.py` implements `theta x contracts x price x (1 - price)` with
banker's rounding to the cent and matches the published 100-lot table at
every price tested (10c, 50c, 93c, 99c). `scanner/rules.py` applies the seven
numbered winner rules and the always-on checks in order, records a near miss
whenever a game passed rules 1 and 2 but failed later, and applies the shorter
clinched-over list. The Under has no code path: a clinched-over evaluation
refuses any quote that is not the Over side of its total. A recorded 18-poll
late-game sequence (`tests/fixtures/replay_phi_jax.json`) replays through the
rules and fires at exactly the three polls it should.

## Milestones

| Tag | Milestone | Status |
| --- | --- | --- |
| v0.1 | Skeleton and CI | done |
| v0.2 | Polymarket reader | done |
| v0.3 | Score feed and matching | done |
| v0.4 | Win probability | done |
| v0.5 | Fees and alert rules | done |
| v0.6 | Diary and scorecard | |
| v0.7 | Phone alerts | |
| v0.8 | Deploy and shadow weekend | |
| v1.0 | Go live | |
