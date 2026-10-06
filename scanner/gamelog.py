"""The football game log and the matchup sheet built from it.

ESPN's free endpoints carry everything a matchup sheet needs for the NFL and
college football: a weekly scoreboard listing every game with scores, quarter
scores, records and the book's line, and a summary per game with the box score
(yards, downs, turnovers, penalties, possession), the scoring plays, and for a
game still to come the venue, weather and ESPN's own projection. This module
fetches each finished game once and keeps it in the diary, then builds each
team's season, home-or-away, first-half and last-three figures with league
ranks, and the sheet the owner reads before deciding anything.

Read only. It never sends anything and knows nothing about alerts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from scanner.books import SCOREBOARD_BASE, SPORTS, BookLine, parse_book_line
from scanner.config import (
    GAMELOG_BACKFILL_PER_PASS,
    GAMELOG_REQUEST_GAP_SECONDS,
    GAMELOG_UPCOMING_HOURS,
    GAMELOG_UPCOMING_REFRESH_MINUTES,
    Settings,
)
from scanner.diary import FOOTBALL_GAME_VERSION, Diary

log = logging.getLogger(__name__)

FOOTBALL = ("nfl", "cfb")
STAT_KEYS = (
    "points",
    "first_half_points",
    "first_downs",
    "third_conv",
    "third_att",
    "fourth_conv",
    "fourth_att",
    "total_yards",
    "pass_yards",
    "pass_comp",
    "pass_att",
    "rush_yards",
    "rush_att",
    "penalties",
    "penalty_yards",
    "turnovers",
    "fumbles_lost",
    "interceptions",
    "possession_seconds",
    "plays",
    "sacks_taken",
    "pass_td",
    "rush_td",
    "first_half_pass_td",
    "first_half_rush_td",
    "first_half_pass_yards",
    "first_half_rush_yards",
    "first_half_total_yards",
)


@dataclass(frozen=True, slots=True)
class TeamRef:
    team_id: str
    abbreviation: str
    name: str
    location: str = ""
    logo: str | None = None  # ESPN's logo image address
    color: str | None = None  # the team's colour, as "RRGGBB"


@dataclass(frozen=True, slots=True)
class SlateGame:
    """One game as the weekly scoreboard lists it, finished or not."""

    sport: str
    game_id: str
    season: int | None
    week: int | None
    date: datetime | None
    completed: bool
    status: str
    neutral: bool
    home: TeamRef
    away: TeamRef
    home_score: int | None
    away_score: int | None
    home_lines: tuple[int, ...]
    away_lines: tuple[int, ...]
    records: dict  # {"home": {"total": "3-1", "home": "2-0", "road": "1-1"}, "away": {...}}
    ranks: dict  # {"home": 12, "away": None} from ESPN's poll rank (college)
    venue: dict  # {"name", "city", "state", "indoor"}
    book: BookLine | None

    def as_dict(self) -> dict:
        return {
            "sport": self.sport,
            "game_id": self.game_id,
            "season": self.season,
            "week": self.week,
            "date": self.date.isoformat() if self.date else None,
            "completed": self.completed,
            "status": self.status,
            "neutral": self.neutral,
            "home": vars_of(self.home),
            "away": vars_of(self.away),
            "home_score": self.home_score,
            "away_score": self.away_score,
            "home_lines": list(self.home_lines),
            "away_lines": list(self.away_lines),
            "records": self.records,
            "ranks": self.ranks,
            "venue": self.venue,
            "book": self.book.as_dict() if self.book else None,
        }


def vars_of(team: TeamRef) -> dict:
    return {
        "team_id": team.team_id,
        "abbreviation": team.abbreviation,
        "name": team.name,
        "location": team.location,
        "logo": team.logo,
        "color": team.color,
    }


@dataclass(frozen=True, slots=True)
class GameRecord:
    """A finished game with both teams' box scores, as kept in the diary."""

    sport: str
    game_id: str
    season: int | None
    week: int | None
    date: datetime
    neutral: bool
    home: TeamRef
    away: TeamRef
    home_score: int
    away_score: int
    home_lines: tuple[int, ...]
    away_lines: tuple[int, ...]
    home_stats: dict
    away_stats: dict
    book: BookLine | None = None  # the book's closing line, from the summary's pickcenter


@dataclass(frozen=True, slots=True)
class TeamGame:
    """One team's side of one finished game."""

    date: datetime
    week: int | None
    game_id: str
    home: bool
    neutral: bool
    opponent: TeamRef
    points: int
    allowed: int
    own: dict
    opp: dict

    @property
    def won(self) -> bool:
        return self.points > self.allowed


# -- parsing ---------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _pair(value: Any, sep: str) -> tuple[int | None, int | None]:
    if not isinstance(value, str) or sep not in value:
        return None, None
    left, _, right = value.partition(sep)
    return _int(left), _int(right)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _team_ref(team: Any, fallback_id: Any = None) -> TeamRef | None:
    if not isinstance(team, dict):
        return None
    team_id = team.get("id", fallback_id)
    abbreviation = team.get("abbreviation")
    name = team.get("displayName") or team.get("name")
    if team_id is None or not isinstance(abbreviation, str) or not isinstance(name, str):
        return None
    logo = team.get("logo")
    if not isinstance(logo, str):
        logos = team.get("logos")
        first = logos[0] if isinstance(logos, list) and logos else None
        logo = first.get("href") if isinstance(first, dict) else None
    color = team.get("color")
    return TeamRef(
        str(team_id),
        abbreviation.strip().upper(),
        name.strip(),
        str(team.get("location") or ""),
        logo if isinstance(logo, str) and logo.startswith("https://") else None,
        color if isinstance(color, str) and len(color) == 6 else None,
    )


def _lines(competitor: dict) -> tuple[int, ...]:
    out = []
    for entry in competitor.get("linescores") or []:
        value = (
            _int(entry.get("value", entry.get("displayValue"))) if isinstance(entry, dict) else None
        )
        if value is None:
            return ()
        out.append(value)
    return tuple(out)


def _records(competitor: dict) -> dict:
    out = {}
    for record in competitor.get("records") or competitor.get("record") or []:
        if isinstance(record, dict) and record.get("type") and record.get("summary"):
            out[str(record["type"])] = str(record["summary"])
    return out


def parse_week_scoreboard(raw: Any, sport: str) -> tuple[int | None, int | None, list[SlateGame]]:
    """(season, week, games) from one weekly scoreboard."""
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        return None, None, []
    season = (
        _int((raw.get("season") or {}).get("year")) if isinstance(raw.get("season"), dict) else None
    )
    week = (
        _int((raw.get("week") or {}).get("number")) if isinstance(raw.get("week"), dict) else None
    )
    games = []
    for event in raw["events"]:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions")
        competition = competitions[0] if isinstance(competitions, list) and competitions else None
        if event.get("id") is None or not isinstance(competition, dict):
            continue
        home_raw = away_raw = None
        for competitor in competition.get("competitors") or []:
            if not isinstance(competitor, dict):
                continue
            if competitor.get("homeAway") == "home":
                home_raw = competitor
            elif competitor.get("homeAway") == "away":
                away_raw = competitor
        if home_raw is None or away_raw is None:
            continue
        home = _team_ref(home_raw.get("team"), home_raw.get("id"))
        away = _team_ref(away_raw.get("team"), away_raw.get("id"))
        if home is None or away is None:
            continue
        status = event.get("status") if isinstance(event.get("status"), dict) else {}
        kind = status.get("type") if isinstance(status.get("type"), dict) else {}
        venue = competition.get("venue") if isinstance(competition.get("venue"), dict) else {}
        address = venue.get("address") if isinstance(venue.get("address"), dict) else {}
        odds = competition.get("odds")
        book = None
        if isinstance(odds, list) and odds and isinstance(odds[0], dict):
            book = parse_book_line(odds[0])
        event_week = event.get("week") if isinstance(event.get("week"), dict) else {}
        event_season = event.get("season") if isinstance(event.get("season"), dict) else {}

        def rank(competitor: dict) -> int | None:
            curated = competitor.get("curatedRank")
            value = _int(curated.get("current")) if isinstance(curated, dict) else None
            return value if value is not None and value < 99 else None

        games.append(
            SlateGame(
                sport=sport,
                game_id=str(event["id"]),
                season=_int(event_season.get("year")) or season,
                week=_int(event_week.get("number")) or week,
                date=_time(competition.get("date") or event.get("date")),
                completed=bool(kind.get("completed")),
                status=str(kind.get("name") or ""),
                neutral=bool(competition.get("neutralSite")),
                home=home,
                away=away,
                home_score=_int(home_raw.get("score")),
                away_score=_int(away_raw.get("score")),
                home_lines=_lines(home_raw),
                away_lines=_lines(away_raw),
                records={"home": _records(home_raw), "away": _records(away_raw)},
                ranks={"home": rank(home_raw), "away": rank(away_raw)},
                venue={
                    "name": venue.get("fullName"),
                    "city": address.get("city"),
                    "state": address.get("state"),
                    "indoor": venue.get("indoor"),
                },
                book=book,
            )
        )
    return season, week, games


def _box_stats(team_block: dict) -> dict:
    """One team's box score, as per-game counting numbers."""
    raw = {}
    for stat in team_block.get("statistics") or []:
        if isinstance(stat, dict) and stat.get("name"):
            raw[str(stat["name"])] = stat.get("displayValue")
    third_conv, third_att = _pair(raw.get("thirdDownEff"), "-")
    fourth_conv, fourth_att = _pair(raw.get("fourthDownEff"), "-")
    pass_comp, pass_att = _pair(raw.get("completionAttempts"), "/")
    penalties, penalty_yards = _pair(raw.get("totalPenaltiesYards"), "-")
    sacks_taken, _ = _pair(raw.get("sacksYardsLost"), "-")
    minutes, seconds = _pair(raw.get("possessionTime"), ":")
    possession = None if minutes is None or seconds is None else minutes * 60 + seconds
    rush_att = _int(raw.get("rushingAttempts"))
    plays = _int(raw.get("totalOffensivePlays"))
    if plays is None and pass_att is not None and rush_att is not None:
        plays = pass_att + rush_att + (sacks_taken or 0)
    return {
        "first_downs": _int(raw.get("firstDowns")),
        "third_conv": third_conv,
        "third_att": third_att,
        "fourth_conv": fourth_conv,
        "fourth_att": fourth_att,
        "total_yards": _int(raw.get("totalYards")),
        "pass_yards": _int(raw.get("netPassingYards")),
        "pass_comp": pass_comp,
        "pass_att": pass_att,
        "rush_yards": _int(raw.get("rushingYards")),
        "rush_att": rush_att,
        "penalties": penalties,
        "penalty_yards": penalty_yards,
        "turnovers": _int(raw.get("turnovers")),
        "fumbles_lost": _int(raw.get("fumblesLost")),
        "interceptions": _int(raw.get("interceptions")),
        "possession_seconds": possession,
        "plays": plays,
        "sacks_taken": sacks_taken,
    }


def parse_game_summary(raw: Any, sport: str) -> GameRecord | None:
    """A finished game's record from its summary, or None if anything core is missing."""
    if not isinstance(raw, dict):
        return None
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    competitions = header.get("competitions")
    competition = competitions[0] if isinstance(competitions, list) and competitions else None
    if not isinstance(competition, dict) or competition.get("id") is None:
        return None
    sides: dict[str, dict] = {}
    for competitor in competition.get("competitors") or []:
        if isinstance(competitor, dict) and competitor.get("homeAway") in ("home", "away"):
            sides[competitor["homeAway"]] = competitor
    if set(sides) != {"home", "away"}:
        return None
    home = _team_ref(sides["home"].get("team"), sides["home"].get("id"))
    away = _team_ref(sides["away"].get("team"), sides["away"].get("id"))
    home_score = _int(sides["home"].get("score"))
    away_score = _int(sides["away"].get("score"))
    date = _time(competition.get("date"))
    if home is None or away is None or home_score is None or away_score is None or date is None:
        return None
    status = competition.get("status") if isinstance(competition.get("status"), dict) else {}
    kind = status.get("type") if isinstance(status.get("type"), dict) else {}
    if kind and not kind.get("completed", True):
        return None

    boxscore = raw.get("boxscore") if isinstance(raw.get("boxscore"), dict) else {}
    stats: dict[str, dict] = {}
    for block in boxscore.get("teams") or []:
        if not isinstance(block, dict):
            continue
        team = block.get("team") if isinstance(block.get("team"), dict) else {}
        side = block.get("homeAway")
        if side not in ("home", "away"):
            side = "home" if str(team.get("id")) == home.team_id else "away"
        stats[side] = _box_stats(block)
    if set(stats) != {"home", "away"}:
        return None

    # [pass TD, rush TD, first-half pass TD, first-half rush TD]
    touchdowns = {"home": [0, 0, 0, 0], "away": [0, 0, 0, 0]}
    for play in raw.get("scoringPlays") or []:
        if not isinstance(play, dict):
            continue
        team = play.get("team") if isinstance(play.get("team"), dict) else {}
        side = "home" if str(team.get("id")) == home.team_id else "away"
        text = str((play.get("type") or {}).get("text") or "").lower()
        if "touchdown" not in text:
            continue
        period = _int((play.get("period") or {}).get("number"))
        early = period is not None and period <= 2
        if text.startswith("passing"):
            touchdowns[side][0] += 1
            touchdowns[side][2] += int(early)
        elif text.startswith("rushing"):
            touchdowns[side][1] += 1
            touchdowns[side][3] += int(early)
    half_yards = _first_half_yards(raw, home)
    book = None
    pickcenter = raw.get("pickcenter")
    if isinstance(pickcenter, list) and pickcenter and isinstance(pickcenter[0], dict):
        book = parse_book_line(pickcenter[0])  # the line as it closed
    home_lines, away_lines = _lines(sides["home"]), _lines(sides["away"])
    for side, score, lines in (("home", home_score, home_lines), ("away", away_score, away_lines)):
        stats[side]["points"] = score
        stats[side]["first_half_points"] = sum(lines[:2]) if len(lines) >= 2 else None
        stats[side]["pass_td"] = touchdowns[side][0]
        stats[side]["rush_td"] = touchdowns[side][1]
        stats[side]["first_half_pass_td"] = touchdowns[side][2]
        stats[side]["first_half_rush_td"] = touchdowns[side][3]
        yards = half_yards.get(side) if half_yards else None
        stats[side]["first_half_pass_yards"] = yards["pass"] if yards else None
        stats[side]["first_half_rush_yards"] = yards["rush"] if yards else None
        stats[side]["first_half_total_yards"] = yards["total"] if yards else None
    season = header.get("season") if isinstance(header.get("season"), dict) else {}
    return GameRecord(
        sport=sport,
        game_id=str(competition["id"]),
        season=_int(season.get("year")),
        week=_int(header.get("week")),
        date=date,
        neutral=bool(competition.get("neutralSite")),
        home=home,
        away=away,
        home_score=home_score,
        away_score=away_score,
        home_lines=home_lines,
        away_lines=away_lines,
        home_stats=stats["home"],
        away_stats=stats["away"],
        book=book,
    )


PASS_PLAY_WORDS = ("reception", "incompletion", "passing touchdown", "sack")
RUSH_PLAY_WORDS = ("rush",)


def _first_half_yards(raw: dict, home: TeamRef) -> dict[str, dict[str, int]] | None:
    """First-half passing and rushing yards for each side, from the play-by-play.

    ESPN's box score has no first-half split, so the drives are added up: every
    play in the first two quarters that was a pass, a sack or a rush, by the
    yardage ESPN credits it. Penalty plays are left out. None without drives.
    """
    drives = raw.get("drives") if isinstance(raw.get("drives"), dict) else {}
    previous = drives.get("previous")
    if not isinstance(previous, list):
        return None
    out = {side: {"pass": 0, "rush": 0, "total": 0} for side in ("home", "away")}
    seen = False
    for drive in previous:
        if not isinstance(drive, dict):
            continue
        team = drive.get("team") if isinstance(drive.get("team"), dict) else {}
        if str(team.get("id")) == home.team_id or team.get("abbreviation") == home.abbreviation:
            side = "home"
        else:
            side = "away"
        for play in drive.get("plays") or []:
            if not isinstance(play, dict) or play.get("isPenalty"):
                continue
            period = _int((play.get("period") or {}).get("number"))
            if period is None or period > 2:
                continue
            kind = str((play.get("type") or {}).get("text") or "").lower()
            if "interception" in kind or "fumble" in kind:
                continue
            yards = _int(play.get("statYardage")) or 0
            if any(word in kind for word in PASS_PLAY_WORDS):
                out[side]["pass"] += yards
            elif any(word in kind for word in RUSH_PLAY_WORDS):
                out[side]["rush"] += yards
            else:
                continue
            out[side]["total"] += yards
            seen = True
    return out if seen else None


def parse_upcoming_summary(raw: Any) -> dict:
    """Venue, weather and ESPN's projection for a game still to come."""
    if not isinstance(raw, dict):
        return {}
    info = raw.get("gameInfo") if isinstance(raw.get("gameInfo"), dict) else {}
    venue = info.get("venue") if isinstance(info.get("venue"), dict) else {}
    weather = info.get("weather") if isinstance(info.get("weather"), dict) else {}
    predictor = raw.get("predictor") if isinstance(raw.get("predictor"), dict) else {}
    out: dict = {}
    if venue:
        out["venue"] = {"name": venue.get("fullName"), "grass": venue.get("grass")}
    if weather:
        out["weather"] = {
            "temperature": weather.get("temperature"),
            "precipitation": weather.get("precipitation"),
            "gust": weather.get("gust"),
        }
    home = predictor.get("homeTeam") if isinstance(predictor.get("homeTeam"), dict) else {}
    away = predictor.get("awayTeam") if isinstance(predictor.get("awayTeam"), dict) else {}
    try:
        if home.get("gameProjection") is not None and away.get("gameProjection") is not None:
            out["predictor"] = {
                "home": float(home["gameProjection"]),
                "away": float(away["gameProjection"]),
            }
    except (TypeError, ValueError):
        pass
    return out


# -- figures ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    label: str
    section: str
    higher_is_better: bool | None  # None: neither (pace)
    decimals: int = 1
    percent: bool = False


METRICS: tuple[Metric, ...] = (
    Metric("ppg", "Points", "Offense", True),
    Metric("pass_ypg", "Pass yards", "Offense", True),
    Metric("pass_td", "Pass TD", "Offense", True, 2),
    Metric("rush_ypg", "Rush yards", "Offense", True),
    Metric("rush_td", "Rush TD", "Offense", True, 2),
    Metric("ypp", "Yards per play", "Offense", True),
    Metric("third_pct", "3rd down %", "Offense", True, 1, True),
    Metric("sacks_allowed", "Sacks allowed", "Offense", False, 2),
    Metric("turnovers", "Turnovers", "Offense", False, 2),
    Metric("ppg_allowed", "Points allowed", "Defense", False),
    Metric("pass_ypg_allowed", "Pass yards allowed", "Defense", False),
    Metric("pass_td_allowed", "Pass TD allowed", "Defense", False, 2),
    Metric("rush_ypg_allowed", "Rush yards allowed", "Defense", False),
    Metric("rush_td_allowed", "Rush TD allowed", "Defense", False, 2),
    Metric("ypp_allowed", "Yards per play allowed", "Defense", False),
    Metric("opp_third_pct", "Opp 3rd down %", "Defense", False, 1, True),
    Metric("sacks", "Sacks", "Defense", True, 2),
    Metric("takeaways", "Takeaways", "Defense", True, 2),
    Metric("to_margin", "Turnover margin", "Situational", True, 2),
    Metric("penalty_yards", "Penalty yards", "Situational", False),
    Metric("pace", "Plays per game", "Situational", None),
    Metric("possession", "Possession (min)", "Situational", True),
)
METRIC_BY_KEY = {m.key: m for m in METRICS}
SECTIONS = ("Offense", "Defense", "Situational")
# Metrics with a first-half figure: (own or opponent's stats, the per-game stat key).
FIRST_HALF_SOURCES = {
    "ppg": ("own", "first_half_points"),
    "pass_ypg": ("own", "first_half_pass_yards"),
    "pass_td": ("own", "first_half_pass_td"),
    "rush_ypg": ("own", "first_half_rush_yards"),
    "rush_td": ("own", "first_half_rush_td"),
    "ppg_allowed": ("opp", "first_half_points"),
    "pass_ypg_allowed": ("opp", "first_half_pass_yards"),
    "pass_td_allowed": ("opp", "first_half_pass_td"),
    "rush_ypg_allowed": ("opp", "first_half_rush_yards"),
    "rush_td_allowed": ("opp", "first_half_rush_td"),
}
FIRST_HALF_KEYS = set(FIRST_HALF_SOURCES)


def _mean(values: list) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _rate(numerators: list, denominators: list) -> float | None:
    pairs = [(n, d) for n, d in zip(numerators, denominators, strict=True) if n is not None and d]
    if not pairs:
        return None
    return 100.0 * sum(n for n, _ in pairs) / sum(d for _, d in pairs)


def _per(numerators: list, denominators: list) -> float | None:
    pairs = [(n, d) for n, d in zip(numerators, denominators, strict=True) if n is not None and d]
    if not pairs:
        return None
    return sum(n for n, _ in pairs) / sum(d for _, d in pairs)


def aggregate(games: list[TeamGame]) -> dict[str, float | None]:
    """Per-game figures over a set of one team's games (empty set: every value None)."""
    own = [g.own for g in games]
    opp = [g.opp for g in games]

    def col(rows: list[dict], key: str) -> list:
        return [r.get(key) for r in rows]

    turnovers = _mean(col(own, "turnovers"))
    takeaways = _mean(col(opp, "turnovers"))
    possession = _mean(col(own, "possession_seconds"))
    return {
        "ppg": _mean([g.points for g in games]),
        "pass_ypg": _mean(col(own, "pass_yards")),
        "pass_td": _mean(col(own, "pass_td")),
        "rush_ypg": _mean(col(own, "rush_yards")),
        "rush_td": _mean(col(own, "rush_td")),
        "ypp": _per(col(own, "total_yards"), col(own, "plays")),
        "third_pct": _rate(col(own, "third_conv"), col(own, "third_att")),
        "sacks_allowed": _mean(col(own, "sacks_taken")),
        "turnovers": turnovers,
        "ppg_allowed": _mean([g.allowed for g in games]),
        "pass_ypg_allowed": _mean(col(opp, "pass_yards")),
        "pass_td_allowed": _mean(col(opp, "pass_td")),
        "rush_ypg_allowed": _mean(col(opp, "rush_yards")),
        "rush_td_allowed": _mean(col(opp, "rush_td")),
        "ypp_allowed": _per(col(opp, "total_yards"), col(opp, "plays")),
        "opp_third_pct": _rate(col(opp, "third_conv"), col(opp, "third_att")),
        "sacks": _mean(col(opp, "sacks_taken")),
        "takeaways": takeaways,
        "to_margin": None if turnovers is None or takeaways is None else takeaways - turnovers,
        "penalty_yards": _mean(col(own, "penalty_yards")),
        "pace": _mean(col(own, "plays")),
        "possession": None if possession is None else possession / 60.0,
    }


def first_half(games: list[TeamGame]) -> dict[str, float | None]:
    """First-half per-game figures for the metrics that have one."""
    out = {}
    for key, (side, stat) in FIRST_HALF_SOURCES.items():
        rows = [g.own if side == "own" else g.opp for g in games]
        out[key] = _mean([r.get(stat) for r in rows])
    return out


def team_games(records: list[GameRecord], team_id: str) -> list[TeamGame]:
    """One team's side of every game it played, oldest first."""
    out = []
    for record in records:
        if record.home.team_id == team_id:
            out.append(
                TeamGame(
                    record.date,
                    record.week,
                    record.game_id,
                    True,
                    record.neutral,
                    record.away,
                    record.home_score,
                    record.away_score,
                    record.home_stats,
                    record.away_stats,
                )
            )
        elif record.away.team_id == team_id:
            out.append(
                TeamGame(
                    record.date,
                    record.week,
                    record.game_id,
                    False,
                    record.neutral,
                    record.home,
                    record.away_score,
                    record.home_score,
                    record.away_stats,
                    record.home_stats,
                )
            )
    out.sort(key=lambda g: g.date)
    return out


def last_n(games: list[TeamGame], n: int = 3) -> list[TeamGame]:
    return games[-n:] if n > 0 else []


def rank_values(values: dict[str, float | None], higher_is_better: bool) -> dict[str, int]:
    """Competition ranking, 1 = best. Teams without a value are left out."""
    present = {team: v for team, v in values.items() if v is not None}
    ranks = {}
    for team, value in present.items():
        better = sum(
            1
            for other in present.values()
            if (other > value if higher_is_better else other < value)
        )
        ranks[team] = better + 1
    return ranks


def league_ranks(
    records: list[GameRecord], window: str = "last3"
) -> tuple[dict[str, dict[str, int]], int]:
    """{team_id: {metric: rank}} over every team in the log, and the number of teams."""
    teams: dict[str, TeamRef] = {}
    for record in records:
        teams[record.home.team_id] = record.home
        teams[record.away.team_id] = record.away
    figures = {}
    for team_id in teams:
        games = team_games(records, team_id)
        figures[team_id] = aggregate(last_n(games) if window == "last3" else games)
    ranks: dict[str, dict[str, int]] = {team_id: {} for team_id in teams}
    for metric in METRICS:
        if metric.higher_is_better is None:
            continue
        per_team = {team_id: fig.get(metric.key) for team_id, fig in figures.items()}
        for team_id, rank in rank_values(per_team, metric.higher_is_better).items():
            ranks[team_id][metric.key] = rank
    return ranks, len(teams)


def margin_ranks(records: list[GameRecord]) -> dict[str, int]:
    """Season point margin per game, ranked: the sheet's "overall" rank."""
    teams: dict[str, TeamRef] = {}
    for record in records:
        teams[record.home.team_id] = record.home
        teams[record.away.team_id] = record.away
    margins: dict[str, float | None] = {}
    for team_id in teams:
        figures = aggregate(team_games(records, team_id))
        ppg, allowed = figures.get("ppg"), figures.get("ppg_allowed")
        margins[team_id] = None if ppg is None or allowed is None else ppg - allowed
    return rank_values(margins, True)


def streak(games: list[TeamGame]) -> str:
    if not games:
        return ""
    last = games[-1].won
    count = 0
    for game in reversed(games):
        if game.won is last:
            count += 1
        else:
            break
    return f"{'W' if last else 'L'}{count}"


# -- the sheet ---------------------------------------------------------------------


def team_block(
    side: str,
    slate: dict,
    records: list[GameRecord],
    ranks: dict,
    season_ranks: dict,
    kickoff,
    overall: dict | None = None,
) -> dict:
    team = slate[side]
    games = team_games(records, team["team_id"])
    split_games = [g for g in games if (g.home if side == "home" else not g.home) and not g.neutral]
    last3 = last_n(games)
    rest_days = None
    if games and kickoff is not None:
        rest_days = (kickoff - games[-1].date).days
    last5 = [
        {
            "opponent": g.opponent.abbreviation,
            "at": "" if g.neutral else ("vs" if g.home else "@"),
            "score": f"{g.points}-{g.allowed}",
            "won": g.won,
        }
        for g in games[-5:]
    ]
    team_season_rank = season_ranks.get(team["team_id"], {})
    return {
        "team_id": team["team_id"],
        "abbreviation": team["abbreviation"],
        "name": team["name"],
        "logo": team.get("logo"),
        "color": team.get("color"),
        "summary_ranks": {
            "offense": team_season_rank.get("ppg"),
            "defense": team_season_rank.get("ppg_allowed"),
            "overall": (overall or {}).get(team["team_id"]),
        },
        "record": (slate.get("records") or {}).get(side) or {},
        "poll_rank": (slate.get("ranks") or {}).get(side),
        "games": len(games),
        "streak": streak(games),
        "rest_days": rest_days,
        "last5": last5,
        "season": aggregate(games),
        "split": aggregate(split_games),
        "split_games": len(split_games),
        "split_label": "Home" if side == "home" else "Away",
        "first_half": first_half(games),
        "last3": aggregate(last3),
        "last3_rank": ranks.get(team["team_id"], {}),
        "season_rank": season_ranks.get(team["team_id"], {}),
    }


def build_sheet(
    upcoming: dict, records: list[GameRecord], extra: dict, polymarket: dict | None
) -> dict:
    """Everything the matchup page shows, from the diary's rows."""
    slate = upcoming["slate"]
    kickoff = _time(slate.get("date"))
    ranks, league_size = league_ranks(records, "last3")
    season_ranks, _ = league_ranks(records, "season")
    overall = margin_ranks(records)
    home = team_block("home", slate, records, ranks, season_ranks, kickoff, overall)
    away = team_block("away", slate, records, ranks, season_ranks, kickoff, overall)
    book = BookLine.from_dict(slate.get("book")) if slate.get("book") else None
    implied = None
    if book is not None and book.total is not None and book.home_spread is not None:
        # The spread is the home team's handicap: TROY -10.5 with a total of 50.5
        # means the market expects TROY 30.5, the visitor 20.
        implied = {
            "home": (book.total - book.home_spread) / 2.0,
            "away": (book.total + book.home_spread) / 2.0,
        }
    fair = book.fair_moneyline() if book is not None else None
    advantages = {}
    for metric in METRICS:
        if metric.higher_is_better is None:
            continue
        h, a = home["last3_rank"].get(metric.key), away["last3_rank"].get(metric.key)
        if h is None or a is None or h == a:
            advantages[metric.key] = None
        else:
            advantages[metric.key] = "home" if h < a else "away"
    return {
        "sport": upcoming["sport"],
        "game_id": upcoming["game_id"],
        "kickoff": kickoff,
        "season": slate.get("season"),
        "week": slate.get("week"),
        "neutral": slate.get("neutral"),
        "venue": {**(slate.get("venue") or {}), **(extra.get("venue") or {})},
        "weather": extra.get("weather"),
        "predictor": extra.get("predictor"),
        "book": book,
        "implied": implied,
        "win_probability": None if fair is None else {"home": fair[0], "away": fair[1]},
        "polymarket": polymarket,
        "home": home,
        "away": away,
        "advantages": advantages,
        "league_size": league_size,
        "games_in_log": len(records),
    }


# -- fetching ----------------------------------------------------------------------


class ScoreboardFeed:
    """Weekly scoreboards and game summaries from ESPN. One request per call."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
        gap_seconds: float = GAMELOG_REQUEST_GAP_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": "polymarket-scanner/0.1 (read-only)"}
        )
        self._gap = gap_seconds
        self._sleep = sleep
        self.request_count = 0

    def _get(self, url: str) -> Any:
        if self.request_count and self._gap > 0:
            self._sleep(self._gap)
        self.request_count += 1
        try:
            response = self._client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GameLogError(f"{type(exc).__name__}: {exc}") from exc

    def week(self, sport: str, week: int | None = None) -> Any:
        sport_info = SPORTS[sport]
        query = "seasontype=2&limit=300"
        if week is not None:
            query = f"week={week}&{query}"
        if sport_info.query:
            query = f"{sport_info.query}&{query}"
        return self._get(f"{SCOREBOARD_BASE}/{sport_info.path}/scoreboard?{query}")

    def summary(self, sport: str, game_id: str) -> Any:
        return self._get(f"{SCOREBOARD_BASE}/{SPORTS[sport].path}/summary?event={game_id}")


class GameLogError(Exception):
    """A scoreboard or summary could not be read."""


@dataclass
class RefreshSummary:
    sport: str
    season: int | None = None
    week: int | None = None
    games_stored: int = 0
    upcoming: int = 0
    summaries_fetched: int = 0
    backlog: int = 0  # finished games still to fetch when the budget ran out
    reread: int = 0  # stored games read again because the parser has learned more
    stale: int = 0  # stored games still waiting for that re-read
    projected: int = 0  # upcoming games given a projection this pass
    graded: int = 0  # projections graded this pass
    staked: int = 0  # stake suggestions open after this pass
    errors: list[str] = field(default_factory=list)


class FootballLog:
    """Keeps the diary's football tables current and builds sheets from them."""

    def __init__(
        self,
        settings: Settings,
        diary: Diary,
        feed: ScoreboardFeed | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.diary = diary
        self.feed = feed or ScoreboardFeed()
        self._now = now

    # -- refresh --------------------------------------------------------------------

    def refresh(
        self, sport: str, now: datetime | None = None, budget: int = GAMELOG_BACKFILL_PER_PASS
    ) -> RefreshSummary:
        now = now or self._now()
        summary = RefreshSummary(sport=sport)
        try:
            season, week, games = parse_week_scoreboard(self.feed.week(sport), sport)
        except GameLogError as exc:
            summary.errors.append(f"{sport} scoreboard: {exc}")
            return summary
        summary.season, summary.week = season, week
        stored = self.diary.football_game_ids(sport)
        budget = self._store_week(sport, season, week, games, stored, now, budget, summary)
        upcoming = list(games)
        if week is not None:
            # ESPN's "current" week rolls over midweek, so the next week's games are
            # read too: Thursday's game needs its sheet on Tuesday.
            try:
                _, _, next_games = parse_week_scoreboard(self.feed.week(sport, week + 1), sport)
                upcoming.extend(next_games)
            except GameLogError as exc:
                summary.errors.append(f"{sport} week {week + 1}: {exc}")
        self._store_upcoming(sport, upcoming, now, summary)
        if season is not None and week is not None:
            for past in range(1, week):
                if budget <= 0:
                    break
                if self.diary.football_week_complete(sport, season, past):
                    continue
                try:
                    _, _, past_games = parse_week_scoreboard(self.feed.week(sport, past), sport)
                except GameLogError as exc:
                    summary.errors.append(f"{sport} week {past}: {exc}")
                    continue
                budget = self._store_week(
                    sport, season, past, past_games, stored, now, budget, summary
                )
        self._reread_stale(sport, now, budget, summary)
        self._refresh_upcoming_summaries(sport, now, summary)
        if self.settings.PROJECTION_ENABLED:
            self._project(sport, season, now, summary)
        return summary

    def _project(self, sport, season, now, summary) -> None:
        """Fit the model to the log and write a projection for every upcoming game.

        A projection keeps updating as games come in until kickoff, then locks; it
        is graded once the game's own record lands in the log.
        """
        from scanner.projection import fit, project
        from scanner.stakes import gate, suggest

        ratings = fit(self.diary.football_games(sport, season), sport)
        stakes_on = self.settings.STAKE_ENABLED
        gate_open = (
            stakes_on and gate(self.diary.projection_summary(sport=sport), self.settings)["open"]
        )
        for row in self.diary.football_upcoming(sport):
            kickoff = datetime.fromisoformat(row["date"])
            if kickoff <= now:
                continue
            slate = row["slate"]
            book = BookLine.from_dict(slate.get("book")) if slate.get("book") else None
            projection = project(
                ratings,
                slate["home"]["team_id"],
                slate["away"]["team_id"],
                bool(slate.get("neutral")),
                book,
            )
            if projection is None:
                continue
            self.diary.upsert_projection(
                sport,
                row["game_id"],
                kickoff,
                slate["home"]["abbreviation"],
                slate["away"]["abbreviation"],
                projection,
                now,
            )
            summary.projected += 1
            if stakes_on:
                line = self.diary.last_pregame_line(sport, row["game_id"])
                suggestions = suggest(
                    projection,
                    line["polymarket"] if line else None,
                    sport,
                    slate["home"]["abbreviation"],
                    slate["away"]["abbreviation"],
                    self.settings,
                )
                summary.staked += self.diary.sync_stakes(
                    sport,
                    row["game_id"],
                    kickoff,
                    slate["home"]["abbreviation"],
                    slate["away"]["abbreviation"],
                    suggestions,
                    gate_open,
                    self.settings.BANKROLL,
                    line["scanned_at"] if line else None,
                    now,
                )
        summary.graded = self.diary.grade_projections(sport, now)
        if stakes_on:
            self.diary.grade_stakes(sport, now)

    def _reread_stale(self, sport, now, budget, summary) -> None:
        """Re-read games stored by an older parser, with whatever budget is left.

        New games always come first; the re-read only spends what they left. A game
        whose summary no longer parses is marked current anyway, so it is not read
        on every pass.
        """
        for game_id in self.diary.football_games_stale(sport, limit=max(budget, 0)):
            summary.summaries_fetched += 1
            try:
                record = parse_game_summary(self.feed.summary(sport, game_id), sport)
            except GameLogError as exc:
                summary.errors.append(f"{sport} re-read {game_id}: {exc}")
                continue
            if record is None:
                log.info("game %s %s: re-read gave no usable box score", sport, game_id)
                self.diary.mark_football_game_version(sport, game_id, FOOTBALL_GAME_VERSION)
            else:
                self.diary.store_football_game(record, now)
            summary.reread += 1
        summary.stale = len(self.diary.football_games_stale(sport))

    def _store_week(self, sport, season, week, games, stored, now, budget, summary) -> int:
        finished = [g for g in games if g.completed]
        missing = [g for g in finished if g.game_id not in stored]
        for game in missing:
            if budget <= 0:
                break
            budget -= 1
            summary.summaries_fetched += 1
            try:
                record = parse_game_summary(self.feed.summary(sport, game.game_id), sport)
            except GameLogError as exc:
                summary.errors.append(f"{sport} game {game.game_id}: {exc}")
                continue
            if record is None:
                log.info("game %s %s: summary has no usable box score", sport, game.game_id)
                continue
            self.diary.store_football_game(record, now)
            stored.add(game.game_id)
            summary.games_stored += 1
        remaining = sum(1 for g in finished if g.game_id not in stored)
        summary.backlog += remaining
        if season is not None and week is not None:
            # A week is done when every listed game is final and stored. An empty
            # week (none listed) is done too, or it would be re-read every pass.
            complete = all(g.completed for g in games) and remaining == 0
            self.diary.mark_football_week(sport, season, week, complete, now)
        return budget

    def _store_upcoming(self, sport, games, now, summary) -> None:
        horizon = now + timedelta(hours=GAMELOG_UPCOMING_HOURS)
        for game in games:
            if game.completed or game.date is None:
                continue
            if game.date < now - timedelta(hours=8) or game.date > horizon:
                continue
            self.diary.store_football_upcoming(game, now)
            summary.upcoming += 1
        self.diary.delete_football_upcoming_before(sport, now - timedelta(hours=8))

    def _refresh_upcoming_summaries(self, sport, now, summary) -> None:
        stale = now - timedelta(minutes=GAMELOG_UPCOMING_REFRESH_MINUTES)
        for row in self.diary.football_upcoming(sport):
            fetched = row.get("summary_fetched_at")
            if fetched and datetime.fromisoformat(fetched) > stale:
                continue
            try:
                extra = parse_upcoming_summary(self.feed.summary(sport, row["game_id"]))
            except GameLogError as exc:
                summary.errors.append(f"{sport} upcoming {row['game_id']}: {exc}")
                continue
            self.diary.update_football_upcoming_extra(sport, row["game_id"], extra, now)

    def refresh_all(self, now: datetime | None = None) -> list[RefreshSummary]:
        now = now or self._now()
        return [self.refresh(sport, now) for sport in FOOTBALL]

    # -- reading ----------------------------------------------------------------------

    def slate(self, sport: str) -> list[dict]:
        return self.diary.football_upcoming(sport)

    def sheet(self, sport: str, game_id: str) -> dict | None:
        return sheet_from_diary(self.diary, sport, game_id)


def sheet_from_diary(diary: Diary, sport: str, game_id: str) -> dict | None:
    """The matchup sheet for an upcoming game, from the diary alone (no network)."""
    upcoming = diary.football_upcoming_game(sport, game_id)
    if upcoming is None:
        return None
    season = upcoming["slate"].get("season")
    records = diary.football_games(sport, season)
    polymarket = None
    line = diary.last_pregame_line(sport, game_id)
    if line is not None:
        polymarket = {"scanned_at": line["scanned_at"], **(line.get("polymarket") or {})}
    sheet = build_sheet(upcoming, records, upcoming.get("extra") or {}, polymarket)
    stored = diary.projection(sport, game_id)
    sheet["projection"] = stored["projection"] if stored else None
    sheet["stakes"] = diary.stakes_for(sport, game_id)
    sheet["model_record"] = diary.projection_summary(sport=sport)
    return sheet
