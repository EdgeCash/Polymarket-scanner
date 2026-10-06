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
  report.py        the shadow weekend report                (milestone 8)
tests/             pytest suite; fixtures/ holds saved API responses
scripts/           smoke_live.py (by hand), train_winprob.py
docs/              winprob_report.md
```

## Running locally

```bash
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
| `PERIOD_MARKETS_ENABLED` | `true` | Evaluate and record decided quarter and half markets |
| `PERIOD_ALERTS_ENABLED` | `false` | Also send them to the phone (needs `ALERTS_ENABLED` too) |
| `OBSERVATION_MINUTES_LEFT` | `15` | Record winner checks between `MAX_MINUTES_LEFT` and this; never sends; `0` turns it off |
| `PREGAME_ENABLED` | `true` | The pre-game scan across every sport (records, never sends) |
| `PREGAME_SPORTS` | all 14 | Comma list of `mlb,nba,wnba,cbb,nhl,nfl,cfb,epl,mls,ucl,laliga,bundesliga,seriea,ligue1` |
| `PREGAME_SCAN_MINUTES` | `15` | How often the pre-game scan runs |
| `PREGAME_MIN_EDGE` | `0.03` | Recording threshold: edge after fee against the book's vig-free number |
| `PREGAME_HORIZON_HOURS` | `36` | How far ahead games are compared |
| `GAMELOG_ENABLED` | `true` | The football game log and matchup sheets |
| `REPEAT_ALERT_MINUTES` | `5` | Gap between repeat alerts |
| `MAX_ALERTS_PER_DAY` | `10` | Daily cap, both alert types together |
| `CFB_EXTRA_MARGIN` | `0.01` | Extra caution for college winner alerts |
| `DATABASE_PATH` | `/data/diary.db` | Diary location |
| `TZ` | `America/Chicago` | Time zone for messages |
| `PORT` | `8080` | Status page port |
| `SEND_TEST_MESSAGE_ON_START` | `false` | Flip to true once: one test message at startup, whatever `ALERTS_ENABLED` says |
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

## Diary and scorecard (milestone 6)

`scanner/diary.py` writes every alert, near miss, follow-up price, observation
and outcome to SQLite at `DATABASE_PATH` (WAL mode, one file on the persistent
volume).
Grading settles a winner alert at $1, $0 or 50 cents for an NFL tie, a
clinched-over alert at $1 when the final combined score is above the line,
and marks alerts on postponed or cancelled games "not graded" so they stay out
of the totals. `scanner/web.py` serves `/health` and `/scorecard` as one-column
phone pages (or JSON with `Accept: application/json`); both need
`STATUS_TOKEN` as `?token=` or the `X-Status-Token` header and return 503 when
no token is configured, so the pages are never open. Opening any page once with
`?token=` signs that device in: the token is kept in an `HttpOnly` cookie for a
year, so the links between pages carry no token and a bookmark of `/matchups`
works on its own. `/logout` clears the cookie. A wrong token sets nothing.

## Phone alerts (milestone 7)

`scanner/notify.py` puts Telegram behind one `send(text)` call. The bot token
and chat id come from `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; the sender
never logs the request URL, which carries the token. With `ALERTS_ENABLED`
false nothing leaves the box, alerts included; the message text is still
built and stored in the diary so the shadow weekend records exactly what would
have been sent. Both message layouts are snapshot-tested against the examples
in the brief. One deliberate exception to the switch: `python -m scanner
--send-test-message` sends a single test message so the owner can confirm the
phone is reachable before going live.

Setting up the bot from a phone: message @BotFather on Telegram, `/newbot`,
copy the token into the host's secrets as `TELEGRAM_BOT_TOKEN`; then message
the new bot once and open `https://api.telegram.org/bot<TOKEN>/getUpdates` in
the phone browser to read your `chat.id` for `TELEGRAM_CHAT_ID`.

## Running it for real (milestone 8)

`scanner/loop.py` ties everything together. One pass every 5 seconds while a
game is live: refresh the game list (every 60 s), read both ESPN scoreboards,
match, grade finished games, find candidates (late 4th quarter with a fair
price near the bar, or a clinched total), read prices only for those, apply
the rules, send and record, then re-read prices 30 s and 2 min after each
alert. The loop sleeps when no game is within 30 minutes of kickoff and checks
the game list every 15 minutes while asleep. `/health` shows which state it is
in.

Useful commands:

```bash
python -m scanner                      # run the loop and the status pages
python -m scanner --once               # one pass (or "asleep") and exit
python -m scanner --dry-run            # settings only
python -m scanner --send-test-message  # the owner's phone check (ignores ALERTS_ENABLED)
python -m scanner --shadow-report      # print the shadow weekend report; add --send to send it
python -m scanner --pregame-once       # one pre-game scan across every sport, printed
```

### Hosting: Railway

The owner chose Railway (Hobby plan, about $5 a month). `railway.json` sets the
build to the Dockerfile, restarts the container if it ever exits, and uses `/`
(which needs no token) as the health check. Everything below is done in Safari
on the iPad.

1. railway.com → log in with GitHub → **New Project** → **Deploy from GitHub
   repo** → `EdgeCash/polymarket-scanner`. Railway builds the Dockerfile.
2. Open the service → **Variables** → **Raw Editor** and paste the settings
   from `.env.example`. Keep `ALERTS_ENABLED=false`. Fill in
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and a made-up `STATUS_TOKEN`
   (a long random phrase). Leave `PORT` out: Railway sets it.
3. Service → **Settings** → **Volumes** → add a volume with mount path `/data`.
4. Service → **Settings** → **Networking** → **Generate Domain**. Open
   `https://<domain>/health?token=<STATUS_TOKEN>` on the iPad. It should say
   "sleeping" with the next wake time, or "awake" during a game window. The
   "Build" row shows the short git commit Railway deployed, so you can check
   that a merge has gone live without opening Railway. That one visit signs
   the iPad in for a year; after it, `https://<domain>/matchups` with no token
   works, and a bookmark of it is enough.
5. Phone check (milestone 7): set `SEND_TEST_MESSAGE_ON_START=true`, let
   Railway redeploy, confirm the message on the phone, then set it back to
   `false`. Do this on a weekday, never during a game window.

Railway redeploys on every push to `main`, so merge only on a Tuesday or
Wednesday. Logs are under the service's **Deployments** tab; the diary can be
downloaded from the volume page if you ever want the raw file.

After the shadow weekend, run `python -m scanner --shadow-report --send` (or
read the diary) and decide whether to set `ALERTS_ENABLED=true`.

## Period markets and the observation window (shadow extras)

Two additions after the first live night, both quiet by design.

**Decided quarter and half markets.** Polymarket US lists, for every football
game, a total for each quarter and half, team totals for each half and spreads
for each quarter and half (`football_game_first_quarter_total`,
`football_team_first_half_spread` and so on; there are no quarter or half
moneylines). ESPN's scoreboard carries each team's points by quarter in
`competitors[].linescores`, kept only when both teams have them and they add up
to the score, and `STATUS_END_PERIOD` marks a quarter as over. Once a span has
ended, or an Over's points have already passed the line, the market's result is
known while it can still trade. `scanner/periods.py` says which side is decided
(never on a push, never without per-period scores, never for a spread or Under
before the span ends) and `evaluate_period` in `scanner/rules.py` applies the
clinched-over rules to that side: 99.5c fair price, `MIN_EDGE_CLINCHED`, the
`CLINCH_COOLDOWN_SECONDS` wait measured from the end of the span (or from the
score, for an Over passed mid-span), the book walk and the repeat gap. A game
can carry a hundred decided lines at halftime, so reads are rationed: three per
pass across all games, never-read markets first and among those the narrowest
margins first, each re-read at most every two minutes; a market seen closed or
resolved is not read again. These alerts keep their own repeat gap and daily
cap, so a period alert can never use up the winner alerts' cap, and the cap
only applies once `PERIOD_ALERTS_ENABLED` is on: while they are only being
recorded, every qualifying market is written down. They are sent only when
`PERIOD_ALERTS_ENABLED` is on as well as `ALERTS_ENABLED`. The scorecard shows
them as "Period markets" and grades them from the final per-period scores.

**The observation window.** With `OBSERVATION_MINUTES_LEFT` above
`MAX_MINUTES_LEFT` (15 and 8 by default), a winner candidate with between 8 and
15 minutes left is run through the same winner rules with the time filter
widened, at most once every 30 seconds per game, and the answer is written to
the diary's `observations` table: fair, model and ESPN prices, the buy price,
the edge, the dollars for sale and whether every rule would have passed. Nothing
is sent, nothing counts against a cap, and the alert thresholds are untouched.
Each game and pick counts once, at its first "would alert" check, and is graded
at the final like a winner alert, so the scorecard's "Observation window"
section shows the win rate needed, the actual win rate and the paper result the
8-minute rule is leaving on the table (or saving). Changing the rule itself is
still the owner's decision.

## Pre-game gaps across every sport (shadow)

The owner's question after the first live night: can the scanner watch every
matchup in every sport and tell when Polymarket is on the wrong side of the
books? ESPN's scoreboard for each sport carries one book's line (DraftKings in
October 2026): a moneyline for each side, a total with over and under prices, a
spread with a price for each side, and a draw price for soccer. A book's two
prices on one market add up to more than 100%; dividing each implied probability
by their sum removes the margin and leaves the book's own estimate
(`scanner/books.py`). Polymarket's event list carries every side's buy price, so
no order-book reads are needed to compare.

`scanner/pregame.py` runs in its own thread every `PREGAME_SCAN_MINUTES`, with
its own, slower request ceiling so the live football scanner never waits for
it. It reads each sport's scoreboard for yesterday, today and tomorrow (ESPN
counts days in US Eastern time, one request per day), lists the matching
Polymarket league, pairs the games (nicknames are allowed here, since the NBA
and NHL listings use "Nets" and "Predators" as whole names), and for every game
still to start compares the buy prices with the book's vig-free numbers:
moneylines only where a draw is impossible, totals and spreads only at the
book's own line, since a different number is a different bet. A side whose edge
after the fee clears `PREGAME_MIN_EDGE` is a gap: what the scan would have told
the owner to buy. The diary keeps the gap at the first price seen (later scans
only refresh how it looks now), stores each game's lines whenever they change,
gives every gap the book's last line before the start once the game begins, and
grades it from the final score: win, loss or push.

Two numbers judge it on the scorecard's "Pre-game gaps" section. The paper
result needs hundreds of near-coin-flip bets to mean much. The edge at the close
does not: if what the scan buys at 50 cents keeps closing at 54 at the book, the
edge is real before the results come in, and if it keeps closing at 49 the scan
is being picked off. Nothing here sends anything; turning any of it into an
alert is a separate decision for the owner.

`python -m scanner --pregame-once` runs one scan by hand and prints what it
found. Milestone check on 6 October 2026: 14 sports, 31 games matched, 26 with
a book line, no gaps at 3 cents on a quiet Tuesday morning.

## Football game log and matchup sheets

The owner reads matchup sheets before deciding anything and was paying a
subscription for them. ESPN's free endpoints carry the raw material for the NFL
and college football: a weekly scoreboard listing every game with scores,
quarter scores, records and the book's line, and a summary per game with the
box score (first downs, third and fourth downs, total, passing and rushing
yards, penalties, turnovers, possession, and in the NFL plays and sacks), the
scoring plays, the drive-by-drive play list, each team's logo and colour, and
for a game still to come the venue, weather and ESPN's own projection.

`scanner/gamelog.py` fetches each finished game once and keeps it in the diary
(`football_games`), on the same thread as the pre-game scan: the current and
next week's scoreboards every pass, earlier weeks until each is complete, and at
most `GAMELOG_BACKFILL_PER_PASS` (40) summaries per pass with a quarter-second
gap between requests, so the first college backfill takes a couple of hours and
a normal week a minute. Upcoming games within 48 hours go to `football_upcoming`
with their weather and venue re-read hourly. From those rows it builds, for each
team, season, home-or-away, first-half and last-three figures (points, passing
and rushing yards and touchdowns, yards per play, third downs, sacks, turnovers,
their "allowed" twins, turnover margin, penalties, pace, possession) and ranks
every team in the log on each one over its last three games. ESPN's box score
has no first-half split, so the first-half passing and rushing yards are added
up from the play-by-play (every pass, sack or rush in the first two quarters,
penalty plays left out) and the first-half touchdowns from the scoring plays.

`/matchups` lists the upcoming games with a sheet, each with its logos, the
book's line and the market implied score; `/matchup/{sport}/{game id}` is the
sheet: a card per team with its logo, record, streak, rest days, season offence,
defence and overall (point margin) ranks and last five results, then kickoff in
the owner's zone, venue, surface, weather, the book's line, the market implied
score, the book's win probability with the margin removed, the Polymarket prices
the pre-game scan last saw, ESPN's projection, and the three stat tables with
the advantage on each row. The implied score is arithmetic on the book's line:
with the home team at -3.5 and a total of 47.5 the market expects 25.5 to 22,
home = (total - home spread) / 2 and away = (total + home spread) / 2. Both
pages need the status token (or the cookie). Nothing on a sheet is a
recommendation, and the pages never place anything.
`python -m scanner --gamelog-once` runs one refresh by hand.

## Milestones

| Tag | Milestone | Status |
| --- | --- | --- |
| v0.1 | Skeleton and CI | done |
| v0.2 | Polymarket reader | done |
| v0.3 | Score feed and matching | done |
| v0.4 | Win probability | done |
| v0.5 | Fees and alert rules | done |
| v0.6 | Diary and scorecard | done |
| v0.7 | Phone alerts | code done; owner test message pending |
| v0.8 | Deploy and shadow weekend | loop code done; deploy needs the owner |
| v1.0 | Go live | |
