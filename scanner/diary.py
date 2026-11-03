"""The SQLite diary: every alert, near miss, follow-up price and outcome.

One file on the persistent volume. Everything the scorecard shows comes from
here, so the numbers survive a restart and can be checked by hand with the
sqlite3 command line.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scanner.config import FOLLOW_UP_SECONDS
from scanner.models import (
    Alert,
    AlertType,
    GameState,
    GameStatus,
    League,
    NearMiss,
    Observation,
    PregameGap,
    PregameLineRecord,
    Side,
)
from scanner.periods import decide, from_json, is_period_pick

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    league TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    pick TEXT NOT NULL,
    pick_side TEXT,
    side_label TEXT NOT NULL,
    fair_price REAL NOT NULL,
    model_price REAL,
    espn_price REAL,
    buy_price REAL NOT NULL,
    fee REAL NOT NULL,
    edge REAL NOT NULL,
    dollars_available REAL NOT NULL,
    average_price REAL,
    situation TEXT NOT NULL,
    polymarket_score TEXT,
    polymarket_score_differs INTEGER NOT NULL DEFAULT 0,
    line REAL,
    combined_score INTEGER,
    message TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0,
    followup_30 REAL,
    followup_30_at TEXT,
    followup_120 REAL,
    followup_120_at TEXT,
    outcome TEXT,
    settlement REAL,
    result_per_contract REAL,
    final_home INTEGER,
    final_away INTEGER,
    graded_at TEXT
);
CREATE TABLE IF NOT EXISTS near_misses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    league TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    pick TEXT NOT NULL,
    reason TEXT NOT NULL,
    fair_price REAL,
    buy_price REAL,
    edge REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    league TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    pick TEXT NOT NULL,
    pick_side TEXT NOT NULL,
    minutes_left REAL NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    fair_price REAL,
    model_price REAL,
    espn_price REAL,
    buy_price REAL,
    fee REAL,
    edge REAL,
    dollars_available REAL,
    would_alert INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    outcome TEXT,
    settlement REAL,
    result_per_contract REAL,
    graded_at TEXT
);
CREATE TABLE IF NOT EXISTS pregame_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL,
    sport TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    start_time TEXT,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    book TEXT NOT NULL,
    polymarket TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pregame_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    sport TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    start_time TEXT,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    market TEXT NOT NULL,
    pick TEXT NOT NULL,
    pick_side TEXT NOT NULL,
    line REAL,
    market_slug TEXT NOT NULL,
    side_label TEXT NOT NULL,
    buy_price REAL NOT NULL,
    fee REAL NOT NULL,
    book_fair REAL NOT NULL,
    book_odds INTEGER,
    edge REAL NOT NULL,
    provider TEXT,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    latest_buy REAL,
    latest_edge REAL,
    max_edge REAL,
    closing_fair REAL,
    closing_odds INTEGER,
    clv REAL,
    closed_at TEXT,
    outcome TEXT,
    settlement REAL,
    result_per_contract REAL,
    final_home INTEGER,
    final_away INTEGER,
    graded_at TEXT
);
-- football_games, football_weeks and football_upcoming hold every sport with a game
-- log (the NBA and NHL too); the names date from when football was the only one. For
-- a sport ESPN lists by day, "week" is the day as YYYYMMDD.
CREATE TABLE IF NOT EXISTS football_games (
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    season INTEGER,
    week INTEGER,
    date TEXT NOT NULL,
    neutral INTEGER NOT NULL DEFAULT 0,
    home_id TEXT NOT NULL,
    home_abbr TEXT NOT NULL,
    home_name TEXT NOT NULL,
    away_id TEXT NOT NULL,
    away_abbr TEXT NOT NULL,
    away_name TEXT NOT NULL,
    home_score INTEGER NOT NULL,
    away_score INTEGER NOT NULL,
    home_lines TEXT NOT NULL,
    away_lines TEXT NOT NULL,
    home_stats TEXT NOT NULL,
    away_stats TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (sport, game_id)
);
CREATE TABLE IF NOT EXISTS player_games (
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    team_abbr TEXT NOT NULL,
    opponent_id TEXT NOT NULL,
    opponent_abbr TEXT NOT NULL,
    home INTEGER NOT NULL,
    season INTEGER,
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    position TEXT,
    starter INTEGER,
    played INTEGER NOT NULL,
    stats TEXT NOT NULL,
    PRIMARY KEY (sport, game_id, player_id)
);
CREATE INDEX IF NOT EXISTS player_games_by_player ON player_games (sport, player_id, date);
CREATE TABLE IF NOT EXISTS football_weeks (
    sport TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sport, season, week)
);
CREATE TABLE IF NOT EXISTS football_upcoming (
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    date TEXT NOT NULL,
    slate TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    summary_fetched_at TEXT,
    PRIMARY KEY (sport, game_id)
);
CREATE TABLE IF NOT EXISTS projections (
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    model TEXT NOT NULL,
    made_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    projection TEXT NOT NULL,
    first_projection TEXT NOT NULL,
    outcome TEXT,
    grades TEXT,
    graded_at TEXT,
    PRIMARY KEY (sport, game_id)
);
CREATE TABLE IF NOT EXISTS stakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    side_label TEXT NOT NULL,
    line REAL,
    buy_price REAL NOT NULL,
    fee REAL NOT NULL,
    model_prob REAL NOT NULL,
    edge REAL NOT NULL,
    kelly REAL NOT NULL,
    share REAL NOT NULL,
    stake REAL NOT NULL,
    contracts REAL NOT NULL,
    bankroll REAL NOT NULL,
    price_at TEXT,
    gate_open INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    settlement REAL,
    profit REAL,
    final_home INTEGER,
    final_away INTEGER,
    graded_at TEXT,
    UNIQUE (sport, game_id, market)
);
CREATE TABLE IF NOT EXISTS player_projections (
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    stat TEXT NOT NULL,
    team_id TEXT NOT NULL,
    team_abbr TEXT NOT NULL,
    opponent_abbr TEXT NOT NULL,
    home INTEGER NOT NULL,
    name TEXT NOT NULL,
    position TEXT,
    kickoff TEXT NOT NULL,
    model TEXT NOT NULL,
    made_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    mean REAL NOT NULL,
    sd REAL NOT NULL,
    median INTEGER NOT NULL,
    p10 INTEGER NOT NULL,
    p90 INTEGER NOT NULL,
    params TEXT NOT NULL,
    pmf TEXT NOT NULL,
    outcome TEXT,
    actual INTEGER,
    error REAL,
    pit REAL,
    in_band INTEGER,
    over_median INTEGER,
    graded_at TEXT,
    PRIMARY KEY (sport, game_id, player_id, stat)
);
CREATE INDEX IF NOT EXISTS player_projections_open ON player_projections (sport, outcome, kickoff);
CREATE TABLE IF NOT EXISTS cards (
    card_date TEXT PRIMARY KEY,
    locked_at TEXT NOT NULL,
    picks INTEGER NOT NULL,
    late INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS card_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_date TEXT NOT NULL,
    locked_at TEXT NOT NULL,
    rank INTEGER NOT NULL,
    sport TEXT NOT NULL,
    game_id TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    side_label TEXT NOT NULL,
    line REAL,
    odds INTEGER NOT NULL,
    price REAL NOT NULL,
    model_value REAL NOT NULL,
    book_value REAL NOT NULL,
    gap REAL NOT NULL,
    z REAL NOT NULL,
    trust REAL NOT NULL,
    conviction REAL NOT NULL,
    detail TEXT NOT NULL,
    outcome TEXT,
    units REAL,
    final_home INTEGER,
    final_away INTEGER,
    closing_value REAL,
    clv REAL,
    graded_at TEXT,
    UNIQUE (card_date, rank),
    UNIQUE (card_date, sport, game_id)
);
CREATE INDEX IF NOT EXISTS alerts_game ON alerts (league, feed_id);
CREATE INDEX IF NOT EXISTS near_misses_time ON near_misses (created_at);
CREATE INDEX IF NOT EXISTS observations_game ON observations (league, feed_id);
CREATE INDEX IF NOT EXISTS pregame_lines_game ON pregame_lines (sport, feed_id, id);
CREATE INDEX IF NOT EXISTS pregame_gaps_game ON pregame_gaps (sport, feed_id);
CREATE INDEX IF NOT EXISTS football_games_season ON football_games (sport, season, date);
CREATE INDEX IF NOT EXISTS projections_open ON projections (sport, outcome, kickoff);
"""

ALERT_TYPES = (AlertType.WINNER.value, AlertType.CLINCHED_OVER.value, AlertType.PERIOD.value)

# The stored game format. Rows written by an older parser are re-read once so that
# fields the parser has since learned (first-half yards, the closing line) fill in.
# Bump it when parse_game_summary learns something worth a re-read.
FOOTBALL_GAME_VERSION = 3  # 3: per-player lines are kept beside the team box scores
# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = (
    ("football_games", "version", "INTEGER NOT NULL DEFAULT 1"),
    ("football_games", "book", "TEXT"),
)

OUTCOME_WIN = "win"
OUTCOME_LOSS = "loss"
OUTCOME_TIE = "tie"
OUTCOME_PUSH = "push"
OUTCOME_NOT_GRADED = "not_graded"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


@dataclass(frozen=True, slots=True)
class FollowUpDue:
    alert_id: int
    seconds: int
    market_slug: str
    side_label: str
    due_at: datetime


class Diary:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns that newer code expects to tables an older diary already has."""
        for table, column, definition in MIGRATIONS:
            present = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- alerts -------------------------------------------------------------

    def record_alert(self, alert: Alert, sent: bool) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO alerts (created_at, alert_type, league, feed_id, event_slug,
                   market_slug, home, away, pick, pick_side, side_label, fair_price, model_price,
                   espn_price, buy_price, fee, edge, dollars_available, average_price, situation,
                   polymarket_score, polymarket_score_differs, line, combined_score, message,
                   enabled, sent)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _iso(alert.created_at),
                    alert.alert_type.value,
                    alert.league.value,
                    alert.feed_id,
                    alert.event_slug,
                    alert.market_slug,
                    alert.home,
                    alert.away,
                    alert.pick,
                    alert.pick_side,
                    alert.side_label,
                    alert.fair_price,
                    alert.model_price,
                    alert.espn_price,
                    alert.buy_price,
                    alert.fee,
                    alert.edge,
                    alert.dollars_available,
                    alert.average_price,
                    json.dumps(alert.situation, default=str),
                    alert.polymarket_score,
                    int(alert.polymarket_score_differs),
                    alert.line,
                    alert.combined_score,
                    alert.message,
                    int(alert.enabled),
                    int(sent),
                ),
            )
            return int(cur.lastrowid)

    def alert(self, alert_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return dict(row) if row else None

    def alerts(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def alerts_since(self, since: datetime) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE created_at >= ? ORDER BY id", (_iso(since),)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- near misses --------------------------------------------------------

    def record_near_miss(self, miss: NearMiss) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO near_misses (created_at, league, feed_id, event_slug, pick,
                   reason, fair_price, buy_price, edge) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    _iso(miss.created_at),
                    miss.league.value,
                    miss.feed_id,
                    miss.event_slug,
                    miss.pick,
                    miss.reason,
                    miss.fair_price,
                    miss.buy_price,
                    miss.edge,
                ),
            )
            return int(cur.lastrowid)

    def last_near_miss(self, league: League, feed_id: str, pick: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM near_misses WHERE league = ? AND feed_id = ? AND pick = ?
                   ORDER BY id DESC LIMIT 1""",
                (league.value, feed_id, pick),
            ).fetchone()
        return dict(row) if row else None

    def near_misses_by_type(self, since: datetime | None = None) -> dict[str, dict[str, int]]:
        """Near-miss counts by alert type ("winner" or "clinched_over") and reason."""
        with self._lock:
            if since is None:
                rows = self._conn.execute(
                    "SELECT pick, reason, COUNT(*) AS n FROM near_misses GROUP BY pick, reason"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT pick, reason, COUNT(*) AS n FROM near_misses WHERE created_at >= ? "
                    "GROUP BY pick, reason",
                    (_iso(since),),
                ).fetchall()
        out: dict[str, dict[str, int]] = {kind: {} for kind in ALERT_TYPES}
        for r in rows:
            out[_kind_of_pick(str(r["pick"]))][r["reason"]] = out[
                _kind_of_pick(str(r["pick"]))
            ].get(r["reason"], 0) + int(r["n"])
        return out

    def near_misses_by_reason(self, since: datetime | None = None) -> dict[str, int]:
        with self._lock:
            if since is None:
                rows = self._conn.execute(
                    "SELECT reason, COUNT(*) AS n FROM near_misses GROUP BY reason"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT reason, COUNT(*) AS n FROM near_misses WHERE created_at >= ? "
                    "GROUP BY reason",
                    (_iso(since),),
                ).fetchall()
        return {r["reason"]: int(r["n"]) for r in rows}

    # -- observations (the window just outside the late-game filter) ----------

    def record_observation(self, obs: Observation) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO observations (created_at, league, feed_id, event_slug, pick,
                   pick_side, minutes_left, home_score, away_score, fair_price, model_price,
                   espn_price, buy_price, fee, edge, dollars_available, would_alert, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _iso(obs.created_at),
                    obs.league.value,
                    obs.feed_id,
                    obs.event_slug,
                    obs.pick,
                    obs.pick_side,
                    obs.minutes_left,
                    obs.home_score,
                    obs.away_score,
                    obs.fair_price,
                    obs.model_price,
                    obs.espn_price,
                    obs.buy_price,
                    obs.fee,
                    obs.edge,
                    obs.dollars_available,
                    int(obs.would_alert),
                    obs.reason,
                ),
            )
            return int(cur.lastrowid)

    def observations_since(self, since: datetime | None = None) -> list[dict]:
        with self._lock:
            if since is None:
                rows = self._conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM observations WHERE created_at >= ? ORDER BY id", (_iso(since),)
                ).fetchall()
        return [dict(r) for r in rows]

    def observation_summary(self, since: datetime | None = None) -> dict:
        """What the window outside the late-game filter would have done.

        Each game and pick counts once, at its first "would alert" check, so a game
        checked every 30 seconds for five minutes is one paper trade, not ten.
        """
        rows = self.observations_since(since)
        games = {(r["league"], r["feed_id"]) for r in rows}
        fired = [r for r in rows if r["would_alert"]]
        first: dict[tuple[str, str, str], dict] = {}
        for r in fired:
            first.setdefault((r["league"], r["feed_id"], r["pick"]), r)
        picks = list(first.values())
        graded = [r for r in picks if r["outcome"] in (OUTCOME_WIN, OUTCOME_LOSS, OUTCOME_TIE)]
        wins = sum(1 for r in graded if r["outcome"] == OUTCOME_WIN)
        losses = sum(1 for r in graded if r["outcome"] == OUTCOME_LOSS)
        ties = sum(1 for r in graded if r["outcome"] == OUTCOME_TIE)
        priced = [r for r in picks if r["buy_price"] is not None]
        needed = (
            sum(r["buy_price"] + (r["fee"] or 0.0) for r in priced) / len(priced)
            if priced
            else None
        )
        actual = (wins + 0.5 * ties) / len(graded) if graded else None
        profit = sum(
            100 * r["result_per_contract"] for r in graded if r["result_per_contract"] is not None
        )
        reasons = Counter(r["reason"] for r in rows if not r["would_alert"])
        return {
            "rows": len(rows),
            "games": len(games),
            "would_alert_rows": len(fired),
            "picks": len(picks),
            "graded": len(graded),
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate_needed": needed,
            "actual_win_rate": actual,
            "profit_per_100": profit,
            "reasons": dict(reasons.most_common(8)),
        }

    # -- the pre-game scan ----------------------------------------------------

    def record_pregame_line(self, record: PregameLineRecord) -> bool:
        """Store a game's lines unless they equal the last stored reading. True if stored."""
        book = json.dumps(record.book, sort_keys=True, default=str)
        polymarket = json.dumps(record.polymarket, sort_keys=True, default=str)
        with self._lock:
            last = self._conn.execute(
                "SELECT book, polymarket FROM pregame_lines WHERE sport = ? AND feed_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (record.sport, record.feed_id),
            ).fetchone()
            if last is not None and last["book"] == book and last["polymarket"] == polymarket:
                return False
            self._conn.execute(
                """INSERT INTO pregame_lines (scanned_at, sport, feed_id, event_slug, start_time,
                   home, away, book, polymarket) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    _iso(record.scanned_at),
                    record.sport,
                    record.feed_id,
                    record.event_slug,
                    _iso(record.start) if record.start else None,
                    record.home,
                    record.away,
                    book,
                    polymarket,
                ),
            )
            return True

    def last_pregame_line(
        self, sport: str, feed_id: str, before: datetime | None = None
    ) -> dict | None:
        """The last stored reading of a game's lines, optionally at or before a moment."""
        with self._lock:
            if before is None:
                row = self._conn.execute(
                    "SELECT * FROM pregame_lines WHERE sport = ? AND feed_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (sport, feed_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM pregame_lines WHERE sport = ? AND feed_id = ? "
                    "AND scanned_at <= ? ORDER BY id DESC LIMIT 1",
                    (sport, feed_id, _iso(before)),
                ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["book"] = json.loads(out["book"])
        out["polymarket"] = json.loads(out["polymarket"])
        return out

    def upsert_pregame_gap(self, gap: PregameGap, now: datetime) -> str:
        """Insert a gap, or refresh the open one for the same game, market and pick.

        The first price seen is the would-be wager and never changes; later scans
        only update what the gap looks like now. Returns "new" or "updated".
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT id, max_edge FROM pregame_gaps WHERE sport = ? AND feed_id = ?
                   AND market = ? AND pick = ? AND outcome IS NULL ORDER BY id DESC LIMIT 1""",
                (gap.sport, gap.feed_id, gap.market, gap.pick),
            ).fetchone()
            if row is not None:
                max_edge = max(float(row["max_edge"] or 0.0), gap.edge)
                self._conn.execute(
                    """UPDATE pregame_gaps SET last_seen_at = ?, seen_count = seen_count + 1,
                       latest_buy = ?, latest_edge = ?, max_edge = ? WHERE id = ?""",
                    (_iso(now), gap.buy_price, gap.edge, max_edge, row["id"]),
                )
                return "updated"
            self._conn.execute(
                """INSERT INTO pregame_gaps (created_at, sport, feed_id, event_slug, start_time,
                   home, away, market, pick, pick_side, line, market_slug, side_label, buy_price,
                   fee, book_fair, book_odds, edge, provider, last_seen_at, seen_count,
                   latest_buy, latest_edge, max_edge)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (
                    _iso(gap.created_at),
                    gap.sport,
                    gap.feed_id,
                    gap.event_slug,
                    _iso(gap.start) if gap.start else None,
                    gap.home,
                    gap.away,
                    gap.market,
                    gap.pick,
                    gap.pick_side,
                    gap.line,
                    gap.market_slug,
                    gap.side_label,
                    gap.buy_price,
                    gap.fee,
                    gap.book_fair,
                    gap.book_odds,
                    gap.edge,
                    gap.provider,
                    _iso(now),
                    gap.buy_price,
                    gap.edge,
                    gap.edge,
                ),
            )
            return "new"

    def pregame_gaps(self, since: datetime | None = None, limit: int = 1000) -> list[dict]:
        with self._lock:
            if since is None:
                rows = self._conn.execute(
                    "SELECT * FROM pregame_gaps ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM pregame_gaps WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                    (_iso(since), limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def pregame_gaps_unclosed(self, now: datetime) -> list[dict]:
        """Gaps whose game has started and whose closing line is not yet recorded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pregame_gaps WHERE closed_at IS NULL AND start_time IS NOT NULL "
                "AND start_time <= ? ORDER BY id",
                (_iso(now),),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_pregame_closing(
        self,
        gap_id: int,
        closing_fair: float | None,
        closing_odds: int | None,
        clv: float | None,
        at: datetime,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pregame_gaps SET closing_fair = ?, closing_odds = ?, clv = ?, "
                "closed_at = ? WHERE id = ?",
                (closing_fair, closing_odds, clv, _iso(at), gap_id),
            )

    def pregame_gaps_ungraded(self, started_before: datetime) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pregame_gaps WHERE outcome IS NULL AND start_time IS NOT NULL "
                "AND start_time <= ? ORDER BY id",
                (_iso(started_before),),
            ).fetchall()
        return [dict(r) for r in rows]

    def grade_pregame_gap(
        self,
        gap_id: int,
        outcome: str,
        settlement: float | None,
        final_home: int | None,
        final_away: int | None,
        at: datetime,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT buy_price, fee FROM pregame_gaps WHERE id = ?", (gap_id,)
            ).fetchone()
            result = None
            if row is not None and settlement is not None:
                result = settlement - row["buy_price"] - row["fee"]
            self._conn.execute(
                """UPDATE pregame_gaps SET outcome = ?, settlement = ?, result_per_contract = ?,
                   final_home = ?, final_away = ?, graded_at = ? WHERE id = ?""",
                (outcome, settlement, result, final_home, final_away, _iso(at), gap_id),
            )

    def open_pregame_gap_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM pregame_gaps WHERE outcome IS NULL"
            ).fetchone()
        return int(row["n"]) if row else 0

    def pregame_summary(self, since: datetime | None = None) -> dict:
        """What the pre-game scan would have bought, and how it did."""
        rows = self.pregame_gaps(since, limit=100000)
        graded = [r for r in rows if r["outcome"] in (OUTCOME_WIN, OUTCOME_LOSS, OUTCOME_PUSH)]
        wins = sum(1 for r in graded if r["outcome"] == OUTCOME_WIN)
        losses = sum(1 for r in graded if r["outcome"] == OUTCOME_LOSS)
        pushes = sum(1 for r in graded if r["outcome"] == OUTCOME_PUSH)
        needed = sum(r["buy_price"] + r["fee"] for r in rows) / len(rows) if rows else None
        actual = (wins + 0.5 * pushes) / len(graded) if graded else None
        profit = sum(
            100 * r["result_per_contract"] for r in graded if r["result_per_contract"] is not None
        )
        closed = [r for r in rows if r["clv"] is not None]
        avg_edge = sum(r["edge"] for r in rows) / len(rows) if rows else None
        avg_clv = sum(r["clv"] for r in closed) / len(closed) if closed else None
        positive = sum(1 for r in closed if r["clv"] > 0)
        recent = [
            {
                "created_at": r["created_at"],
                "sport": r["sport"],
                "home": r["home"],
                "away": r["away"],
                "pick": r["pick"],
                "market": r["market"],
                "buy_price": r["buy_price"],
                "book_fair": r["book_fair"],
                "book_odds": r["book_odds"],
                "edge": r["edge"],
                "clv": r["clv"],
                "outcome": r["outcome"],
            }
            for r in rows[:10]
        ]
        return {
            "gaps": len(rows),
            "by_sport": dict(Counter(r["sport"] for r in rows)),
            "by_market": dict(Counter(r["market"] for r in rows)),
            "graded": len(graded),
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "not_graded": sum(1 for r in rows if r["outcome"] == OUTCOME_NOT_GRADED),
            "win_rate_needed": needed,
            "actual_win_rate": actual,
            "profit_per_100": profit,
            "avg_edge": avg_edge,
            "closed": len(closed),
            "avg_clv": avg_clv,
            "positive_clv_share": (positive / len(closed)) if closed else None,
            "recent": recent,
        }

    # -- the football game log --------------------------------------------------

    def store_football_game(self, record, at: datetime) -> None:
        """Keep a finished game's box scores. Replaces an earlier copy of the same game."""
        book = json.dumps(record.book.as_dict()) if record.book is not None else None
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO football_games (sport, game_id, season, week, date,
                   neutral, home_id, home_abbr, home_name, away_id, away_abbr, away_name,
                   home_score, away_score, home_lines, away_lines, home_stats, away_stats,
                   fetched_at, version, book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.sport,
                    record.game_id,
                    record.season,
                    record.week,
                    _iso(record.date),
                    int(record.neutral),
                    record.home.team_id,
                    record.home.abbreviation,
                    record.home.name,
                    record.away.team_id,
                    record.away.abbreviation,
                    record.away.name,
                    record.home_score,
                    record.away_score,
                    json.dumps(list(record.home_lines)),
                    json.dumps(list(record.away_lines)),
                    json.dumps(record.home_stats),
                    json.dumps(record.away_stats),
                    _iso(at),
                    FOOTBALL_GAME_VERSION,
                    book,
                ),
            )
            if not record.players:
                return  # a record without lines (one read back from here) keeps what is stored
            self._conn.execute(
                "DELETE FROM player_games WHERE sport = ? AND game_id = ?",
                (record.sport, record.game_id),
            )
            sides = {
                record.home.team_id: (record.home, record.away, 1),
                record.away.team_id: (record.away, record.home, 0),
            }
            for line in record.players:
                if line.team_id not in sides:
                    continue
                team, opponent, home = sides[line.team_id]
                self._conn.execute(
                    """INSERT OR REPLACE INTO player_games (sport, game_id, player_id, team_id,
                       team_abbr, opponent_id, opponent_abbr, home, season, date, name, position,
                       starter, played, stats) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record.sport,
                        record.game_id,
                        line.player_id,
                        line.team_id,
                        team.abbreviation,
                        opponent.team_id,
                        opponent.abbreviation,
                        home,
                        record.season,
                        _iso(record.date),
                        line.name,
                        line.position,
                        None if line.starter is None else int(line.starter),
                        int(line.played),
                        json.dumps(line.stats),
                    ),
                )

    @staticmethod
    def _player_row(row) -> dict:
        out = dict(row)
        out["stats"] = json.loads(out["stats"])
        out["home"] = bool(out["home"])
        out["played"] = bool(out["played"])
        out["starter"] = None if out["starter"] is None else bool(out["starter"])
        return out

    def player_games(
        self, sport: str, season: int | None = None, team_id: str | None = None
    ) -> list[dict]:
        """Every stored player line of a sport (a season's, a team's), oldest game first."""
        where, params = ["sport = ?"], [sport]
        if season is not None:
            where.append("season = ?")
            params.append(season)
        if team_id is not None:
            where.append("team_id = ?")
            params.append(team_id)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM player_games WHERE {' AND '.join(where)} ORDER BY date, game_id",
                params,
            ).fetchall()
        return [self._player_row(r) for r in rows]

    def player_lines_for_game(self, sport: str, game_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM player_games WHERE sport = ? AND game_id = ? ORDER BY team_id, name",
                (sport, game_id),
            ).fetchall()
        return [self._player_row(r) for r in rows]

    def player_game_counts(self) -> dict[str, int]:
        """Stored games with player lines, per sport (the re-read fills older games in)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT sport, COUNT(DISTINCT game_id) AS n FROM player_games GROUP BY sport"
            ).fetchall()
        return {r["sport"]: int(r["n"]) for r in rows}

    def football_game_ids(self, sport: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT game_id FROM football_games WHERE sport = ?", (sport,)
            ).fetchall()
        return {str(r["game_id"]) for r in rows}

    def football_games_stale(self, sport: str, limit: int | None = None) -> list[str]:
        """Games stored by an older parser, oldest first: the ones worth a re-read."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT game_id FROM football_games WHERE sport = ? AND version < ? "
                "ORDER BY date LIMIT ?",
                (sport, FOOTBALL_GAME_VERSION, -1 if limit is None else limit),
            ).fetchall()
        return [str(r["game_id"]) for r in rows]

    def mark_football_game_version(self, sport: str, game_id: str, version: int) -> None:
        """Record which parser a stored game went through (without re-storing it)."""
        with self._lock:
            self._conn.execute(
                "UPDATE football_games SET version = ? WHERE sport = ? AND game_id = ?",
                (version, sport, game_id),
            )

    def football_games(self, sport: str, season: int | None = None) -> list:
        """Every stored game of a season (or all seasons), as GameRecord objects, oldest first."""
        from scanner.books import BookLine
        from scanner.gamelog import GameRecord, TeamRef

        with self._lock:
            if season is None:
                rows = self._conn.execute(
                    "SELECT * FROM football_games WHERE sport = ? ORDER BY date", (sport,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM football_games WHERE sport = ? AND season = ? ORDER BY date",
                    (sport, season),
                ).fetchall()
        out = []
        for r in rows:
            out.append(
                GameRecord(
                    sport=r["sport"],
                    game_id=r["game_id"],
                    season=r["season"],
                    week=r["week"],
                    date=datetime.fromisoformat(r["date"]),
                    neutral=bool(r["neutral"]),
                    home=TeamRef(r["home_id"], r["home_abbr"], r["home_name"]),
                    away=TeamRef(r["away_id"], r["away_abbr"], r["away_name"]),
                    home_score=r["home_score"],
                    away_score=r["away_score"],
                    home_lines=tuple(json.loads(r["home_lines"])),
                    away_lines=tuple(json.loads(r["away_lines"])),
                    home_stats=json.loads(r["home_stats"]),
                    away_stats=json.loads(r["away_stats"]),
                    book=BookLine.from_dict(json.loads(r["book"])) if r["book"] else None,
                )
            )
        return out

    def football_game_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sport, COUNT(*) AS n FROM football_games GROUP BY sport"
            ).fetchall()
        return {r["sport"]: int(r["n"]) for r in rows}

    def football_week_complete(self, sport: str, season: int, week: int) -> bool:
        """Whether a week's (or, by day, a day's) games are all stored."""
        with self._lock:
            row = self._conn.execute(
                "SELECT complete FROM football_weeks WHERE sport = ? AND season = ? AND week = ?",
                (sport, season, week),
            ).fetchone()
        return bool(row and row["complete"])

    def mark_football_week(
        self, sport: str, season: int, week: int, complete: bool, at: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO football_weeks (sport, season, week, fetched_at, complete) "
                "VALUES (?,?,?,?,?)",
                (sport, season, week, _iso(at), int(complete)),
            )

    def store_football_upcoming(self, game, at: datetime) -> None:
        """Keep (or refresh) an upcoming game's scoreboard entry; the summary extras stay."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO football_upcoming (sport, game_id, date, slate, fetched_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(sport, game_id) DO UPDATE SET date = excluded.date,
                   slate = excluded.slate, fetched_at = excluded.fetched_at""",
                (game.sport, game.game_id, _iso(game.date), json.dumps(game.as_dict()), _iso(at)),
            )

    def update_football_upcoming_extra(
        self, sport: str, game_id: str, extra: dict, at: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE football_upcoming SET extra = ?, summary_fetched_at = ? "
                "WHERE sport = ? AND game_id = ?",
                (json.dumps(extra), _iso(at), sport, game_id),
            )

    def delete_football_upcoming_before(self, sport: str, before: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM football_upcoming WHERE sport = ? AND date < ?", (sport, _iso(before))
            )

    @staticmethod
    def _upcoming_row(row) -> dict:
        out = dict(row)
        out["slate"] = json.loads(out["slate"])
        out["extra"] = json.loads(out["extra"] or "{}")
        return out

    def football_upcoming(self, sport: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM football_upcoming WHERE sport = ? ORDER BY date", (sport,)
            ).fetchall()
        return [self._upcoming_row(r) for r in rows]

    def football_upcoming_game(self, sport: str, game_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM football_upcoming WHERE sport = ? AND game_id = ?", (sport, game_id)
            ).fetchone()
        return self._upcoming_row(row) if row else None

    # -- the projection model's record ------------------------------------------

    def upsert_projection(
        self,
        sport: str,
        game_id: str,
        kickoff: datetime,
        home: str,
        away: str,
        projection: dict,
        at: datetime,
    ) -> None:
        """Write a game's projection; later passes update it until it is graded.

        The first projection is kept beside the latest so a later look can tell
        what the model said when the game first came into view.
        """
        body = json.dumps(projection)
        with self._lock:
            self._conn.execute(
                """INSERT INTO projections (sport, game_id, kickoff, home, away, model,
                   made_at, updated_at, projection, first_projection)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sport, game_id) DO UPDATE SET kickoff = excluded.kickoff,
                   home = excluded.home, away = excluded.away, model = excluded.model,
                   updated_at = excluded.updated_at, projection = excluded.projection
                   WHERE projections.outcome IS NULL""",
                (
                    sport,
                    game_id,
                    _iso(kickoff),
                    home,
                    away,
                    str(projection.get("model") or ""),
                    _iso(at),
                    _iso(at),
                    body,
                    body,
                ),
            )

    @staticmethod
    def _projection_row(row) -> dict:
        out = dict(row)
        out["projection"] = json.loads(out["projection"])
        out["first_projection"] = json.loads(out["first_projection"])
        out["grades"] = json.loads(out["grades"]) if out.get("grades") else None
        return out

    def projection(self, sport: str, game_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projections WHERE sport = ? AND game_id = ?", (sport, game_id)
            ).fetchone()
        return self._projection_row(row) if row else None

    def open_projections(self, sport: str) -> dict[str, dict]:
        """Ungraded projections by game id: the ones the matchups list shows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projections WHERE sport = ? AND outcome IS NULL", (sport,)
            ).fetchall()
        return {str(r["game_id"]): self._projection_row(r) for r in rows}

    def grade_projections(
        self, sport: str, now: datetime, grace_hours: float = 4.0, give_up_days: float = 10.0
    ) -> int:
        """Grade projections whose game is in the log; give up on games that never land."""
        from scanner.books import BookLine
        from scanner.projection import grade

        graded = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projections WHERE sport = ? AND outcome IS NULL AND kickoff < ?",
                (sport, _iso(now - timedelta(hours=grace_hours))),
            ).fetchall()
            for row in rows:
                game = self._conn.execute(
                    "SELECT home_score, away_score, book FROM football_games "
                    "WHERE sport = ? AND game_id = ?",
                    (sport, row["game_id"]),
                ).fetchone()
                if game is None:
                    if datetime.fromisoformat(row["kickoff"]) < now - timedelta(days=give_up_days):
                        self._conn.execute(
                            "UPDATE projections SET outcome = ?, graded_at = ? "
                            "WHERE sport = ? AND game_id = ?",
                            (OUTCOME_NOT_GRADED, _iso(now), sport, row["game_id"]),
                        )
                    continue
                close = BookLine.from_dict(json.loads(game["book"])) if game["book"] else None
                grades = grade(
                    json.loads(row["projection"]), game["home_score"], game["away_score"], close
                )
                self._conn.execute(
                    "UPDATE projections SET outcome = 'graded', grades = ?, graded_at = ? "
                    "WHERE sport = ? AND game_id = ?",
                    (json.dumps(grades), _iso(now), sport, row["game_id"]),
                )
                graded += 1
        return graded

    def projection_summary(self, since: datetime | None = None, sport: str | None = None) -> dict:
        """How the model has done against the final scores and the book."""
        from scanner.projection import summarise

        where, params = ["outcome = 'graded'"], []
        if since is not None:
            where.append("kickoff >= ?")
            params.append(_iso(since))
        if sport is not None:
            where.append("sport = ?")
            params.append(sport)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM projections WHERE {' AND '.join(where)} ORDER BY kickoff DESC",
                params,
            ).fetchall()
            open_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM projections WHERE outcome IS NULL"
                + ("" if sport is None else " AND sport = ?"),
                () if sport is None else (sport,),
            ).fetchone()["n"]
        rows = [self._projection_row(r) for r in rows]
        out = summarise([r["grades"] for r in rows], sport)
        out["open"] = int(open_count)
        out["by_sport"] = dict(Counter(r["sport"] for r in rows))
        if sport is None:
            # Misses and gap rows are in each sport's own unit, so the scorecard
            # shows them sport by sport.
            out["sports"] = {
                key: summarise([r["grades"] for r in rows if r["sport"] == key], key)
                for key in sorted(out["by_sport"])
            }
        out["recent"] = [
            {
                "kickoff": r["kickoff"],
                "sport": r["sport"],
                "home": r["home"],
                "away": r["away"],
                "raw_margin": r["projection"]["raw"]["margin"],
                "raw_total": r["projection"]["raw"]["total"],
                "book_margin": (r["projection"].get("book") or {}).get("margin"),
                "book_total": (r["projection"].get("book") or {}).get("total"),
                "final": r["grades"]["final"],
                "ats": r["grades"].get("ats"),
                "ou": r["grades"].get("ou"),
            }
            for r in rows[:10]
        ]
        return out

    # -- what the model would stake ------------------------------------------------

    def sync_stakes(
        self,
        sport: str,
        game_id: str,
        kickoff: datetime,
        home: str,
        away: str,
        suggestions: list[dict],
        gate_open: bool,
        bankroll: float,
        price_at: str | None,
        at: datetime,
    ) -> int:
        """Make a game's open stake rows match this pass's suggestions.

        One row per market: written or updated while the game is still to come,
        dropped when the edge has gone, locked at kickoff and left to be graded.
        """
        if kickoff <= at:
            return 0
        with self._lock:
            kept = []
            for row in suggestions:
                kept.append(row["market"])
                self._conn.execute(
                    """INSERT INTO stakes (created_at, updated_at, sport, game_id, kickoff, home,
                       away, market, side, side_label, line, buy_price, fee, model_prob, edge,
                       kelly, share, stake, contracts, bankroll, price_at, gate_open)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(sport, game_id, market) DO UPDATE SET
                       updated_at = excluded.updated_at, kickoff = excluded.kickoff,
                       side = excluded.side, side_label = excluded.side_label,
                       line = excluded.line, buy_price = excluded.buy_price, fee = excluded.fee,
                       model_prob = excluded.model_prob, edge = excluded.edge,
                       kelly = excluded.kelly, share = excluded.share, stake = excluded.stake,
                       contracts = excluded.contracts, bankroll = excluded.bankroll,
                       price_at = excluded.price_at, gate_open = excluded.gate_open
                       WHERE stakes.outcome IS NULL""",
                    (
                        _iso(at),
                        _iso(at),
                        sport,
                        game_id,
                        _iso(kickoff),
                        home,
                        away,
                        row["market"],
                        row["side"],
                        row["side_label"],
                        row["line"],
                        row["buy_price"],
                        row["fee"],
                        row["model_prob"],
                        row["edge"],
                        row["kelly"],
                        row["share"],
                        row["stake"],
                        row["contracts"],
                        bankroll,
                        price_at,
                        int(gate_open),
                    ),
                )
            placeholders = ",".join("?" for _ in kept) or "''"
            self._conn.execute(
                f"DELETE FROM stakes WHERE sport = ? AND game_id = ? AND outcome IS NULL "
                f"AND market NOT IN ({placeholders})",
                (sport, game_id, *kept),
            )
        return len(kept)

    def stakes_for(self, sport: str, game_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stakes WHERE sport = ? AND game_id = ? ORDER BY edge DESC",
                (sport, game_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def grade_stakes(self, sport: str, now: datetime, grace_hours: float = 4.0) -> int:
        """Settle open stakes whose game is in the log; grade like the alerts are graded."""
        from scanner.stakes import profit, settle

        graded = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stakes WHERE sport = ? AND outcome IS NULL AND kickoff < ?",
                (sport, _iso(now - timedelta(hours=grace_hours))),
            ).fetchall()
            for row in rows:
                game = self._conn.execute(
                    "SELECT home_score, away_score FROM football_games "
                    "WHERE sport = ? AND game_id = ?",
                    (sport, row["game_id"]),
                ).fetchone()
                if game is None:
                    if datetime.fromisoformat(row["kickoff"]) < now - timedelta(days=10):
                        self._conn.execute(
                            "UPDATE stakes SET outcome = ?, graded_at = ? WHERE id = ?",
                            (OUTCOME_NOT_GRADED, _iso(now), row["id"]),
                        )
                    continue
                outcome, settlement = settle(
                    row["market"], row["side"], row["line"], game["home_score"], game["away_score"]
                )
                gain = profit(dict(row), settlement) if outcome != OUTCOME_NOT_GRADED else None
                self._conn.execute(
                    """UPDATE stakes SET outcome = ?, settlement = ?, profit = ?, final_home = ?,
                       final_away = ?, graded_at = ? WHERE id = ?""",
                    (
                        outcome,
                        settlement,
                        gain,
                        game["home_score"],
                        game["away_score"],
                        _iso(now),
                        row["id"],
                    ),
                )
                graded += 1
        return graded

    def stake_summary(self, since: datetime | None = None) -> dict:
        """What the model would have staked, and how that paper money did."""
        with self._lock:
            if since is None:
                rows = self._conn.execute("SELECT * FROM stakes ORDER BY kickoff DESC").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM stakes WHERE kickoff >= ? ORDER BY kickoff DESC", (_iso(since),)
                ).fetchall()
        rows = [dict(r) for r in rows]
        graded = [r for r in rows if r["outcome"] in ("win", "loss", "push")]
        staked = sum(r["stake"] for r in graded)
        gain = sum(r["profit"] or 0.0 for r in graded)
        by_market = {}
        for market in ("moneyline", "total", "spread"):
            subset = [r for r in graded if r["market"] == market]
            by_market[market] = {
                "suggested": sum(1 for r in rows if r["market"] == market),
                "wins": sum(1 for r in subset if r["outcome"] == "win"),
                "losses": sum(1 for r in subset if r["outcome"] == "loss"),
                "pushes": sum(1 for r in subset if r["outcome"] == "push"),
                "profit": sum(r["profit"] or 0.0 for r in subset),
            }
        return {
            "suggested": len(rows),
            "open": sum(1 for r in rows if r["outcome"] is None),
            "shown": sum(1 for r in rows if r["gate_open"]),
            "graded": len(graded),
            "not_graded": sum(1 for r in rows if r["outcome"] == OUTCOME_NOT_GRADED),
            "wins": sum(1 for r in graded if r["outcome"] == "win"),
            "losses": sum(1 for r in graded if r["outcome"] == "loss"),
            "pushes": sum(1 for r in graded if r["outcome"] == "push"),
            "staked": staked,
            "profit": gain,
            "roi": (gain / staked) if staked else None,
            "avg_edge": (sum(r["edge"] for r in rows) / len(rows)) if rows else None,
            "by_market": by_market,
            "recent": [
                {
                    "kickoff": r["kickoff"],
                    "sport": r["sport"],
                    "home": r["home"],
                    "away": r["away"],
                    "market": r["market"],
                    "side_label": r["side_label"],
                    "buy_price": r["buy_price"],
                    "model_prob": r["model_prob"],
                    "edge": r["edge"],
                    "stake": r["stake"],
                    "outcome": r["outcome"],
                    "profit": r["profit"],
                    "gate_open": bool(r["gate_open"]),
                }
                for r in rows[:10]
            ],
        }

    # -- player lines ---------------------------------------------------------------------

    def replace_player_projections(
        self, sport: str, game_id: str, rows: list[dict], at: datetime
    ) -> int:
        """Make a game's open player lines match this pass's projections, keeping made_at."""
        with self._lock:
            graded = {
                (r["player_id"], r["stat"])
                for r in self._conn.execute(
                    "SELECT player_id, stat FROM player_projections WHERE sport = ? AND "
                    "game_id = ? AND outcome IS NOT NULL",
                    (sport, game_id),
                ).fetchall()
            }
            if graded:
                return 0  # the game has landed: its lines are fixed
            made = {
                (r["player_id"], r["stat"]): r["made_at"]
                for r in self._conn.execute(
                    "SELECT player_id, stat, made_at FROM player_projections WHERE sport = ? "
                    "AND game_id = ?",
                    (sport, game_id),
                ).fetchall()
            }
            self._conn.execute(
                "DELETE FROM player_projections WHERE sport = ? AND game_id = ?", (sport, game_id)
            )
            for row in rows:
                key = (row["player_id"], row["stat"])
                self._conn.execute(
                    """INSERT INTO player_projections (sport, game_id, player_id, stat, team_id,
                       team_abbr, opponent_abbr, home, name, position, kickoff, model, made_at,
                       updated_at, mean, sd, median, p10, p90, params, pmf)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sport,
                        game_id,
                        row["player_id"],
                        row["stat"],
                        row["team_id"],
                        row["team_abbr"],
                        row["opponent_abbr"],
                        int(row["home"]),
                        row["name"],
                        row["position"],
                        _iso(row["kickoff"]),
                        row["model"],
                        made.get(key, _iso(at)),
                        _iso(at),
                        row["mean"],
                        row["sd"],
                        row["median"],
                        row["p10"],
                        row["p90"],
                        json.dumps(row["params"]),
                        json.dumps(row["pmf"]),
                    ),
                )
        return len(rows)

    @staticmethod
    def _player_projection_row(row) -> dict:
        out = dict(row)
        out["params"] = json.loads(out["params"])
        out["pmf"] = json.loads(out["pmf"])
        out["home"] = bool(out["home"])
        for key in ("in_band", "over_median"):
            out[key] = None if out[key] is None else bool(out[key])
        return out

    def player_projections(self, sport: str, game_id: str) -> list[dict]:
        """A game's player lines, the home side first, each side's leaders by mean."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM player_projections WHERE sport = ? AND game_id = ? "
                "ORDER BY home DESC, stat, mean DESC",
                (sport, game_id),
            ).fetchall()
        return [self._player_projection_row(r) for r in rows]

    def player_projection(self, sport: str, game_id: str, player_id: str, stat: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM player_projections WHERE sport = ? AND game_id = ? AND "
                "player_id = ? AND stat = ?",
                (sport, game_id, player_id, stat),
            ).fetchone()
        return self._player_projection_row(row) if row else None

    def player_projection_counts(self, sport: str) -> dict[str, int]:
        """How many open player lines each upcoming game of a sport has."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT game_id, COUNT(*) AS n FROM player_projections WHERE sport = ? AND "
                "outcome IS NULL GROUP BY game_id",
                (sport,),
            ).fetchall()
        return {str(r["game_id"]): int(r["n"]) for r in rows}

    def player_recent_games(self, sport: str, player_id: str, limit: int = 10) -> list[dict]:
        """A player's last games in the log, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM player_games WHERE sport = ? AND player_id = ? "
                "ORDER BY date DESC, game_id DESC LIMIT ?",
                (sport, player_id, limit),
            ).fetchall()
        return [self._player_row(r) for r in rows]

    def player_projection_age(self, sport: str, game_id: str) -> datetime | None:
        """When a game's player lines were last written, or None when it has none."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(updated_at) AS at FROM player_projections WHERE sport = ? AND "
                "game_id = ?",
                (sport, game_id),
            ).fetchone()
        return datetime.fromisoformat(row["at"]) if row and row["at"] else None

    def grade_player_projections(
        self, sport: str, now: datetime, grace_hours: float = 4.0, give_up_days: float = 10.0
    ) -> int:
        """Grade player lines whose game is in the log, from the player's own line in it."""
        from scanner.players import grade_line

        graded = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM player_projections WHERE sport = ? AND outcome IS NULL "
                "AND kickoff < ?",
                (sport, _iso(now - timedelta(hours=grace_hours))),
            ).fetchall()
            for row in rows:
                game = self._conn.execute(
                    "SELECT 1 FROM football_games WHERE sport = ? AND game_id = ?",
                    (sport, row["game_id"]),
                ).fetchone()
                if game is None:
                    if datetime.fromisoformat(row["kickoff"]) < now - timedelta(days=give_up_days):
                        self._conn.execute(
                            "UPDATE player_projections SET outcome = ?, graded_at = ? WHERE "
                            "sport = ? AND game_id = ? AND player_id = ? AND stat = ?",
                            (OUTCOME_NOT_GRADED, _iso(now), sport, row["game_id"],
                             row["player_id"], row["stat"]),
                        )  # fmt: skip
                    continue
                line = self._conn.execute(
                    "SELECT played, stats FROM player_games WHERE sport = ? AND game_id = ? "
                    "AND player_id = ?",
                    (sport, row["game_id"], row["player_id"]),
                ).fetchone()
                actual = None
                if line is not None and line["played"]:
                    value = json.loads(line["stats"]).get(row["stat"])
                    actual = int(round(value)) if isinstance(value, int | float) else None
                grade = grade_line(self._player_projection_row(row), actual)
                self._conn.execute(
                    """UPDATE player_projections SET outcome = ?, actual = ?, error = ?, pit = ?,
                       in_band = ?, over_median = ?, graded_at = ? WHERE sport = ? AND
                       game_id = ? AND player_id = ? AND stat = ?""",
                    (
                        grade["outcome"],
                        grade.get("actual"),
                        grade.get("error"),
                        grade.get("pit"),
                        None if grade.get("in_band") is None else int(grade["in_band"]),
                        None if grade.get("over_median") is None else int(grade["over_median"]),
                        _iso(now),
                        sport,
                        row["game_id"],
                        row["player_id"],
                        row["stat"],
                    ),
                )
                graded += 1
        return graded

    def player_projection_summary(self, since: datetime | None = None) -> dict:
        """How the player lines have done: the miss, the bias and the calibration, by stat."""
        from scanner.players import summarise_grades

        where, params = ["outcome IS NOT NULL"], []
        if since is not None:
            where.append("kickoff >= ?")
            params.append(_iso(since))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sport, stat, outcome, actual, error, pit, in_band, over_median, median, "
                f"mean FROM player_projections WHERE {' AND '.join(where)}",
                params,
            ).fetchall()
            open_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM player_projections WHERE outcome IS NULL"
            ).fetchone()["n"]
            recent = self._conn.execute(
                "SELECT sport, game_id, kickoff, name, team_abbr, opponent_abbr, stat, mean, "
                "median, p10, p90, actual, pit FROM player_projections WHERE outcome = 'graded' "
                "ORDER BY kickoff DESC, mean DESC LIMIT 12"
            ).fetchall()
        graded = []
        for r in rows:
            d = dict(r)
            if d["outcome"] == "graded":
                d["abs_error"] = abs(d["error"])
                d["on_median"] = d["actual"] == d["median"]
            graded.append(d)
        out = summarise_grades(graded)
        out["open"] = int(open_count)
        out["recent"] = [dict(r) for r in recent]
        return out

    # -- the daily card ---------------------------------------------------------------

    def record_card(
        self, card_date: str, locked_at: datetime, late: bool, note: str | None, picks: list[dict]
    ) -> None:
        """Lock a day's card: its picks are written once and never updated."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO cards (card_date, locked_at, picks, late, note) "
                "VALUES (?,?,?,?,?)",
                (card_date, _iso(locked_at), len(picks), int(late), note),
            )
            for row in picks:
                self._conn.execute(
                    """INSERT OR IGNORE INTO card_picks (card_date, locked_at, rank, sport,
                       game_id, kickoff, home, away, market, side, side_label, line, odds, price,
                       model_value, book_value, gap, z, trust, conviction, detail)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        card_date,
                        _iso(locked_at),
                        row["rank"],
                        row["sport"],
                        row["game_id"],
                        _iso(row["kickoff"]),
                        row["home"],
                        row["away"],
                        row["market"],
                        row["side"],
                        row["side_label"],
                        row["line"],
                        row["odds"],
                        row["price"],
                        row["model_value"],
                        row["book_value"],
                        row["gap"],
                        row["z"],
                        row["trust"],
                        row["conviction"],
                        json.dumps(row["detail"]),
                    ),
                )

    @staticmethod
    def _card_pick_row(row) -> dict:
        out = dict(row)
        out["detail"] = json.loads(out["detail"]) if out.get("detail") else {}
        return out

    def card_exists(self, card_date: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM cards WHERE card_date = ?", (card_date,)
            ).fetchone()
        return row is not None

    def latest_card_date(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(card_date) AS d FROM cards").fetchone()
        return row["d"] if row and row["d"] else None

    def card(self, card_date: str) -> dict | None:
        """A day's card with its picks in rank order, or None when that day has none."""
        with self._lock:
            head = self._conn.execute(
                "SELECT * FROM cards WHERE card_date = ?", (card_date,)
            ).fetchone()
            if head is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM card_picks WHERE card_date = ? ORDER BY rank", (card_date,)
            ).fetchall()
        out = dict(head)
        out["late"] = bool(out["late"])
        out["rows"] = [self._card_pick_row(r) for r in rows]
        return out

    def card_pick(self, card_date: str, rank: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM card_picks WHERE card_date = ? AND rank = ?", (card_date, rank)
            ).fetchone()
        return self._card_pick_row(row) if row else None

    def card_history(self, limit: int = 60) -> list[dict]:
        """Each day's card with its record, newest first."""
        from scanner.card import _tally

        with self._lock:
            heads = self._conn.execute(
                "SELECT * FROM cards ORDER BY card_date DESC LIMIT ?", (limit,)
            ).fetchall()
            if not heads:
                return []
            oldest = heads[-1]["card_date"]
            rows = self._conn.execute(
                "SELECT card_date, rank, sport, market, outcome, units, clv FROM card_picks "
                "WHERE card_date >= ?",
                (oldest,),
            ).fetchall()
        by_day: dict[str, list[dict]] = {}
        for r in rows:
            by_day.setdefault(r["card_date"], []).append(dict(r))
        out = []
        for head in heads:
            day = dict(head)
            day["late"] = bool(day["late"])
            picks = by_day.get(day["card_date"], [])
            day.update(_tally(picks))
            day["open"] = sum(1 for p in picks if p["outcome"] is None)
            out.append(day)
        return out

    def grade_card_picks(
        self, now: datetime, grace_hours: float = 4.0, give_up_days: float = 10.0
    ) -> int:
        """Settle picks whose game is in the log; give up on games that never land."""
        from scanner.books import BookLine
        from scanner.card import closing_for, settle_pick

        graded = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM card_picks WHERE outcome IS NULL AND kickoff < ?",
                (_iso(now - timedelta(hours=grace_hours)),),
            ).fetchall()
            for row in rows:
                game = self._conn.execute(
                    "SELECT home_score, away_score, book FROM football_games "
                    "WHERE sport = ? AND game_id = ?",
                    (row["sport"], row["game_id"]),
                ).fetchone()
                if game is None:
                    if datetime.fromisoformat(row["kickoff"]) < now - timedelta(days=give_up_days):
                        self._conn.execute(
                            "UPDATE card_picks SET outcome = ?, graded_at = ? WHERE id = ?",
                            (OUTCOME_NOT_GRADED, _iso(now), row["id"]),
                        )
                    continue
                outcome, units = settle_pick(dict(row), game["home_score"], game["away_score"])
                close = BookLine.from_dict(json.loads(game["book"])) if game["book"] else None
                closing, clv = closing_for(dict(row), close)
                self._conn.execute(
                    """UPDATE card_picks SET outcome = ?, units = ?, final_home = ?,
                       final_away = ?, closing_value = ?, clv = ?, graded_at = ? WHERE id = ?""",
                    (
                        outcome,
                        units,
                        game["home_score"],
                        game["away_score"],
                        closing,
                        clv,
                        _iso(now),
                        row["id"],
                    ),
                )
                graded += 1
        return graded

    def card_summary(self, since: datetime | None = None, picks: int = 5) -> dict:
        """The card's whole record, for the scorecard and the public page."""
        from scanner.card import summarise

        with self._lock:
            if since is None:
                rows = self._conn.execute(
                    "SELECT * FROM card_picks ORDER BY card_date DESC, rank"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM card_picks WHERE kickoff >= ? ORDER BY card_date DESC, rank",
                    (_iso(since),),
                ).fetchall()
            days = self._conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
        rows = [dict(r) for r in rows]
        out = summarise(rows, picks)
        out["days"] = int(days)
        out["recent"] = [
            {
                k: r[k]
                for k in (
                    "card_date",
                    "rank",
                    "kickoff",
                    "sport",
                    "home",
                    "away",
                    "market",
                    "side_label",
                    "odds",
                    "z",
                    "conviction",
                    "outcome",
                    "units",
                    "final_home",
                    "final_away",
                    "clv",
                )
            }
            for r in rows
            if r["outcome"] in ("win", "loss", "push")
        ][:10]
        return out

    # -- events (heartbeats, failures, paused messages) ----------------------

    def log_event(self, kind: str, detail: str, at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (created_at, kind, detail) VALUES (?,?,?)",
                (_iso(at), kind, detail),
            )

    def events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- follow-up prices ---------------------------------------------------

    def pending_followups(self, now: datetime) -> list[FollowUpDue]:
        """Alerts whose 30 second or 2 minute re-read is due and not yet stored."""
        horizon = now - timedelta(hours=6)
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, created_at, market_slug, side_label, followup_30_at, followup_120_at
                   FROM alerts WHERE created_at >= ?""",
                (_iso(horizon),),
            ).fetchall()
        due: list[FollowUpDue] = []
        for row in rows:
            created = _parse(row["created_at"])
            if created is None:
                continue
            for seconds, column in zip(
                FOLLOW_UP_SECONDS, ("followup_30_at", "followup_120_at"), strict=True
            ):
                if row[column] is None and now >= created + timedelta(seconds=seconds):
                    due.append(
                        FollowUpDue(
                            int(row["id"]),
                            seconds,
                            row["market_slug"],
                            row["side_label"],
                            created + timedelta(seconds=seconds),
                        )
                    )
        return due

    def record_followup(
        self, alert_id: int, seconds: int, price: float | None, at: datetime
    ) -> None:
        column = "followup_30" if seconds == FOLLOW_UP_SECONDS[0] else "followup_120"
        with self._lock:
            self._conn.execute(
                f"UPDATE alerts SET {column} = ?, {column}_at = ? WHERE id = ?",
                (price, _iso(at), alert_id),
            )

    # -- grading ------------------------------------------------------------

    def ungraded_games(self) -> set[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT league, feed_id FROM alerts WHERE outcome IS NULL "
                "UNION SELECT DISTINCT league, feed_id FROM observations WHERE outcome IS NULL"
            ).fetchall()
        return {(r["league"], r["feed_id"]) for r in rows}

    def grade_game(self, state: GameState, at: datetime) -> int:
        """Grade every ungraded alert and observation on this game. Returns how many."""
        if state.status not in (GameStatus.FINAL, GameStatus.POSTPONED):
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE league = ? AND feed_id = ? AND outcome IS NULL",
                (state.league.value, state.feed_id),
            ).fetchall()
            graded = 0
            for row in rows:
                outcome, settlement = self._outcome(dict(row), state)
                result = None if settlement is None else settlement - row["buy_price"] - row["fee"]
                self._conn.execute(
                    """UPDATE alerts SET outcome = ?, settlement = ?, result_per_contract = ?,
                       final_home = ?, final_away = ?, graded_at = ? WHERE id = ?""",
                    (
                        outcome,
                        settlement,
                        result,
                        state.home_score,
                        state.away_score,
                        _iso(at),
                        row["id"],
                    ),
                )
                graded += 1
            observations = self._conn.execute(
                "SELECT * FROM observations WHERE league = ? AND feed_id = ? AND outcome IS NULL",
                (state.league.value, state.feed_id),
            ).fetchall()
            for row in observations:
                if state.status is GameStatus.POSTPONED or state.home_score is None:
                    outcome, settlement = OUTCOME_NOT_GRADED, None
                else:
                    outcome, settlement = self._winner_outcome(row["pick_side"], state)
                result = None
                if settlement is not None and row["buy_price"] is not None:
                    result = settlement - row["buy_price"] - (row["fee"] or 0.0)
                self._conn.execute(
                    """UPDATE observations SET outcome = ?, settlement = ?,
                       result_per_contract = ?, graded_at = ? WHERE id = ?""",
                    (outcome, settlement, result, _iso(at), row["id"]),
                )
                graded += 1
        return graded

    @staticmethod
    def _outcome(row: dict, state: GameState) -> tuple[str, float | None]:
        if (
            state.status is GameStatus.POSTPONED
            or state.home_score is None
            or state.away_score is None
        ):
            return OUTCOME_NOT_GRADED, None
        if row["alert_type"] == AlertType.CLINCHED_OVER.value:
            if row["line"] is None:
                return OUTCOME_NOT_GRADED, None
            won = state.home_score + state.away_score > row["line"]
            return (OUTCOME_WIN, 1.0) if won else (OUTCOME_LOSS, 0.0)
        if row["alert_type"] == AlertType.PERIOD.value:
            return Diary._period_outcome(row, state)
        return Diary._winner_outcome(row["pick_side"], state)

    @staticmethod
    def _winner_outcome(pick_side: str | None, state: GameState) -> tuple[str, float | None]:
        if state.home_score is None or state.away_score is None:
            return OUTCOME_NOT_GRADED, None
        if pick_side not in (Side.HOME.value, Side.AWAY.value):
            return OUTCOME_NOT_GRADED, None
        if state.home_score == state.away_score:
            if state.league is League.NFL:
                return OUTCOME_TIE, 0.5
            return OUTCOME_NOT_GRADED, None  # college games do not end tied
        winner = Side.HOME.value if state.home_score > state.away_score else Side.AWAY.value
        return (OUTCOME_WIN, 1.0) if pick_side == winner else (OUTCOME_LOSS, 0.0)

    @staticmethod
    def _period_outcome(row: dict, state: GameState) -> tuple[str, float | None]:
        """Decide the market again from the final per-period scores."""
        try:
            situation = json.loads(row.get("situation") or "{}")
        except ValueError:
            return OUTCOME_NOT_GRADED, None
        parsed = from_json(situation.get("period_market") if isinstance(situation, dict) else None)
        if parsed is None:
            return OUTCOME_NOT_GRADED, None
        market, sides = parsed
        decision = decide(market, state, sides)
        if decision is None:
            return OUTCOME_NOT_GRADED, None  # no per-period scores at the final, or a push
        won = decision.side_label == row["side_label"]
        return (OUTCOME_WIN, 1.0) if won else (OUTCOME_LOSS, 0.0)

    # -- scorecard ----------------------------------------------------------

    def scorecard(self, since: datetime | None = None) -> dict:
        """Every number on the scorecard page, split by alert type and league."""
        with self._lock:
            if since is None:
                rows = [dict(r) for r in self._conn.execute("SELECT * FROM alerts").fetchall()]
            else:
                rows = [
                    dict(r)
                    for r in self._conn.execute(
                        "SELECT * FROM alerts WHERE created_at >= ?", (_iso(since),)
                    ).fetchall()
                ]
        sections = {}
        for alert_type in ALERT_TYPES:
            subset = [r for r in rows if r["alert_type"] == alert_type]
            sections[alert_type] = self._summarise(subset)
        return {
            "generated_at": _iso(datetime.now(UTC)),
            "since": _iso(since) if since else None,
            "alerts_total": len(rows),
            "alerts_sent": sum(1 for r in rows if r["sent"]),
            "by_type": sections,
            "near_misses": self.near_misses_by_reason(since),
            "near_misses_by_type": self.near_misses_by_type(since),
            "observations": self.observation_summary(since),
            "pregame": self.pregame_summary(since),
            "projections": self.projection_summary(since),
            "stakes": self.stake_summary(since),
            "card": self.card_summary(since),
            "players": self.player_projection_summary(since),
        }

    @staticmethod
    def _summarise(rows: list[dict]) -> dict:
        graded = [r for r in rows if r["outcome"] in (OUTCOME_WIN, OUTCOME_LOSS, OUTCOME_TIE)]
        wins = sum(1 for r in graded if r["outcome"] == OUTCOME_WIN)
        losses = sum(1 for r in graded if r["outcome"] == OUTCOME_LOSS)
        ties = sum(1 for r in graded if r["outcome"] == OUTCOME_TIE)
        needed = sum(r["buy_price"] + r["fee"] for r in rows) / len(rows) if rows else None
        actual = (wins + 0.5 * ties) / len(graded) if graded else None
        profit = sum(100 * r["result_per_contract"] for r in graded) if graded else 0.0
        with_followup = [r for r in rows if r["followup_30_at"] is not None]
        still_there = [
            r
            for r in with_followup
            if r["followup_30"] is not None and r["followup_30"] <= r["buy_price"] + 1e-9
        ]
        by_league = Counter(r["league"] for r in rows)
        return {
            "alerts": len(rows),
            "sent": sum(1 for r in rows if r["sent"]),
            "by_league": dict(by_league),
            "graded": len(graded),
            "not_graded": sum(1 for r in rows if r["outcome"] == OUTCOME_NOT_GRADED),
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate_needed": needed,
            "actual_win_rate": actual,
            "profit_per_100": profit,
            "followups_checked": len(with_followup),
            "still_available_at_30s": (len(still_there) / len(with_followup))
            if with_followup
            else None,
        }

    # -- restart ------------------------------------------------------------

    def restore_history(self, history, now: datetime, period_history=None) -> int:
        """Reload today's alerts into the AlertHistory objects after a restart.

        Quarter and half alerts go to ``period_history`` (their own repeat gap and
        daily cap) and are skipped when none is given.
        """
        since = now - timedelta(hours=36)
        rows = self.alerts_since(since)
        count = 0
        for row in rows:
            created = _parse(row["created_at"])
            if created is None:
                continue
            if row["alert_type"] == AlertType.PERIOD.value:
                if period_history is None:
                    continue
                period_history.record(_alert_stub(row, created))
            else:
                history.record(_alert_stub(row, created))
            count += 1
        return count


def _kind_of_pick(pick: str) -> str:
    """Which alert type a pick string belongs to; near misses carry only the pick."""
    if pick.startswith("OVER "):
        return AlertType.CLINCHED_OVER.value
    if is_period_pick(pick):
        return AlertType.PERIOD.value
    return AlertType.WINNER.value


def _alert_stub(row: dict, created: datetime) -> Alert:
    """Just enough of an Alert for AlertHistory.record."""
    return Alert(
        alert_type=AlertType(row["alert_type"]),
        league=League(row["league"]),
        created_at=created,
        feed_id=row["feed_id"],
        event_slug=row["event_slug"],
        market_slug=row["market_slug"],
        home=row["home"],
        away=row["away"],
        pick=row["pick"],
        side_label=row["side_label"],
        fair_price=row["fair_price"],
        model_price=row["model_price"],
        espn_price=row["espn_price"],
        buy_price=row["buy_price"],
        fee=row["fee"],
        edge=row["edge"],
        dollars_available=row["dollars_available"],
        average_price=row["average_price"],
    )
