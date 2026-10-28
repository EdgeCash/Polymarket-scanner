"""The game log and the matchup sheet built from it.

ESPN's free endpoints carry everything a matchup sheet needs: a scoreboard
listing every game with scores, period scores, records and the book's line (by
week for the NFL and college football, by day for the NBA and NHL), and a
summary per game with the box score (yards, downs, turnovers and possession in
football; shooting, rebounds and turnovers in basketball; shots, power plays and
faceoffs in hockey), the scoring plays, and for a game still to come the venue,
weather and ESPN's own projection. This module fetches each finished game once
and keeps it in the diary, then builds each team's season, home-or-away,
first-half and last-three figures with league ranks, and the sheet the owner
reads before deciding anything. LOGS says how each sport differs.

Read only. It never sends anything and knows nothing about alerts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from scanner.books import (
    ESPN_ZONE,
    SCOREBOARD_BASE,
    SPORTS,
    BookLine,
    parse_book_line,
    scoreboard_url,
)
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
    if "total" not in out and "ytd" in out:
        out["total"] = out["ytd"]  # the NHL scoreboard's name for the season record
    return out


def parse_week_scoreboard(raw: Any, sport: str) -> tuple[int | None, int | None, list[SlateGame]]:
    """(season, week, games) from one scoreboard: a week's, or a day's (week None).

    Preseason games are left out: they are not the model's business.
    """
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
        if _int(event_season.get("type")) == 1:
            continue  # preseason

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
    if season is None:  # a day's scoreboard names the season on each event only
        season = next((g.season for g in games if g.season is not None), None)
    return season, week, games


def parse_calendar(raw: Any) -> list[date]:
    """The days a league plays this season, oldest first, from a day's scoreboard."""
    leagues = raw.get("leagues") if isinstance(raw, dict) else None
    league = leagues[0] if isinstance(leagues, list) and leagues else None
    if not isinstance(league, dict) or league.get("calendarType") != "day":
        return []
    days = set()
    for entry in league.get("calendar") or []:
        moment = _time(entry)
        if moment is not None:
            days.add(moment.astimezone(ESPN_ZONE).date())
    return sorted(days)


def _day_key(day: date) -> int:
    """A day as the integer the diary's week column keeps for sports logged by day."""
    return day.year * 10000 + day.month * 100 + day.day


def _raw_stats(team_block: dict) -> dict[str, Any]:
    out = {}
    for stat in team_block.get("statistics") or []:
        if isinstance(stat, dict) and stat.get("name"):
            out[str(stat["name"])] = stat.get("displayValue")
    return out


def _box_stats(team_block: dict) -> dict:
    """One team's football box score, as per-game counting numbers."""
    raw = _raw_stats(team_block)
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


def _nba_box_stats(team_block: dict) -> dict:
    """One team's basketball box score. Possessions are the usual estimate."""
    raw = _raw_stats(team_block)
    fg_made, fg_att = _pair(raw.get("fieldGoalsMade-fieldGoalsAttempted"), "-")
    three_made, three_att = _pair(
        raw.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"), "-"
    )
    ft_made, ft_att = _pair(raw.get("freeThrowsMade-freeThrowsAttempted"), "-")
    off_reb = _int(raw.get("offensiveRebounds"))
    turnovers = _int(raw.get("totalTurnovers"))
    if turnovers is None:
        turnovers = _int(raw.get("turnovers"))
    possessions = None
    if None not in (fg_att, off_reb, turnovers, ft_att):
        possessions = fg_att - off_reb + turnovers + 0.44 * ft_att
    return {
        "fg_made": fg_made,
        "fg_att": fg_att,
        "three_made": three_made,
        "three_att": three_att,
        "ft_made": ft_made,
        "ft_att": ft_att,
        "off_reb": off_reb,
        "def_reb": _int(raw.get("defensiveRebounds")),
        "rebounds": _int(raw.get("totalRebounds")),
        "assists": _int(raw.get("assists")),
        "steals": _int(raw.get("steals")),
        "blocks": _int(raw.get("blocks")),
        "turnovers": turnovers,
        "fouls": _int(raw.get("fouls")),
        "fast_break_points": _int(raw.get("fastBreakPoints")),
        "paint_points": _int(raw.get("pointsInPaint")),
        "possessions": possessions,
    }


def _nhl_box_stats(team_block: dict) -> dict:
    """One team's hockey box score."""
    raw = _raw_stats(team_block)
    return {
        "shots": _int(raw.get("shotsTotal")),
        "pp_goals": _int(raw.get("powerPlayGoals")),
        "pp_opps": _int(raw.get("powerPlayOpportunities")),
        "faceoffs_won": _int(raw.get("faceoffsWon")),
        "hits": _int(raw.get("hits")),
        "blocked_shots": _int(raw.get("blockedShots")),
        "takeaways": _int(raw.get("takeaways")),
        "giveaways": _int(raw.get("giveaways")),
        "penalties": _int(raw.get("penalties")),
        "penalty_minutes": _int(raw.get("penaltyMinutes")),
    }


def _add_football_plays(raw: dict, home: TeamRef, stats: dict[str, dict]) -> None:
    """Touchdowns from the scoring plays and first-half yards from the drives."""
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
    for side in ("home", "away"):
        stats[side]["pass_td"] = touchdowns[side][0]
        stats[side]["rush_td"] = touchdowns[side][1]
        stats[side]["first_half_pass_td"] = touchdowns[side][2]
        stats[side]["first_half_rush_td"] = touchdowns[side][3]
        yards = half_yards.get(side) if half_yards else None
        stats[side]["first_half_pass_yards"] = yards["pass"] if yards else None
        stats[side]["first_half_rush_yards"] = yards["rush"] if yards else None
        stats[side]["first_half_total_yards"] = yards["total"] if yards else None


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

    profile = LOGS[sport]
    boxscore = raw.get("boxscore") if isinstance(raw.get("boxscore"), dict) else {}
    stats: dict[str, dict] = {}
    for block in boxscore.get("teams") or []:
        if not isinstance(block, dict):
            continue
        team = block.get("team") if isinstance(block.get("team"), dict) else {}
        side = block.get("homeAway")
        if side not in ("home", "away"):
            side = "home" if str(team.get("id")) == home.team_id else "away"
        stats[side] = profile.box(block)
    if set(stats) != {"home", "away"}:
        return None

    book = None
    pickcenter = raw.get("pickcenter")
    if isinstance(pickcenter, list) and pickcenter and isinstance(pickcenter[0], dict):
        book = parse_book_line(pickcenter[0])  # the line as it closed
    home_lines, away_lines = _lines(sides["home"]), _lines(sides["away"])
    half = profile.half_periods
    for side, score, lines in (("home", home_score, home_lines), ("away", away_score, away_lines)):
        stats[side]["points"] = score
        stats[side]["first_half_points"] = sum(lines[:half]) if len(lines) >= half else None
        if profile.in_play_periods is not None:
            # A shootout shows as one more line score; the goals that count for the
            # shooting and save percentages are the ones scored in play.
            shootout = len(lines) > profile.in_play_periods
            stats[side]["goals_in_play"] = (
                sum(lines[: profile.in_play_periods]) if shootout else score
            )
    if sport in FOOTBALL:
        _add_football_plays(raw, home, stats)
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


FOOTBALL_METRICS: tuple[Metric, ...] = (
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
NBA_METRICS: tuple[Metric, ...] = (
    Metric("ppg", "Points", "Offense", True),
    Metric("off_rating", "Points per 100 poss", "Offense", True),
    Metric("fg_pct", "FG%", "Offense", True, 1, True),
    Metric("three_pct", "3P%", "Offense", True, 1, True),
    Metric("threes", "3-pointers made", "Offense", True),
    Metric("ft_att", "Free throws att", "Offense", True),
    Metric("off_reb", "Off rebounds", "Offense", True),
    Metric("assists", "Assists", "Offense", True),
    Metric("turnovers", "Turnovers", "Offense", False),
    Metric("paint", "Points in paint", "Offense", True),
    Metric("fast_break", "Fast-break points", "Offense", True),
    Metric("ppg_allowed", "Points allowed", "Defense", False),
    Metric("def_rating", "Allowed per 100 poss", "Defense", False),
    Metric("opp_fg_pct", "Opp FG%", "Defense", False, 1, True),
    Metric("opp_three_pct", "Opp 3P%", "Defense", False, 1, True),
    Metric("def_reb", "Def rebounds", "Defense", True),
    Metric("steals", "Steals", "Defense", True),
    Metric("blocks", "Blocks", "Defense", True),
    Metric("takeaways", "Takeaways", "Defense", True),
    Metric("opp_paint", "Opp points in paint", "Defense", False),
    Metric("pace", "Possessions per game", "Situational", None),
    Metric("reb_margin", "Rebound margin", "Situational", True),
    Metric("to_margin", "Turnover margin", "Situational", True),
    Metric("fouls", "Fouls", "Situational", False),
)
NHL_METRICS: tuple[Metric, ...] = (
    Metric("ppg", "Goals", "Offense", True, 2),
    Metric("shots", "Shots", "Offense", True),
    Metric("shooting_pct", "Shooting %", "Offense", True, 1, True),
    Metric("pp_goals", "Power-play goals", "Offense", True, 2),
    Metric("pp_pct", "Power play %", "Offense", True, 1, True),
    Metric("faceoff_pct", "Faceoff %", "Offense", True, 1, True),
    Metric("giveaways", "Giveaways", "Offense", False),
    Metric("ppg_allowed", "Goals allowed", "Defense", False, 2),
    Metric("shots_allowed", "Shots allowed", "Defense", False),
    Metric("save_pct", "Save %", "Defense", True, 1, True),
    Metric("pk_pct", "Penalty kill %", "Defense", True, 1, True),
    Metric("blocked", "Blocked shots", "Defense", True),
    Metric("hits", "Hits", "Defense", True),
    Metric("takeaways", "Takeaways", "Defense", True),
    Metric("pim", "Penalty minutes", "Situational", False),
    Metric("pp_drawn", "Power plays drawn", "Situational", True),
    Metric("shot_margin", "Shot margin", "Situational", True),
)
SECTIONS = ("Offense", "Defense", "Situational")
# Metrics with a first-half figure: (own or opponent's stats, the per-game stat key).
# Basketball and hockey have only the score by period (the hockey "half" is the
# first period); football has yards and touchdowns from the play-by-play too.
SCORE_FIRST_HALF = {
    "ppg": ("own", "first_half_points"),
    "ppg_allowed": ("opp", "first_half_points"),
}
FOOTBALL_FIRST_HALF = {
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


def _col(rows: list[dict], key: str) -> list:
    return [r.get(key) for r in rows]


def _aggregate_football(games: list[TeamGame]) -> dict[str, float | None]:
    own = [g.own for g in games]
    opp = [g.opp for g in games]
    col = _col
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


def _difference(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _aggregate_nba(games: list[TeamGame]) -> dict[str, float | None]:
    own = [g.own for g in games]
    opp = [g.opp for g in games]
    col = _col
    turnovers, takeaways = _mean(col(own, "turnovers")), _mean(col(opp, "turnovers"))
    off_rating = _per([g.points for g in games], col(own, "possessions"))
    def_rating = _per([g.allowed for g in games], col(opp, "possessions"))
    return {
        "ppg": _mean([g.points for g in games]),
        "off_rating": None if off_rating is None else 100.0 * off_rating,
        "fg_pct": _rate(col(own, "fg_made"), col(own, "fg_att")),
        "three_pct": _rate(col(own, "three_made"), col(own, "three_att")),
        "threes": _mean(col(own, "three_made")),
        "ft_att": _mean(col(own, "ft_att")),
        "off_reb": _mean(col(own, "off_reb")),
        "assists": _mean(col(own, "assists")),
        "turnovers": turnovers,
        "paint": _mean(col(own, "paint_points")),
        "fast_break": _mean(col(own, "fast_break_points")),
        "ppg_allowed": _mean([g.allowed for g in games]),
        "def_rating": None if def_rating is None else 100.0 * def_rating,
        "opp_fg_pct": _rate(col(opp, "fg_made"), col(opp, "fg_att")),
        "opp_three_pct": _rate(col(opp, "three_made"), col(opp, "three_att")),
        "def_reb": _mean(col(own, "def_reb")),
        "steals": _mean(col(own, "steals")),
        "blocks": _mean(col(own, "blocks")),
        "takeaways": takeaways,
        "opp_paint": _mean(col(opp, "paint_points")),
        "pace": _mean(col(own, "possessions")),
        "reb_margin": _difference(_mean(col(own, "rebounds")), _mean(col(opp, "rebounds"))),
        "to_margin": _difference(takeaways, turnovers),
        "fouls": _mean(col(own, "fouls")),
    }


def _aggregate_nhl(games: list[TeamGame]) -> dict[str, float | None]:
    own = [g.own for g in games]
    opp = [g.opp for g in games]
    col = _col
    shots, shots_allowed = _mean(col(own, "shots")), _mean(col(opp, "shots"))
    against = _rate(col(opp, "goals_in_play"), col(opp, "shots"))
    killed = _rate(col(opp, "pp_goals"), col(opp, "pp_opps"))
    faceoffs = [
        None if a is None or b is None else a + b
        for a, b in zip(col(own, "faceoffs_won"), col(opp, "faceoffs_won"), strict=True)
    ]
    return {
        "ppg": _mean([g.points for g in games]),
        "shots": shots,
        "shooting_pct": _rate(col(own, "goals_in_play"), col(own, "shots")),
        "pp_goals": _mean(col(own, "pp_goals")),
        "pp_pct": _rate(col(own, "pp_goals"), col(own, "pp_opps")),
        "faceoff_pct": _rate(col(own, "faceoffs_won"), faceoffs),
        "giveaways": _mean(col(own, "giveaways")),
        "ppg_allowed": _mean([g.allowed for g in games]),
        "shots_allowed": shots_allowed,
        "save_pct": None if against is None else 100.0 - against,
        "pk_pct": None if killed is None else 100.0 - killed,
        "blocked": _mean(col(own, "blocked_shots")),
        "hits": _mean(col(own, "hits")),
        "takeaways": _mean(col(own, "takeaways")),
        "pim": _mean(col(own, "penalty_minutes")),
        "pp_drawn": _mean(col(own, "pp_opps")),
        "shot_margin": _difference(shots, shots_allowed),
    }


@dataclass(frozen=True, slots=True)
class SportLog:
    """How one sport's log is read and shown; the model's own numbers live in projection.TUNING."""

    key: str
    daily: bool  # ESPN lists it by day (US Eastern), not by week
    half_periods: int  # regulation periods that make the sheet's "first half" column
    half_label: str
    in_play_periods: int | None  # periods before a shootout; None where there is no shootout
    unit: str  # "points" or "goals"
    start_label: str  # "Kickoff", "Tip-off", "Puck drop"
    home_label: str  # "home field", "home court", "home ice"
    outdoors: bool  # whether the sheet shows the surface and the weather
    metrics: tuple[Metric, ...]
    first_half_sources: dict[str, tuple[str, str]]
    box: Callable[[dict], dict]  # one team's box score from the summary
    figures: Callable[[list[TeamGame]], dict[str, float | None]]
    note: str  # the sheet's footnote on where the figures come from


LOGS: dict[str, SportLog] = {
    s.key: s
    for s in (
        SportLog(
            "nfl",
            False,
            2,
            "1st half",
            None,
            "points",
            "Kickoff",
            "home field",
            True,
            FOOTBALL_METRICS,
            FOOTBALL_FIRST_HALF,
            _box_stats,
            _aggregate_football,
            "First-half yards come from the play-by-play.",
        ),
        SportLog(
            "cfb",
            False,
            2,
            "1st half",
            None,
            "points",
            "Kickoff",
            "home field",
            True,
            FOOTBALL_METRICS,
            FOOTBALL_FIRST_HALF,
            _box_stats,
            _aggregate_football,
            "First-half yards come from the play-by-play. Sacks and plays are not in "
            "college box scores.",
        ),
        SportLog(
            "nba",
            True,
            2,
            "1st half",
            None,
            "points",
            "Tip-off",
            "home court",
            False,
            NBA_METRICS,
            SCORE_FIRST_HALF,
            _nba_box_stats,
            _aggregate_nba,
            "Possessions are estimated from shots, free throws, offensive rebounds and turnovers.",
        ),
        SportLog(
            "nhl",
            True,
            1,
            "1st period",
            4,
            "goals",
            "Puck drop",
            "home ice",
            False,
            NHL_METRICS,
            SCORE_FIRST_HALF,
            _nhl_box_stats,
            _aggregate_nhl,
            "Goals include an overtime winner but not a shootout; shooting and save "
            "percentages count goals scored in play. The puck line is always 1.5, so the "
            "book's margin is read off its moneyline.",
        ),
    )
}


def aggregate(games: list[TeamGame], sport: str) -> dict[str, float | None]:
    """Per-game figures over a set of one team's games (empty set: every value None)."""
    return LOGS[sport].figures(games)


def first_half(games: list[TeamGame], sport: str) -> dict[str, float | None]:
    """First-half (first-period) per-game figures for the metrics that have one."""
    out = {}
    for key, (side, stat) in LOGS[sport].first_half_sources.items():
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
    records: list[GameRecord], window: str = "last3", sport: str = "nfl"
) -> tuple[dict[str, dict[str, int]], int]:
    """{team_id: {metric: rank}} over every team in the log, and the number of teams."""
    teams: dict[str, TeamRef] = {}
    for record in records:
        teams[record.home.team_id] = record.home
        teams[record.away.team_id] = record.away
    figures = {}
    for team_id in teams:
        games = team_games(records, team_id)
        figures[team_id] = aggregate(last_n(games) if window == "last3" else games, sport)
    ranks: dict[str, dict[str, int]] = {team_id: {} for team_id in teams}
    for metric in LOGS[sport].metrics:
        if metric.higher_is_better is None:
            continue
        per_team = {team_id: fig.get(metric.key) for team_id, fig in figures.items()}
        for team_id, rank in rank_values(per_team, metric.higher_is_better).items():
            ranks[team_id][metric.key] = rank
    return ranks, len(teams)


def margin_ranks(records: list[GameRecord], sport: str = "nfl") -> dict[str, int]:
    """Season point (goal) margin per game, ranked: the sheet's "overall" rank."""
    teams: dict[str, TeamRef] = {}
    for record in records:
        teams[record.home.team_id] = record.home
        teams[record.away.team_id] = record.away
    margins: dict[str, float | None] = {}
    for team_id in teams:
        figures = aggregate(team_games(records, team_id), sport)
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
    sport: str = "nfl",
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
        "season": aggregate(games, sport),
        "split": aggregate(split_games, sport),
        "split_games": len(split_games),
        "split_label": "Home" if side == "home" else "Away",
        "first_half": first_half(games, sport),
        "last3": aggregate(last3, sport),
        "last3_rank": ranks.get(team["team_id"], {}),
        "season_rank": season_ranks.get(team["team_id"], {}),
    }


def build_sheet(
    upcoming: dict, records: list[GameRecord], extra: dict, polymarket: dict | None
) -> dict:
    """Everything the matchup page shows, from the diary's rows."""
    from scanner.projection import book_margin

    sport = upcoming["sport"]
    slate = upcoming["slate"]
    kickoff = _time(slate.get("date"))
    ranks, league_size = league_ranks(records, "last3", sport)
    season_ranks, _ = league_ranks(records, "season", sport)
    overall = margin_ranks(records, sport)
    home = team_block("home", slate, records, ranks, season_ranks, kickoff, overall, sport)
    away = team_block("away", slate, records, ranks, season_ranks, kickoff, overall, sport)
    book = BookLine.from_dict(slate.get("book")) if slate.get("book") else None
    implied = None
    margin = book_margin(book, sport)
    if book is not None and book.total is not None and margin is not None:
        # The spread is the home team's handicap: TROY -10.5 with a total of 50.5
        # means the market expects TROY 30.5, the visitor 20. In hockey the margin
        # comes from the moneyline, since the puck line is always 1.5.
        implied = {"home": (book.total + margin) / 2.0, "away": (book.total - margin) / 2.0}
    fair = book.fair_moneyline() if book is not None else None
    advantages = {}
    for metric in LOGS[sport].metrics:
        if metric.higher_is_better is None:
            continue
        h, a = home["last3_rank"].get(metric.key), away["last3_rank"].get(metric.key)
        if h is None or a is None or h == a:
            advantages[metric.key] = None
        else:
            advantages[metric.key] = "home" if h < a else "away"
    return {
        "sport": sport,
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
    """Scoreboards (by week or by day) and game summaries from ESPN. One request per call."""

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

    def day(self, sport: str, day: str) -> Any:
        """One day's scoreboard, the day as YYYYMMDD in US Eastern time."""
        return self._get(scoreboard_url(SPORTS[sport], day))

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
    days_pending: int = 0  # season days still to read when the budget ran out (by day)
    errors: list[str] = field(default_factory=list)


class GameLog:
    """Keeps the diary's game log current for each sport and builds sheets from it."""

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
        if LOGS[sport].daily:
            return self._refresh_daily(sport, now, budget, summary)
        try:
            season, week, games = parse_week_scoreboard(self.feed.week(sport), sport)
        except GameLogError as exc:
            summary.errors.append(f"{sport} scoreboard: {exc}")
            return summary
        summary.season, summary.week = season, week
        stored = self.diary.football_game_ids(sport)
        budget = self._store_slate(sport, season, week, games, stored, now, budget, summary)
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
                budget = self._store_slate(
                    sport, season, past, past_games, stored, now, budget, summary
                )
        self._reread_stale(sport, now, budget, summary)
        self._refresh_upcoming_summaries(sport, now, summary)
        if self.settings.PROJECTION_ENABLED:
            self._project(sport, season, now, summary)
        return summary

    def _refresh_daily(self, sport, now, budget, summary) -> RefreshSummary:
        """The same for a sport ESPN lists by day: yesterday, today and tomorrow every
        pass, then the season's earlier days from its calendar until each is complete.
        """
        today = now.astimezone(ESPN_ZONE).date()
        slates: list[tuple[date, list[SlateGame]]] = []
        calendar: list[date] = []
        season = None
        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            try:
                raw = self.feed.day(sport, day.strftime("%Y%m%d"))
            except GameLogError as exc:
                summary.errors.append(f"{sport} {day.isoformat()}: {exc}")
                continue
            day_season, _, games = parse_week_scoreboard(raw, sport)
            season = season or day_season
            calendar = calendar or parse_calendar(raw)
            slates.append((day, games))
        if not slates:
            return summary
        summary.season = season
        stored = self.diary.football_game_ids(sport)
        for day, games in slates:
            budget = self._store_slate(
                sport, season, _day_key(day), games, stored, now, budget, summary
            )
        self._store_upcoming(sport, [g for _, games in slates for g in games], now, summary)
        # Earlier days, oldest first, with whatever budget today's games left. A day
        # whose games are all stored (or all preseason) is marked complete and never
        # read again; without a season nothing can be marked, so the walk waits. The
        # days left for a later pass are counted for the status page.
        for day in calendar:
            if day >= today - timedelta(days=1):
                break
            key = _day_key(day)
            if season is not None and self.diary.football_week_complete(sport, season, key):
                continue
            if budget <= 0 or season is None:
                summary.days_pending += 1
                continue
            try:
                raw = self.feed.day(sport, day.strftime("%Y%m%d"))
            except GameLogError as exc:
                summary.errors.append(f"{sport} {day.isoformat()}: {exc}")
                continue
            _, _, past = parse_week_scoreboard(raw, sport)
            budget = self._store_slate(sport, season, key, past, stored, now, budget, summary)
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

    def _store_slate(self, sport, season, key, games, stored, now, budget, summary) -> int:
        """Store a slate's finished games (one week's, or one day's under its day key)."""
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
        if season is not None and key is not None:
            # A slate is done when every listed game is final and stored. An empty
            # one (none listed) is done too, or it would be re-read every pass.
            complete = all(g.completed for g in games) and remaining == 0
            self.diary.mark_football_week(sport, season, key, complete, now)
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
        return [self.refresh(sport, now) for sport in self.settings.gamelog_sports]

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
