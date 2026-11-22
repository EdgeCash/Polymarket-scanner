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
  projection.py    the projection model, graded against the book
  priors.py        last season's ratings, the prior each team starts from
  priors.json      those ratings by sport and ESPN team id (scripts/build_priors.py)
  injuries.py      injury reports, rosters and depth charts: who is out and what it costs
  goalies.py       hockey: who is likely in goal tonight and what the gap is worth
  goalie_priors.json   every goalie's record before this season (scripts/build_goalie_priors.py)
  scratches.py     hockey: the league's own scratch list, read just before the game
  stakes.py        what the model would stake (recorded, never placed)
  card.py          the daily card: five picks by rule, locked, graded, public
  grid.py          every final score's exact chance: the one source of odds
  simulate.py      the grid as 100,000 games per pick, as pictures
tests/             pytest suite; fixtures/ holds saved API responses
scripts/           smoke_live.py (by hand), train_winprob.py, the simulator's
                   fetch_history.py, build_sim_tables.py and grade_sim.py, and the
                   model's build_priors.py, fit_priors.py, fit_players.py,
                   fetch_box_scores.py, bulk_box_scores.py, fit_injuries.py,
                   fit_ratings.py, fit_nfl_situations.py, fit_goalies.py,
                   build_goalie_priors.py, fit_absence_proxy.py,
                   fit_roster_prior.py and build_roster_priors.py
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
| `GAMELOG_ENABLED` | `true` | The game log and matchup sheets |
| `GAMELOG_SPORTS` | `nfl,cfb,nba,nhl,cbb` | Sports with a game log, sheets, projections and stake suggestions |
| `PROJECTION_ENABLED` | `true` | The projection model on the sheets and scorecard |
| `STAKE_ENABLED` | `true` | Record (and, once earned, show) what the model would stake |
| `BANKROLL` | `1000` | Dollars the stake suggestions are sized from |
| `STAKE_MIN_EDGE` | `0.03` | Edge after fees a suggestion needs, in probability |
| `STAKE_KELLY_FRACTION` | `0.25` | Share of the full Kelly stake to suggest |
| `STAKE_MAX_SHARE` | `0.05` | Largest suggestion as a share of the bankroll |
| `STAKE_GATE_GAMES` | `50` | Graded projections per sport before the sheet shows stakes |
| `PLAYERS_ENABLED` | `true` | Player lines for every upcoming game, graded at the final |
| `INJURIES_ENABLED` | `true` | Read each game's injury report; move the projection and the lines for the men out |
| `ROSTERS_ENABLED` | `true` | Read each team's roster (and NFL depth chart) daily for its injured list and starters |
| `ROSTER_SPORTS` | `nfl,nba,nhl` | Sports whose rosters are read (ESPN carries no report for college) |
| `SCRATCHES_ENABLED` | `true` | Read the NHL's scratch list from the league's API just before each game |
| `CARD_ENABLED` | `true` | The daily card: picks chosen by rule, locked, graded, public at `/card` |
| `CARD_PICKS` | `5` | Picks on a card, one per game |
| `CARD_LOCK_TIME` | `10:00` | When the card locks each day, `HH:MM` in `CARD_TZ` |
| `CARD_TZ` | `America/New_York` | The card's clock; the public page shows times in it |
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
- **ESPN game summaries, rosters and depth charts** (unofficial) for the game
  log, the injury reports and who starts; parsed in `gamelog.py` and
  `injuries.py`, read-only, a request every quarter second at most.
- **The NHL's own API** (api-web.nhle.com, public, no key) for the scratch list
  of each game about to start: the day's schedule and one sheet a game, parsed
  in `scratches.py`.

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
   works, and a bookmark of it is enough. The root, `https://<domain>/`, is
   the public front door: the daily card and the player lines with their
   records, no token, the address to give people. Its footer names the build
   too, so a deploy can be checked without signing in.
5. Phone check (milestone 7): set `SEND_TEST_MESSAGE_ON_START=true`, let
   Railway redeploy, confirm the message on the phone, then set it back to
   `false`. Do this on a weekday, never during a game window.

6. The monthly check on the phone (optional). A Railway cron is a second
   service from the same repo that runs on a schedule and exits, so: **New**
   → **GitHub Repo** → the same repo again; in its **Settings** set
   **Config-as-code** to `railway.cron.json` (the Dockerfile build, the start
   command `python -m scanner --monthly-check --remote`, and the schedule
   `30 13 1 * *`: 13:30 UTC on the 1st, which is 8:30 or 9:30 in the East);
   in its **Variables** set `SCANNER_URL` to `https://<domain>` from step 4
   and `STATUS_TOKEN` to the same phrase (a reference,
   `${{scanner.STATUS_TOKEN}}`, keeps them in step). No volume: the cron has
   no diary of its own, so it posts to the running service's `/monthly-check`,
   which builds the last full month from its diary and sends it, then the cron
   exits until next month. Railway schedules in UTC. To try it by hand, run
   the cron service's **Deploy** once from its page, or
   `python -m scanner --monthly-check --remote` anywhere with those two
   variables set.

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
a normal week a minute. Each stored game carries the format version of the parser
that read it; when the parser learns something worth a re-read (first-half yards,
the closing line), the version is bumped and older rows are read again once with
whatever budget new games leave, oldest first. The summary's `pickcenter` block
is the book's line as it closed, kept with the game for grading. Upcoming games
within 48 hours go to `football_upcoming`
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

## Projection model (football)

The owner wants each sheet to carry enough to decide on, and to know whether a
model built from the log can hold its own against the book before any money
follows it. `scanner/projection.py` is that model, kept plain enough to explain
on the sheet: for every team, half of what it scored and half of what its yards
say it should have scored (the league's points per yard times its yards), each
adjusted for who it played, shrunk toward its share of last season's rating
while games are few (so many pseudo-games of the prior: four in most sports,
three in college football), plus home field (1.8 points in the NFL, 2.5 in
college). Two ratings per team
come out, points above average on offence and points above average allowed on
defence; a game's projection is the league average plus the two ratings that
meet, with half the home edge on each side, and the win probability is the
projected margin against the usual spread of results (13.5 points NFL, 16.25
college; totals 13.5 and 16.75: real totals miss the NFL's closing number by
13.2, and the model's own college projections miss by 16.1 and 16.8 when they
have seen only earlier games). A first-half score is the projection times the
league's first-half share. Turnover luck is mostly washed out by the yards
half; of the weather only the wind is in it, of rest only the back-to-back,
and of injuries what ESPN lists before the game, as below.

The wind is one of two conditions that move a total by more than noise, and the
market prices only part of it. `scanner/weather.py` takes each mph of
sustained wind off a football total at 0.27 points, linear from calm, with a
dome or a closed roof as calm; the ratings already carry the average
conditions they were fit in, so a game is adjusted by its effect less that
average (a dome up about a point and a half, a six-mph day not at all, a
twenty-mph gale down three and a half), split evenly between the sides before
the blend, and the sheet's projection block says so. Fit by
`scripts/fit_weather.py` on 3,314 NFL games (nflverse's stadium reports,
2010-2026) against a weather-blind expected total from ratings fit only on
each season's earlier games: the miss on 2020-2026 falls from 13.75 to 13.62
with the wind in, cold adds nothing once it is, and the closing total still
misses 0.16 a mph, so the market prices about forty percent of it. ESPN
reports a gust, not a sustained wind, so sustained is taken as two thirds of
the gust; college football takes the NFL's fit, the one source with the wind
on record; and a game with no weather yet, or indoors with no indoor flag, is
left as it is.

Rain is the other condition that earns a place. nflverse's games file has no
rain on record, but nflfastR's play-by-play carries each stadium's weather
note, and `scripts/fit_nfl_situations.py` reads it for 3,357 games (2010-2026):
a game played in rain runs 4.1 points under the same wind-adjusted expectation
(182 games; held out on 2020-2026 the total's miss falls from 13.71 to 13.66),
the same with the wind fit beside it, steady in every stretch since 2015, and
the closing total still misses 3.7 of it. Snow shows nothing that holds (40
games, and the other way). Before a game there is only a forecast, so the
effect is weighted by ESPN's chance of precipitation, a forecast at or below
freezing is taken as snow and moves nothing, and like the wind it is applied
less its mean over the fitted games (one in eighteen was wet, a fifth of a
point), so a dry day and a dome each get that back. College football takes the
NFL's number.

The same script fit the rest of an NFL game's situation and none of it earned
a place. A divisional game ran 2.0 points under from 2010 to 2019 but half a
point since 2020, by which time the close had caught up, and using it made the
held-out miss worse. Coming off a bye is worth 0.8 of a point by this model's
reckoning (422 games, give or take 0.65), which the close already carries in
full; the estimate swings from -0.3 to +3.4 between stretches of seasons. A
Pacific side at night against an Eastern one (71 games) and a Pacific side in
the East's early window (140) did not hold out either, and a short week nearly
always falls on both sides at once. `scripts/fit_ratings.py` tested what a
rating is made of on five seasons of box scores, each game projected as the
scanner would have seen it: taking the day's turnovers back out of the score
made the margin's miss worse at every price tried (the yards half already
does that work), a cap on the margin one game may count for moved it by a
hundredth of a point, and fading old games helped one stretch and hurt
another. The dials stay in `projection.TUNING`, switched off.

A side that played the day before gives up ground, and the schedule says so
before the market has to. `scanner/rest.py` moves the raw margin toward the
rested side when the other is on a back-to-back: 2.5 points in the NBA, 0.45
goals in the NHL (carried at 0.40 since the goalie model below took its share), fit by `scripts/fit_rest.py` against a rest-blind expected
margin from ratings fit only on each season's earlier games (4,661 NBA games
and 4,947 NHL games, 2021-22 to 2024-25, held out on 2024-25: the NBA's margin
miss falls from 14.22 to 14.13, hockey's from 2.563 to 2.553). The two sides'
coefficients cannot be told apart (NBA home -2.8, away +2.4), so one serves
both; long rest adds nothing; the totals do not move; both sides tired is a
wash. A back-to-back is a game on the day after the side's last one by the
Eastern date (a 7 pm tip is the next day in UTC), counted from every game on
the season's schedule in the log, played or still to come, so a side whose
game yesterday is not yet stored still shows as tired. The sheet's projection
block names the tired side. The history files carry the same adjustment, so
the widths and tables describe what the scanner projects.

A missing man is the one thing the book knows that the box scores do not, so
the model reads the same report. `scanner/injuries.py` takes each upcoming
game's injury list from its ESPN summary (re-read hourly with the venue and
weather; the NFL, NBA and NHL publish one, college does not) and each side's
roster page (read about once a day for teams with a game in the next two days,
a dozen teams a pass, `ROSTER_SPORTS`), which adds the men on injured reserve
or suspended, and in the NFL the depth chart, which names the starter at every
spot. Each listed man is given a chance of missing the game by his status
(out, injured reserve and a suspension in full; doubtful 0.9; day-to-day 0.45;
questionable 0.4; a practice report that says active, nothing) and a worth in
the sport's own units from his lines in the log: in basketball his production
a game (points, assists at 0.7, rebounds at 0.5) above a replacement's 8 (6
in college); in hockey a skater's points a game plus 0.03 a minute of ice time
above 0.6, and the starting goalie (most ice time this season) as a unit of his
own; in football the starting quarterback (rank one on the depth chart, else
the man who threw most last game) as a unit of his own, a back's or receiver's
rushing and receiving yards a game, and any other starter on the depth chart
as a head. The ratings were fit on the games the side actually played, so a
man who has already missed most of them is already priced in: his worth is
scaled by the share of the side's last ten games he played, and a man the log
has never seen costs nothing. Worth times chance times share, summed and
capped (12 points in basketball, 1.5 goals, 14 points in football), comes off
the side's projected score before the blend; the margin moves, the total
moves with it, and the sheet's projection block names the men and the cost,
with the whole report in its own block. The coefficients a unit are fit by
`scripts/fit_injuries.py` on the 2024-25 seasons of box scores that
`scripts/fetch_box_scores.py` gathers: an absence in history is a man who
played his side's previous game and not this one, valued as the scanner values
him the day before, regressed against an absence-blind expected margin from
ratings fit on the season's earlier games and held out on the last 30% of each
season. A unit of basketball production costs 0.115 points of margin (5,913 NBA
games over five seasons, from `scripts/bulk_box_scores.py`; the held-out miss
falls from 14.22 to 14.09; college takes 0.12 by hand), a unit of a skater's
production 0.23 goals (2,490 NHL games over two seasons, 2.627 to 2.619; the
goalie has a model of his own, below), and the starting quarterback 4.9 points
(1,119 NFL
games over 2021-2025, 212 with a starter lost; held out 13.22 to 13.10). His
worth does not move with how good he is by any measure the box score offers:
terms for the starter's and the backup's adjusted yards an attempt each came
out at nothing over 371 games started by another man. A back's or receiver's
yards keep a hand-set 0.012 a yard (the box score names no positions; counted
together they give 0.016 over the five seasons and nothing on the part held
out) and the other starters 0.4 a head (the depth chart has no past). Every graded projection records its miss with and without the
adjustment, and the scorecard shows both, so the report has to earn its place. The player lines read the same list: a man at
least 0.75 likely out gets no line, and in the sports with playing time his
minutes go to the men who stay in proportion to their own; a man in doubt
keeps his line, marked with the chance (`availability` in its parameters).
`INJURIES_ENABLED=false` leaves the model as it was. Nothing is sent or placed.

Hockey has two more things to read before a game. The first is the goalie.
A side's rating carries its goaltending as it has been, in the share each man
started; tonight has one man in net, and when he is not the usual one the
rating is off by the gap between them: a point of save percentage is three
tenths of a goal over a night's thirty shots. `scanner/goalies.py` rates each
goalie by his save rate this season plus his earlier seasons at half weight
(`scanner/goalie_priors.json`, built by `scripts/build_goalie_priors.py`; a
single season's save rate told the fit nothing), shrunk toward the league's by
500 league-average shots. Nobody posts the starter where the scanner
can read it, but the schedule says most of it: on the second night of a
back-to-back last night's man starts again one time in eleven (1,141 such
nights over two seasons), otherwise the share of recent starts is the best guess,
and a goalie the injury report or the scratch list rules out does not start.
The expected save rate over those chances, less the side's usual, times the
shots it allows, comes off what the other side is projected to score.
`scripts/fit_goalies.py` fit it on three seasons, 2023-24 as the memory and
2,450 games of the next two scored. The goalie who did start, known only
afterwards, explains the margin's miss at 1.16 goals a goal of edge (give or
take 0.30; held out 2.5924 to 2.5842), so the save rates mean what they say
and the edge counts in full. What is known beforehand moves 1,900 of those
games by a twentieth of a goal on average and takes the held-out miss from
2.5924 to 2.5897 with the back-to-back fit beside it; the back-to-back itself
falls from 0.37 to 0.32 goals when the edge is in, which was the tired side's
second goalie all along, so `scanner/rest.py` now carries hockey's 0.45 at
0.40. A confirmed starter would be worth three times what the guess is; no
free source posts one where the scanner can read it. The sheet's projection
block names the likely
man and his save rate against the usual, and the scorecard keeps the miss with
and without, as it does for injuries.

The second is the scratch list. About half an hour before the puck drops the
league posts each side's scratches on its own site (api-web.nhle.com), hurt or
healthy, which ESPN's report misses or carries a day late. `scanner/scratches.py`
asks for it only for games inside fifty minutes of their start, matches the
league's game to ESPN's by the two sides and the hour and each man by his
name, and counts a scratched man as out in full: a skater through the injury
valuation above, a goalie by ruling him out of the start. An empty sheet with
no officials named is taken as not yet posted and asked for again on the next
pass. `SCRATCHES_ENABLED=false` switches it off.

The NBA got one change to what a rating is made of and several that were
tried and left out. An old game now fades: one 25 of the team's own games ago
counts half (`half_life` in `projection.TUNING`). On five seasons from
`scripts/bulk_box_scores.py` (6,163 games, each projected as the scanner would
have seen it by `scripts/fit_ratings.py`) the margin's miss falls from 13.84 to
13.79 over the first four and from 14.56 to 14.50 on the last, which chose
nothing, and the total's from 18.86 to 18.60 and from 19.07 to 18.97: the
league scores more before the new year than after it, and a team is not in
March what it was in November. Left out, each for want of a held-out gain:
other measures of a player's worth (points alone, the box score's game score,
minutes; all within 0.02 of the one in use); travel into the game by distance
or by time zones crossed, on any night or the second of a back-to-back (2,365
games; an eastward zone showed 0.7 of a point, give or take 0.4, in one season
and nothing in the other); a separate chance that a star rests on the second
night (they sit 9.9% of those against 6.3% rested, about 0.15 of a point,
which the back-to-back's 2.5 already holds); and a cap on one game's margin.
One more thing did earn a place: a quarter of a side's three-point makes above
the league's rate on its own attempts is taken back out of the score as luck
before the game is rated (`three_luck`). On the same five seasons the margin's
miss goes from 13.79 to 13.78 and from 14.50 to 14.49, the total's from 18.60
to 18.55 and from 18.97 to 18.91; at a half the margin gets worse, so most of
a hot night is skill or the defence and a quarter of it is the rim.
In hockey neither a cap nor a fade moved the miss in the third decimal place.
The overtime branch and the empty net were already in the score grid, and the
back-to-back by home and road was already fit in `scripts/fit_rest.py`.

The shrinkage is fit per sport on history (`scripts/fit_shrinkage.py`): every
game rated only on its season's earlier games, with the scanner's own
half-score, half-volume performance, for each shrinkage in a grid, scored by
how far the real margin moves per projected point (1 when the projection is as
bold as it should be) and by its miss. The yards half makes a college
performance steady enough that four pseudo-games left the projection 1.66
times too timid; at a half the slope is 1.10 (0.98 early in a season), the
margin miss falls from 16.9 to 16.1 and the Brier score from 0.196 to 0.187,
for a total miss 1.8% wider. Hockey's shots do the same job: with them the
miss is flat from 1.5 to 5 pseudo-games, so it keeps four. On goals alone it
would have wanted sixteen, which is why a grading that leaves the shots out
calls the hockey ratings overconfident when they are not.

The home edges are fit the same way (`--hfa-grid`): the edge at which home
sides, rated on earlier games, come out with no residual. Hockey's 0.2 is
right. College football's 2.8 was a touch high: 2.5 once the empty-stadium
2020 season is set aside (2.9 over 2023-2025 alone, so it is drifting back;
give or take a quarter of a point either way). The NBA's 2.5 left home sides
0.44 a game short over 4,730 games, so it is 2; college basketball's 3.5 left
them 0.65 short over 15,460, so it is 4. The NFL's 1.8 is not fit here, since
its history is graded against the close rather than the ratings.

The prior is last season. `scanner/priors.json` holds every team's final
offence and defence rating from the season before (`scripts/build_priors.py`,
from ESPN's scoreboards, points only, by ESPN team id), and `projection.fit`
shrinks a team toward a share of it instead of toward average: 0.9 in college
basketball, 0.8 in college football, 0.6 in the NBA, 0.5 in hockey and 0.4 in
the NFL, worth the pseudo-games above. A team the file does not know (a
newcomer, or a visitor from outside the division) starts where the season's
strangers finished, about 25 points below average in both college sports. Fit
on the 2024-25 and 2025-26 seasons with each game rated as the scanner sees
it (`scripts/fit_priors.py`): the prior takes college basketball's miss over
the first six weeks from 14.0 to 12.9 points and over the season from 12.3 to
11.8, college football's early miss from 16.8 to 15.8, the NBA's from 13.2 to
12.9 and hockey's from 2.46 to 2.42; the NFL's one test season preferred no
prior at all (12.90 against 13.02 at a half), so it keeps a light one. Before
the prior, every team opened the season at average and with four games was
rated at half of what its results said, which read a 25-point favourite as a
3-point one and sent the daily card after 25-point underdogs; the review that
found it is in [`docs/card_review_nov2026.md`](docs/card_review_nov2026.md).

Each game's projection also says how far it can be trusted. The sport's
spread of results (`sigma`) is the model's average miss; the book's miss on
the same games (closing spreads: about 10.5 in college basketball, 12.5 in the
NBA, 13.0 in the NFL, 16 in college football, 2.45 goals in hockey) is the
game's own noise plus the book's small error about the truth, so what the
model's miss carries beyond that noise is its settled rating error, and a game
whose sides have played a handful of games adds the variance those few games
leave (`performance_sd` over games plus pseudo-games, fit on the same
history). The projection's `league.sigma` is the sport's spread widened to
that game (14 points for a college game between four-game sides, 12 by
mid-season), and the grid, the card and the pictures all read it, so a
four-game rating cannot claim a 97% cover.

Beside the raw projection the sheet shows a blend with the book, weighed by
precision: the model's weight is the book's error variance over the book's and
the model's together (`projection.uncertainty`), which comes out at 6-13% in
college basketball from November to March, 8% in the NBA, 6% in hockey, 3-4%
in the NFL and 7-8% in college football. That is the honest answer: the
closing line is the best single forecast in every sport, a flat-start model
with a month of games is not close, and the research behind the review found
no public model that beats it by more than a point or two. The blend's own
spread is narrower than the model's, its win chance comes from its own margin
through the grid, and the gap between the raw model and the book is still
written out in points, with the inputs (both teams' ratings, the league
average, home field, games used) under the table so the owner can see why.
Until November 2026 the blend's weight was games over games plus six.

Every projection is written to the diary (`projections`) when a game comes into
the 48-hour window, updated each pass until kickoff, then locked and graded once
the game's record lands in the log: the miss against the final margin and total
for the model, the blend and the book; a Brier score for each win probability;
which side the model took against the book's spread and total and whether it
covered, kept for every game and bucketed on the scorecard by how far the model
sat from the book (1, 2, 3 and 5 points) so no threshold is chosen for the
owner; and whether the closing line moved the model's way. The scorecard's
"Projection model" section shows all of it, with the book as the bar to beat.
`PROJECTION_ENABLED=false` switches the model off. Nothing in it sends or places
anything.

### Stake suggestions

The owner set a bankroll of $1,000 (`BANKROLL`). `scanner/stakes.py` turns the
blended projection into what the model would stake on Polymarket: for each
market the pre-game scan has a price for (the moneyline, and the total and
spread at the book's line), the blend's probability for each side (from the
same score grid the simulation pages draw, key numbers and overtime included;
a line that can push is priced given it does not, since a push is void) is set
against the last
Polymarket price seen, after the taker fee. When the edge clears
`STAKE_MIN_EDGE` (3 cents), the stake is a quarter of the Kelly share
(`STAKE_KELLY_FRACTION`) of the bankroll, capped at `STAKE_MAX_SHARE` (5%).
Suggestions go to the diary (`stakes`) as one row per market, updated each pass
until kickoff, dropped if the edge goes, locked at kickoff and settled at the
final like the alerts are (a tie pays half, a push costs nothing), with the
paper profit at the suggested stake after fees.

The sheet shows the Stake table only once the model has earned it in that
sport: `STAKE_GATE_GAMES` (50) graded projections with the closing line moving
the model's way on average. Until then the sheet says so and the rows are
recorded as shadow suggestions; the scorecard's "Stake suggestions" section
shows all of them, shown or not, with wins, losses, pushes, money staked, profit
and return. `STAKE_ENABLED=false` switches the layer off. A suggestion is a
number on a page: nothing here places anything.

## Basketball and hockey (NBA, NHL)

The NBA and NHL seasons began in October 2026 and the owner asked for the same
sheets, model and stake suggestions for them. Everything above carries over:
the same `GameLog` thread, the same diary tables (still named `football_*`;
for a sport ESPN lists by day the week column holds the day as `YYYYMMDD`),
the same projection, grading and stake code, and the same per-sport gate.
`GAMELOG_SPORTS` (`nfl,cfb,nba,nhl,cbb`) says which sports get a log; the `LOGS`
table in `scanner/gamelog.py` says how each one is read and shown, and
`TUNING` in `scanner/projection.py` holds the model's numbers per sport. What
differs:

- ESPN lists these sports by day (US Eastern), not by week. Each pass reads
  yesterday, today and tomorrow, then walks the season's calendar (the
  scoreboard carries it) from its first day with whatever summary budget the
  new games left, marking each day complete once its games are stored, so the
  walk is paid for once. Preseason games are left out.
- Box scores. Basketball keeps shooting (field goals, threes, free throws),
  rebounds, assists, steals, blocks, turnovers, fouls, paint and fast-break
  points, and an estimate of possessions (FGA − ORB + TOV + 0.44·FTA), so the
  sheet can show points per 100 possessions and pace. Hockey keeps shots,
  power-play goals and chances, faceoffs, hits, blocked shots, takeaways,
  giveaways and penalty minutes. A shootout counts in the score (the market
  settles on it) but not as a goal in play, so the shooting and save
  percentages use the goals scored in play. The sheet's "1st half" column is
  the first period in hockey.
- The model. Basketball uses points alone (home court 2, final margin
  spread 14, the model's own miss on 4,730 games rated only on earlier ones;
  total spread 19). Hockey uses half goals and half what the
  shots say (the league's goals per shot times shots), with home ice 0.2, a
  margin spread of 2.55 and a total spread of 2.3 (the model's own misses on
  5,030 games rated only on earlier ones, shots included). The book's puck line is
  always 1.5 whatever the matchup, so the margin the book expects is read off
  its moneyline: the margin at which the model's own score grid (empty nets,
  overtime and all) gives the book's vig-free win probability, at the book's
  total; that margin also gives the market implied score, and the
  scorecard's gap rows for hockey are in quarter goals. The scorecard shows
  the model's record sport by sport, since the misses are in different units.
- Stakes. The same quarter-Kelly rule against the last Polymarket prices the
  pre-game scan saw: the moneyline, the total and the spread or puck line at
  the book's number. The gate is per sport, 50 graded games with the closing
  line moving the model's way, which these sports reach in a couple of weeks
  of nights rather than a season.

Live check on 28 October 2026: each pass stored 40 games per sport, the NBA's
season to date (56 games from the 20 October opener) in two passes and the
NHL's (from 29 September) within a couple of hours, with the status page
counting the days still to read; 15 sheets per sport for the next two days,
every one projected, and 30 and 32 teams ranked. The live football scanner and
its alerts are untouched: the NBA and NHL are sheets, projections and shadow
stakes only. Nothing is sent or placed.

### College basketball (CBB)

The college season opened on 2 November 2026 and the owner asked for it next.
It is the NBA's path with three differences: ESPN's Division I listing
(`groups=50`, the same filter the pre-game scan uses; a visitor from outside
Division I appears with its games against Division I sides and is usually
thin), two halves instead of four quarters (the sheet's "1st half" is the
first line score), and its own numbers in `TUNING` (home court 4 then, a margin
spread of 12, a total spread of 17, a first-half share of 0.47 until the log
says otherwise). Opening night listed 182 games, so the backfill takes a few
passes per day and the matchups page is long on a Saturday. The simulator's
overtime for college is five minutes at a slightly lower pace than the NBA's.

Four things were then fit for college on four seasons of box scores (20,642
rated games, 2022-23 to 2025-26, from `scripts/bulk_box_scores.py`), and each
earned a place.

The home edge is two numbers. Home sides fell 1.0 to 1.5 points short of the
single 4.0 inside their own conference in every one of the four seasons and
beat it by 1.7 to 3.1 outside it, whether the host was favoured or not: a
conference visitor knows the gym and the trip, a November guest does not.
ESPN's scoreboard and summary both say whether a game is inside a conference
(`conferenceCompetition`), so the log now keeps it (version 4 of the game
record; college games stored before it are read once more, 40 a pass, to fill
it in), and `TUNING` carries 3.0 inside and 5.0 outside, with 4.0 for a game
whose kind is unknown. With the ratings fit again on those edges the margin's
miss goes from 11.72 to 11.69 over the first three seasons and from 11.79 to
11.73 on the last, which chose nothing (`scripts/fit_ratings.py --only home`).

The prior is a forecast of this season, not a copy of the last. Two fifths of
a college side's minutes come back in a usual year and the portal moves the
rest, and every player keeps ESPN's athlete id from one side to the next, so
the box scores say who is back, who has arrived and what each did last season.
`scripts/fit_roster_prior.py` fits a side's final rating on the rating split by
the share of minutes returning, that share, the newcomers in starter-equivalents
(capped at the minutes the returning men leave open) with their old sides'
ratings, what both groups produced a minute against the league, and the
conference's level for the share that left. On the season left out it misses
a side's final rating by 4.16 points where last season's rating alone misses
by 4.74 (1,445 team-seasons), and played through whole seasons in place of
the plain prior it takes the margin's miss from 11.66 to 11.59 over the season
and from 12.45 to 12.27 in its first 45 days. `scripts/build_roster_priors.py` writes
the forecast into `scanner/priors.json` (366 sides for 2026-27, from who has
played so far this season; 3.0 points from the plain prior on average), marked
`weighted` so the scanner takes it whole.

There is no injury report, so the last box score stands in for one. A regular
(half of the side's last ten games, more production than a replacement) who
sat out his side's last game is still out the next time 63% of the time
(15,162 such absences), and the margin moves 0.137 points a unit sat out, so a
unit actually lost is worth 0.22, nearly twice the NBA's: college rotations
are shorter. `scanner/injuries.py` lists those men with the status "missed
last game" at that chance, and the held-out miss goes from 11.79 to 11.77
(`scripts/fit_absence_proxy.py`). The player lines already left such men out.

An old game fades, as in the NBA, here with a half-life of 20 of the side's
games. The margin barely moves (11.72 to 11.72, 11.79 to 11.78) but the total
does (16.76 to 16.70 and 17.41 to 17.34). And a quarter of a side's
three-point makes above the league's rate is taken back out as luck, as in the
NBA: with the rest in place the margin's miss goes from 11.69 to 11.67 and
from 11.73 to 11.69, the total's from 16.70 to 16.64 and from 17.34 to 17.28.

Left out, each tested on the same games: a cap on one game's margin (worse at
35, 28 and 22: in college a rout carries news); possessions as a product
beside the two sides' own paces (the ratings already carry pace, and the
product added nothing held out); altitude (a high host beat the projection by
1.1 against lowland visitors, but lowland hosts beat it by 1.7 against high
visitors, so it is not the air); a home edge of its own for each side (a
side's home miss in earlier seasons predicts its next at a correlation of 0.17)
or for each conference (which was the conference split above in disguise).
Together the split home edge, the fade and the roster prior take the margin's
miss over the four seasons from 11.66 to 11.53 (12.45 to 12.26 in the first 45
days) and the total's from 16.92 to 16.85, before the stand-in for the report.
Conviction in the card is in each sport's own spread, so college sits beside
the others on the same scale; with 360 teams and few games each, its early
gaps from the book are noisy, which the per-sport trust weight and the record
by sport are there to show.

### College football: what was tried

College football got rain (above, on the NFL's number) and the quarterback's
4.9 points from the NFL's fit, and nothing else of its own, because the rest
either did not hold out or could not be fit from what is free to read.

Tested on eight seasons of FBS games (5,705, from cfbfastR's schedules, each
projected from earlier weeks alone) and left out: a home edge of its own
outside the conference (hosts beat the projection by 2.6 points there, give or
take 0.9, but using it helped two held-out seasons by 0.03 and hurt a third by
as much; the flag is stored with each game all the same); altitude (1.8 points
for a high host against a lowland visitor, give or take 1.4, and anywhere from
-4.5 to +9.0 by season); and fading old games (the margin's miss gets worse at
every half-life tried). The stand-in for an injury report was fit for the
quarterback on two seasons of box scores: a regular starter who did not throw
in his side's last game is still out the next time 64.5% of the time (259
cases), but the margin moved 3.2 points a case in 2024 and -0.7 in 2025 (1.2
together, give or take 1.4), so it is not used. The same stand-in in the NFL
is worth 3.8 points, give or take 1.2, over five seasons, where the injury
report already says it better.

Not fit, for want of a source the scanner or the fits can read for nothing: a
wind and rain coefficient of college's own (no free history of game weather);
the conferences' own availability reports, which the SEC, the Big 12 and the
Big Ten now publish before each game and which would replace the stand-in
with the real thing; and a prior from recruiting, returning production or
last season's closing lines (collegefootballdata.com has all three behind a
key). A prior from closing lines was at least tried where the lines are free:
on 16 NFL seasons it missed by 13.44 where last season's results miss by
13.44, so the market's memory of a team is no better than the scoreboard's.
In hockey the two things left on the table are the confirmed starting goalie,
worth three times the guess the scanner makes, and shot quality in place of
shot counts; both live on sites with no free feed.

## Player lines

Every summary the log fetches also carries each player's line, and from
version 3 of the game record the log keeps them (`player_games`, one row per
player per game, with the team, the opponent, home or away and the date, so a
player's season reads back in one query). Basketball keeps minutes, points,
shooting, rebounds, assists, turnovers, steals, blocks and fouls, with a
starter flag and whether the player played at all; hockey keeps skaters'
goals, assists, shots, hits, blocked shots, time on ice and power-play time,
and goalies' saves, shots against and goals against; football keeps passing,
rushing, receiving (targets in the NFL only), fumbles, tackles, sacks,
interceptions and kicking, one line per player across the groups ESPN lists
him in. ESPN's football groups carry no position, so football lines have
none. Games stored before version 3 are re-read with the leftover budget each
pass, the same way older parsers' games were, and the health page's game log
row counts how many games have their player lines until every one does. This is the data under the player projections.

### Player lines

`scanner/players.py` turns those lines into a projection for every leader of
every upcoming game, as a whole distribution rather than one number. A
player's rate for a stat is what he has produced per minute (per second on
the ice in hockey; per game in football, whose box scores give no playing
time), shrunk toward the league rate for his position while his games are few
(72 minutes of league-average play in the NBA, two hours in hockey, two games
in football). In football the league rate is the role's, each team's leaders
in the stat, as many as a side gets lines for: shrunk toward every man who
threw a pass, backups and trick plays included, the lines ran 10 to 60 yards
light. His playing time is projected from his last five games, most recent
weighted most, with a spread from his last ten (floored at four minutes), and
in basketball a projected rout trims it for the men who play most (0.4% a
point of the blend's margin beyond three, up to 8%: in the sample the top half
by minutes played 3-7% less once the margin passed nine, and the bench more).
The game projection says how his team's scoring should differ from its
average against this opponent at this pace, and that scales scoring stats
(points, assists, threes, shots, goals, yards, receptions); rebounds scale by
the pace alone; a goalie's saves by the other side's scoring. The stat is a
negative binomial around that mean, with a dispersion per stat (`DISPERSION`,
and each sport's own in `DISPERSION_BY_SPORT` where its grading differs) that
gives a 25-point scorer the 15-to-35 range real games show, mixed over the
uncertainty in his minutes. The distribution is computed exactly for the
diary, which makes every player of every game affordable each pass, and drawn
100,000 times for the pages from the same parameters.

Only players who played their team's last game are projected (a man who sat
out is most likely hurt, and the box score cannot say) and only after
`PLAYER_MIN_GAMES` (2). Goalies are projected as if they start. Each game's
lines are written when it comes into the 48-hour window and refreshed hourly
(`PLAYER_REFRESH_MINUTES`) until kickoff, then fixed; `SPORT_STATS` says which
stats and how many leaders a side. When the game lands every line is graded
from the player's own line in the box score: the miss against the projected
mean, whether the actual fell above the median (a tie counted as half; should
be near half), whether
it fell inside the 80% band (should be near 80%) and where in the
distribution it fell, so the scorecard's "Player lines" section says whether
the bands mean what they claim, stat by stat. A projected player who did not
play is counted, not graded. The dispersions were set by hand and then moved
once on the first three days of grading (7 November 2026, 3,400 lines):
basketball and hockey counting stats had been too wide and football yards far
too narrow. The same two numbers say when to move them again. `PLAYERS_ENABLED=false` switches it off. Nothing
is sent or placed.

The lines are public, like the card: `/props` lists every upcoming game with
lines and the record so far (lines graded, the share above the median, the
share inside the 80% band, the average miss); `/props/<sport>/<game_id>` is a
game's lines, both sides, a row a player and a column a stat, with the actual
in bold once the game is in; `/props/<sport>/<game_id>/<player_id>` is one
player's lines with each stat's distribution drawn 100,000 times (the 80%
band shaded, the median marked, the actual marked once graded), the chance at
lines either side of the median, the inputs behind it and his recent games;
and `.png` on that address is a share image. The matchup sheet (token) shows
the same table under "Player lines".

The lines are the model's own, not a book's. Polymarket US was checked for
per-game player markets on 3 November 2026 (every league's event listing,
the markets endpoint under its documented filters, and the search endpoint
by player name): the listings carry team markets (the moneyline, spreads,
totals, periods and team totals the scanner already reads), and a player's
name finds only season-long futures (points leader, 100-point scorers). There
is nothing to compare a line to or to grade a gap against, so no prop market
code exists; if Polymarket lists them later, the pre-game scan's pattern
(record the price, compare, grade at the final) is the one to follow. Until
then the record on `/props`, the share of actuals above the median and inside
the band, is what says whether the lines are worth anything.

## The monthly check

Once a month is long enough to say something about the model and short enough
to catch a drift. `scanner/monthly.py` reads every projection graded in a
calendar month back from the diary and sets it against what happened and
against the line the book closed at (the book stored with the finished game):
the Brier score of the model's win chance beside the close's fair moneyline,
how often a side the model gave 60-70% went on to win (and each other 10%
bin), how far the finals landed from the model's margin and total and from the
close's, how many finals fell outside the model's own middle 90% (ten percent
should: more means the sport's spread of results in `TUNING` is too narrow for
the scanner's own projections, fewer too wide), the record of the side the
model took in each market against the closing line (moneyline, spread, total,
the puck line in hockey), and how often the close moved the model's way. The
scorecard shows the last full month and the month so far, sport by sport;
`python -m scanner --monthly-check [--month YYYY-MM]` prints a month and
`--send` sends it to the phone; `--remote` asks the running service to do
both through its `POST /monthly-check` (token required), which is what the
Railway cron in `railway.cron.json` does at 13:30 UTC on the 1st. Nothing in
it places anything.

## The daily card and the simulator

The owner wants to publish research, not tips to tail blindly, and cares most
that the model's few highest-conviction sides each day hold up. `scanner/card.py`
makes that measurable. Once a day, at `CARD_LOCK_TIME` in `CARD_TZ` (10:00
Eastern), the game log's refresh ranks every projected game that starts in the
next 24 hours:

- One side per game, the stronger of its spread (the moneyline in hockey,
  where the puck line never moves) and its total. The side is the one the
  model's raw number takes against the book's line at that moment, the same
  comparison the scorecard's gap rows grade.
- Each side's edge is the blend's chance it comes in, read from the score grid
  (key numbers, overtime and all, at the blend's own spread, given the line
  does not push, since a push is a refund), less the chance the book's price
  needs to break even (52.4% at -110). Every market and sport is then on one
  scale, and half a point across football's 3 counts for more than half a
  point across 11. The side with the bigger edge is the game's. Conviction is
  the edge times a trust weight for the sport: 1 until the sport has 20 closing
  lines that moved, then twice the share of moved lines that moved the model's
  way (1 when the market comes the model's way half the time, which is what no
  information gets; up to 1.5; down to nothing, with no floor, since a sport
  the market moves against nine times in ten has earned no place). A raw
  number more than one game's spread from the book rules its side out: the
  biggest disagreements are the model's errors, and history shows the final
  landing on the line. A side with no edge keeps its edge unweighted, so it
  ranks below every side with one and a thin day still fills its card. Thin
  projections (a side with under 3 games) and games without a book line are
  left out. Until 18 November 2026 the card priced each side from the raw
  model at the sport's average spread, with trust floored at a half, which
  made it a search for the games where the model was most wrong (every pick a
  25-to-42-point underdog claimed at 95-98%, graded a coin flip); before
  November it ranked by the gap in the sport's usual spread of results. Picks
  locked under either rule keep their conviction and say so on their page.
- The top `CARD_PICKS` (5) are written to the diary (`cards`, `card_picks`)
  with the book's line and price at the lock, ranked, and never changed. A day
  with nothing eligible is left unlocked so a later pass can lock it; a card
  locked more than 30 minutes after its time is marked late, and the lock time
  is shown. Each pick is settled at the final from the game log like the stakes
  are (units at one unit a pick at the book's price, a push costs nothing), with
  the closing line's move toward the pick kept beside it.

`/card` is public, no token: the latest card, each pick with its picture, and
the whole record, overall and rank by rank, by sport and by market, with every
past day linked (`/card/<date>`). The record by rank is the point of the
exercise: if rank 1 does no better than rank 5, conviction means nothing, and
the page says so by showing it. The scorecard carries the same section;
`python -m scanner --card` prints it. A big gap from the book is as likely a
missing player as an edge, since the model knows nothing of injuries, rest or
weather, which is why the closing line weighs on the ranking and why misses
are shown with the wins.

`scanner/grid.py` lays out every final score of a game with its exact chance,
from the model's own numbers: a margin and a total from their normal spreads,
on the whole-number scores they can make (a margin and a total share a parity,
and no margin is bigger than the total), times the sport's table of how much
more or less often real games land on each regulation margin and team score
(`scanner/sim_tables.json`, built from history). A level game goes to the
sport's overtime, won by the regulation favourite at its regulation edge scaled
by the table's `overtime_edge`. Last, the two bell curves' centres move just
enough that the mean final margin and total are exactly the projection's: the
projection is fit to final scores, so overtime and the tables change the shape,
never the expectation. Nothing is drawn at random, so a page shows the same
picture every time with no simulation noise. The projection's win chance, the
stakes and the card all read from the grid, so they cannot disagree.

The tables come from history. `scripts/fetch_history.py` gathers every
regular-season game into `tests/fixtures/history/` with what was expected of it
(the NFL's closing spread and total; elsewhere, team ratings fit only on that
season's earlier games, the way the scanner sees a game before kickoff), its
score at the end of regulation and its final. `scripts/build_sim_tables.py`
fits each sport's table by iterative proportional fitting: lay out every game
at its expectation, add the chances up, and scale each margin and team score
by how much more often real games landed there, until they agree; the widths
are fit so the table changes only the shape, and the overtime outcomes and how
much of the favourite's edge survives into overtime are counted from the
overtimes that happened. `scripts/grade_sim.py` prints the report card, and
`tests/test_sim_history.py` fails if a change moves a sport's key numbers, its
level-after-regulation rate or its misplaced margin chance past a tolerance.
The full before-and-after report card is in `docs/sim_report.md`.

Football before and after its tables, on 4,297 NFL games against the close
(2010-2026) and 4,528 FBS games (2018-2025):

| | NFL before | NFL after | NFL real | CFB before | CFB after | CFB real |
|---|---|---|---|---|---|---|
| Margin of exactly 3 | 6.7% | 14.4% | 14.6% | 5.0% | 10.8% | 10.6% |
| Margin of exactly 7 | 5.5% | 8.6% | 8.7% | 4.4% | 8.8% | 8.6% |
| Level after regulation | 2.7% | 5.9% | 6.0% | 2.1% | 4.7% | 4.6% |
| Margin chance on the wrong numbers | 18.9% | 1.6% | | 22.1% | 2.0% | |

The grid can also move a sport's margin table with the size of the expected
margin (`margin_by_spread`, interpolated between a few centres so no price
jumps), and `build_sim_tables.py --spread-only --holdout-from YEAR` fits those
adjustments on earlier seasons and scores them against the single table on the
seasons held out. They ship only when they win by at least two standard errors
of the paired, per-game difference in log loss (`grade_sim.SPREAD_MIN_SE`): a
hair's improvement over two thousand games is noise, and the bar scales with
the count. Neither sport's do. The NFL's, fit on 2010-2019 and scored on
2020-2026, are 1.9 standard errors worse than the single table (3.8276 against
3.8260); in-sample they had looked better (games spread 2.5 to 4 land on 3 at
17.0%, the single table says 15.4%, the adjusted 16.7%), but much of what
looks like spread dependence is era-to-era noise (an NFL margin of 7 ran 8.8%,
9.9%, 7.0% and 8.5% across 2010-14, 2015-19, 2020-22 and 2023-26). College
football's, fit on 2018-2022 and scored on 2023-2025, come out a fifth of a
standard error better, which is nothing. Each check's result is recorded in
the sport's table (`source.spread_check`, the z-score and the bar included),
and `tests/test_sim_history.py` rescores its fingerprint on a fixed sample of
the held-out games with the grid, the widths and the tables as they stand: a
change that leaves the record stale fails the suite until the check is rerun,
the shipped state must match the decision, and the decision must follow the
rule as it stands.

Basketball's tables are its end of game: a tie after regulation, which late
fouling and the last shot make twice as common as a bell curve says, and fewer
one-point games. On 4,730 NBA games (2021-22 to 2024-25) and 15,460 men's
college games (2022-23 to 2024-25), each rated only on earlier games:

| | NBA before | NBA after | NBA real | CBB before | CBB after | CBB real |
|---|---|---|---|---|---|---|
| One-point margin | 5.5% | 4.0% | 4.0% | 5.9% | 4.5% | 4.5% |
| Level after regulation | 2.6% | 5.1% | 5.1% | 2.8% | 6.2% | 6.2% |

The same games set basketball's margin spreads: the model's own ratings miss
the NBA by 13.9 and college by 12.0 when they have seen only earlier games, so
`TUNING` holds 14 and 12 (college was 11, nine percent too narrow). Overtime
goes to the regulation favourite at about half its edge (0.49 NBA, 0.52
college), and adds what real overtimes added, by count.

Hockey's bell curves are the goals before the net empties. A side ahead by one
then scores into the empty net 47% of the time, by two 65%, by three 28% (from
the league's own goal records), which is what makes three-goal games more
common than two-goal ones. Overtime and the shootout go to the favourite at
under a quarter of its regulation edge (0.23): favourites won 52.8% of 1,088
overtimes. On 5,030 NHL games (2021-22 to 2024-25), each rated only on earlier
games:

| | Before | After | Real |
|---|---|---|---|
| One-goal margin | 44.8% | 39.6% | 39.1% |
| Two-goal margin | 23.3% | 20.0% | 19.8% |
| Three-goal margin | 16.0% | 23.7% | 23.9% |
| Winner by two or more (the puck line) | 55.2% | 60.4% | 60.9% |
| Level after regulation | 15.7% | 22.0% | 21.8% |

Hockey's win chances are still too confident at the ends (a side given 80-90%
won 67% of these games), which is the ratings, not the shape: projected margins
from goals alone overshoot by about a quarter (by half early in a season). The
scanner's own ratings add shots, which these games do not have, so its
shrinkage is left to be fit on its own graded record.

`scanner/simulate.py` turns the grid into what the pages draw, as counts out of
100,000 games: how often each margin and total lands, the likeliest finals and
how often the pick comes in. The card shows a histogram per pick with the
book's line and the model's mean marked and the pick's side shaded; the pick's
own page (`/card/<date>/<rank>`) adds the total, a heatmap of every final score,
the ten likeliest, and the chance at other lines. `/sim/<sport>/<game_id>`
(token) does the same for any projected game. It is a picture of the model, not
a better model. Nothing in it is sent or placed.

For posting, `scanner/share.py` draws the same things as 1200 by 675 images
(X's shape, served at twice that for sharpness): `/card.png` and
`/card/<date>.png` are the day's card, every pick with its call, price, sim
chance and result once graded, and the running record; `/card/<date>/<rank>.png`
is one pick with its histogram. They are drawn as SVG and rasterised with
cairosvg, so the Docker image carries `libcairo2` and DejaVu Sans; nothing is
fetched while an image is drawn, so there are no logos, only the teams' colours.
The pages link to them.

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
