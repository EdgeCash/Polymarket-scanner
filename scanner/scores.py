"""ESPN scoreboard client. The only module that knows ESPN's shape.

The feed is unofficial and undocumented, so every field is validated. A game
whose core fields are missing or malformed comes back with
``status == UNKNOWN`` and a reason; nothing downstream alerts on it. Situation
fields (possession, down, yard line, timeouts) are ``None`` when ESPN leaves
them out, which blocks winner alerts but not clinched-over alerts.

Yard lines: ESPN's ``situation.yardLine`` runs from the home team's goal line
(0) to the away team's goal line (100). With the home team in possession the
yards to the end zone are ``100 - yardLine``; with the away team, ``yardLine``.
Confirmed against ESPN play-by-play on 5 October 2026 and cross-checked here
against ``possessionText`` ("CLE 16") whenever it is present.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from scanner.models import GameState, GameStatus, League, Side, Team

log = logging.getLogger(__name__)

SCOREBOARD_URLS = {
    League.NFL: "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    League.CFB: (
        "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
        "?groups=80"
    ),
}

PERIOD_SECONDS = 15 * 60
REGULATION_PERIODS = 4

# ESPN status type names seen on football scoreboards, mapped to our statuses.
STATUS_BY_NAME = {
    "STATUS_SCHEDULED": GameStatus.PRE,
    "STATUS_IN_PROGRESS": GameStatus.LIVE,
    "STATUS_END_PERIOD": GameStatus.LIVE,
    "STATUS_HALFTIME": GameStatus.HALFTIME,
    "STATUS_FINAL": GameStatus.FINAL,
    "STATUS_FULL_TIME": GameStatus.FINAL,
    "STATUS_DELAYED": GameStatus.DELAYED,
    "STATUS_RAIN_DELAY": GameStatus.DELAYED,
    "STATUS_SUSPENDED": GameStatus.DELAYED,
    "STATUS_POSTPONED": GameStatus.POSTPONED,
    "STATUS_CANCELED": GameStatus.POSTPONED,
    "STATUS_FORFEIT": GameStatus.FINAL,
}

_POSSESSION_TEXT = re.compile(r"^\s*([A-Za-z&.\-' ]+?)\s+(\d{1,2})\s*$")


class ScoreFeedError(Exception):
    """The feed could not be read. The caller treats it as 'score feed failing'."""


def _int(value: Any, lo: int | None = None, hi: int | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value or value != int(value):
            return None
        value = int(value)
    if isinstance(value, str):
        if not value.strip().lstrip("-").isdigit():
            return None
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    if lo is not None and value < lo:
        return None
    if hi is not None and value > hi:
        return None
    return value


def _float(value: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:
        return None
    if lo is not None and result < lo:
        return None
    if hi is not None and result > hi:
        return None
    return result


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _team(competitor: dict[str, Any]) -> Team | None:
    team = competitor.get("team")
    if not isinstance(team, dict):
        return None
    abbreviation = team.get("abbreviation")
    name = team.get("displayName") or team.get("name")
    feed_id = team.get("id", competitor.get("id"))
    if not isinstance(abbreviation, str) or not abbreviation.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    location = team.get("location")
    return Team(
        name=name.strip(),
        abbreviation=abbreviation.strip().upper(),
        feed_id=str(feed_id),
        location=location.strip() if isinstance(location, str) and location.strip() else None,
    )


def _unknown(
    league: League,
    feed_id: str,
    home: Team | None,
    away: Team | None,
    kickoff: datetime | None,
    fetched_at: datetime,
    reason: str,
) -> GameState:
    blank = Team(name="?", abbreviation="?", feed_id=None)
    return GameState(
        league=league,
        feed_id=feed_id,
        home=home or blank,
        away=away or blank,
        kickoff=kickoff,
        status=GameStatus.UNKNOWN,
        home_score=None,
        away_score=None,
        period=None,
        clock_seconds=None,
        seconds_left=None,
        possession=None,
        down=None,
        distance=None,
        yards_to_endzone=None,
        home_timeouts=None,
        away_timeouts=None,
        espn_home_win_probability=None,
        home_spread=None,
        fetched_at=fetched_at,
        unknown_reason=reason,
    )


def _yards_to_endzone(
    situation: dict[str, Any], possession: Side, home: Team, away: Team
) -> int | None:
    """Yards to the end zone for the team with the ball, or None if unclear."""
    yard_line = _int(situation.get("yardLine"), 0, 100)
    from_yard_line = None
    if yard_line is not None:
        from_yard_line = 100 - yard_line if possession is Side.HOME else yard_line

    from_text = None
    text = situation.get("possessionText")
    if isinstance(text, str):
        stripped = text.strip()
        if stripped == "50":
            from_text = 50
        else:
            match = _POSSESSION_TEXT.match(stripped)
            if match:
                owner = match.group(1).strip().upper()
                number = int(match.group(2))
                possessor = home if possession is Side.HOME else away
                other = away if possession is Side.HOME else home
                if 0 < number <= 50:
                    if owner == possessor.abbreviation:
                        from_text = 100 - number
                    elif owner == other.abbreviation:
                        from_text = number

    if from_yard_line is not None and from_text is not None and from_yard_line != from_text:
        log.info(
            "yard line disagreement: yardLine=%s says %s, text %r says %s",
            yard_line,
            from_yard_line,
            text,
            from_text,
        )
        return None
    if from_yard_line is not None:
        return from_yard_line
    return from_text


def _spread(competition: dict[str, Any], home: Team, away: Team) -> float | None:
    odds = competition.get("odds")
    if not isinstance(odds, list) or not odds:
        return None
    entry = odds[0]
    if not isinstance(entry, dict):
        return None
    spread = _float(entry.get("spread"), -100, 100)
    details = entry.get("details")
    if spread is None:
        return None
    if isinstance(details, str):
        match = re.match(r"^\s*([A-Z&.\-' ]+?)\s+([+-]?\d+(?:\.\d+)?)\s*$", details.strip())
        if match:
            favourite, number = match.group(1).strip().upper(), float(match.group(2))
            if favourite == home.abbreviation:
                expected = number
            elif favourite == away.abbreviation:
                expected = -number
            else:
                expected = None
            if expected is not None and abs(expected - spread) > 1e-6:
                log.info("spread disagreement: spread=%s details=%r", spread, details)
                return None
    return spread


def parse_event(raw: dict[str, Any], league: League, fetched_at: datetime) -> GameState | None:
    """One ESPN scoreboard event to a :class:`GameState`. None if it has no id."""
    feed_id = raw.get("id")
    if feed_id is None:
        return None
    feed_id = str(feed_id)
    competitions = raw.get("competitions")
    competition = competitions[0] if isinstance(competitions, list) and competitions else None
    if not isinstance(competition, dict):
        return _unknown(league, feed_id, None, None, None, fetched_at, "no competition block")
    kickoff = _parse_time(competition.get("date") or raw.get("date"))

    competitors = competition.get("competitors")
    home_raw = away_raw = None
    if isinstance(competitors, list):
        for competitor in competitors:
            if not isinstance(competitor, dict):
                continue
            if competitor.get("homeAway") == "home":
                home_raw = competitor
            elif competitor.get("homeAway") == "away":
                away_raw = competitor
    if home_raw is None or away_raw is None:
        return _unknown(league, feed_id, None, None, kickoff, fetched_at, "home/away missing")
    home = _team(home_raw)
    away = _team(away_raw)
    if home is None or away is None:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "team fields missing")

    status_raw = raw.get("status") if isinstance(raw.get("status"), dict) else None
    if status_raw is None and isinstance(competition.get("status"), dict):
        status_raw = competition["status"]
    if status_raw is None:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "status missing")
    status_type = status_raw.get("type") if isinstance(status_raw.get("type"), dict) else {}
    type_name = status_type.get("name")
    status = STATUS_BY_NAME.get(type_name) if isinstance(type_name, str) else None
    detail = status_type.get("shortDetail") or status_type.get("detail") or ""
    if status is None:
        return _unknown(
            league, feed_id, home, away, kickoff, fetched_at, f"status {type_name!r} unknown"
        )

    home_score = _int(home_raw.get("score"), 0, 200)
    away_score = _int(away_raw.get("score"), 0, 200)
    if home_score is None or away_score is None:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "score malformed")

    period = _int(status_raw.get("period"), 0, 20)
    clock = _float(status_raw.get("clock"), 0, PERIOD_SECONDS)
    if period is None:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "period malformed")
    if status in (GameStatus.LIVE, GameStatus.HALFTIME, GameStatus.DELAYED) and clock is None:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "clock malformed")
    if status in (GameStatus.LIVE, GameStatus.HALFTIME) and period < 1:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "live with period 0")
    if status is GameStatus.HALFTIME and period != 2:
        return _unknown(league, feed_id, home, away, kickoff, fetched_at, "halftime not in Q2")

    seconds_left: float | None
    if status is GameStatus.PRE:
        seconds_left = float(REGULATION_PERIODS * PERIOD_SECONDS)
    elif status is GameStatus.FINAL:
        seconds_left = 0.0
    elif period > REGULATION_PERIODS:
        seconds_left = 0.0
    elif clock is not None:
        seconds_left = (REGULATION_PERIODS - period) * PERIOD_SECONDS + clock
    else:
        seconds_left = None

    situation = competition.get("situation")
    possession = down = distance = yards = home_timeouts = away_timeouts = None
    espn_wp = None
    if isinstance(situation, dict):
        owner = situation.get("possession")
        if owner is not None:
            owner = str(owner)
            if owner == home.feed_id:
                possession = Side.HOME
            elif owner == away.feed_id:
                possession = Side.AWAY
        down = _int(situation.get("down"), 1, 4)
        distance = _int(situation.get("distance"), 0, 99)
        if possession is not None:
            yards = _yards_to_endzone(situation, possession, home, away)
        home_timeouts = _int(situation.get("homeTimeouts"), 0, 3)
        away_timeouts = _int(situation.get("awayTimeouts"), 0, 3)
        last_play = situation.get("lastPlay")
        if isinstance(last_play, dict) and isinstance(last_play.get("probability"), dict):
            espn_wp = _float(last_play["probability"].get("homeWinPercentage"), 0.0, 1.0)

    return GameState(
        league=league,
        feed_id=feed_id,
        home=home,
        away=away,
        kickoff=kickoff,
        status=status,
        home_score=home_score,
        away_score=away_score,
        period=period,
        clock_seconds=clock,
        seconds_left=seconds_left,
        possession=possession,
        down=down,
        distance=distance,
        yards_to_endzone=yards,
        home_timeouts=home_timeouts,
        away_timeouts=away_timeouts,
        espn_home_win_probability=espn_wp,
        home_spread=_spread(competition, home, away),
        fetched_at=fetched_at,
        unknown_reason=None,
        status_detail=str(detail),
    )


def parse_scoreboard(raw: Any, league: League, fetched_at: datetime) -> list[GameState]:
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        raise ScoreFeedError("scoreboard has no events list")
    states: list[GameState] = []
    for event in raw["events"]:
        if not isinstance(event, dict):
            continue
        state = parse_event(event, league, fetched_at)
        if state is not None:
            states.append(state)
    return states


class ScoreFeed:
    """Fetches and parses one league's scoreboard. One request per call."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: float = 8.0,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": "polymarket-scanner/0.1 (read-only)"}
        )
        self._now = now
        self.request_count = 0

    def fetch_raw(self, league: League) -> Any:
        self.request_count += 1
        try:
            response = self._client.get(SCOREBOARD_URLS[league])
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ScoreFeedError(f"{league.value}: {type(exc).__name__}: {exc}") from exc

    def fetch(self, league: League) -> list[GameState]:
        raw = self.fetch_raw(league)
        return parse_scoreboard(raw, league, self._now())
