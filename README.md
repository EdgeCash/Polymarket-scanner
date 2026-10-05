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
  winprob.py       win probability model                    (milestone 4)
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

## Milestones

| Tag | Milestone | Status |
| --- | --- | --- |
| v0.1 | Skeleton and CI | done |
| v0.2 | Polymarket reader | |
| v0.3 | Score feed and matching | |
| v0.4 | Win probability | |
| v0.5 | Fees and alert rules | |
| v0.6 | Diary and scorecard | |
| v0.7 | Phone alerts | |
| v0.8 | Deploy and shadow weekend | |
| v1.0 | Go live | |
