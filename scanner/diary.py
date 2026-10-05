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
from scanner.models import Alert, AlertType, GameState, GameStatus, League, NearMiss, Side

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
CREATE INDEX IF NOT EXISTS alerts_game ON alerts (league, feed_id);
CREATE INDEX IF NOT EXISTS near_misses_time ON near_misses (created_at);
"""

OUTCOME_WIN = "win"
OUTCOME_LOSS = "loss"
OUTCOME_TIE = "tie"
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
                "SELECT DISTINCT league, feed_id FROM alerts WHERE outcome IS NULL"
            ).fetchall()
        return {(r["league"], r["feed_id"]) for r in rows}

    def grade_game(self, state: GameState, at: datetime) -> int:
        """Grade every ungraded alert on this game. Returns how many were graded."""
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
        if row["pick_side"] not in (Side.HOME.value, Side.AWAY.value):
            return OUTCOME_NOT_GRADED, None
        if state.home_score == state.away_score:
            if state.league is League.NFL:
                return OUTCOME_TIE, 0.5
            return OUTCOME_NOT_GRADED, None  # college games do not end tied
        winner = Side.HOME.value if state.home_score > state.away_score else Side.AWAY.value
        return (OUTCOME_WIN, 1.0) if row["pick_side"] == winner else (OUTCOME_LOSS, 0.0)

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
        for alert_type in (AlertType.WINNER.value, AlertType.CLINCHED_OVER.value):
            subset = [r for r in rows if r["alert_type"] == alert_type]
            sections[alert_type] = self._summarise(subset)
        return {
            "generated_at": _iso(datetime.now(UTC)),
            "since": _iso(since) if since else None,
            "alerts_total": len(rows),
            "alerts_sent": sum(1 for r in rows if r["sent"]),
            "by_type": sections,
            "near_misses": self.near_misses_by_reason(since),
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

    def restore_history(self, history, now: datetime) -> int:
        """Reload today's alerts into an AlertHistory after a restart."""
        since = now - timedelta(hours=36)
        rows = self.alerts_since(since)
        count = 0
        for row in rows:
            created = _parse(row["created_at"])
            if created is None:
                continue
            history.record(_alert_stub(row, created))
            count += 1
        return count


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
