"""Who is in goal tonight, and what it is worth: hockey's biggest unknown before a game.

A team's rating carries its goaltending as it has been: the goals against of
whoever started, in the share they started. Tonight's game has one goalie, and
when he is not the usual one the rating is off by the gap between them. The gap is
small in save rate and large in goals: one point of save percentage is about
three tenths of a goal over a night's thirty shots.

A goalie's quality is his save rate shrunk hard toward the league's, since a
save rate takes thousands of shots to mean much: PRIOR_SHOTS of league play are
added to his own, and his own are this season's with the side plus his earlier
seasons' at CARRY (scanner/goalie_priors.json, built from last season's box
scores by scripts/build_goalie_priors.py; one season alone told the fit nothing).
The side's usual quality is the same thing averaged
over its games by who started them. Who starts tonight is not announced where the
scanner can read it, but the schedule and the box scores say most of it: on the
second night of a back-to-back the man who played last night starts again about
one time in eleven (REPEAT_ON_BACK_TO_BACK; 1,141 such nights over two seasons), otherwise
the share of recent starts is the best guess, and a goalie the injury report or
the scratch list rules out does not start at all. The expected quality over
those chances, less the usual, times the shots the side allows a night, is the
goals the side's rating is off by tonight; the difference between the two sides
moves the margin.

The box score lists only the goalies who played, so the side's goalies are the
men who have played for it lately, and history cannot say who was hurt on the
night: the fit below knows nothing of the injury report, which can only help.

Fit by scripts/fit_goalies.py on three seasons, 2023-24 as the memory and 2,450
games of the next two scored. The goalie who did start, known only afterwards,
explains the margin's miss at 1.16 goals a goal of edge (give or take 0.30; the
miss held out 2.5924 to 2.5842), so the save rates mean what they say and the
edge counts in full. What is known beforehand moves 1,900 of those games by a
twentieth of a goal on average and takes the held-out miss from 2.5924 to
2.5897, with the back-to-back fit beside it; the back-to-back itself falls from
0.37 to 0.32 goals when the edge is in, which is the tired side's second goalie,
so scanner.rest gives that much back. Nothing here is sent or placed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

GOALIE = "G"
SPORTS = ("nhl",)
PRIOR_SHOTS = 500.0  # league-average shots added to a goalie's own before his rate is trusted
CARRY = 0.5  # what a shot faced in an earlier season counts beside one faced in this one
PRIORS_PATH = Path(__file__).with_name("goalie_priors.json")
LEAGUE_SAVE_RATE = 0.900  # used until the log has enough shots to say
MIN_LEAGUE_SHOTS = 2000.0
REPEAT_ON_BACK_TO_BACK = 0.09  # the chance last night's starter starts again tonight
RECENT_STARTS = 20  # starts shares are counted over this many of the side's latest games
WITH_SIDE_WITHIN = 10  # a goalie who has not played in this many of its games has moved on
DEFAULT_SHOTS = 29.0
EDGE_COEFFICIENT = 1.0  # goals of margin per goal of expected goaltending edge
ABSENT_FROM = 0.75  # a listed chance of missing the game at or above this rules him out
MIN_TEAM_GAMES = 5  # below this the side's usual goaltending is not yet known


@dataclass
class TeamGoalies:
    """One side's goaltending so far this season, from the log's player lines."""

    team_id: str
    starts: list[str] = field(default_factory=list)  # each game's starter, oldest first
    played: list[set[str]] = field(default_factory=list)  # each game's goalies who played
    saves: dict[str, float] = field(default_factory=dict)
    shots: dict[str, float] = field(default_factory=dict)
    # Each goalie's saves and shots from earlier seasons, already faded among themselves.
    career: dict[str, tuple[float, float]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    shots_against: float = 0.0

    @property
    def games(self) -> int:
        return len(self.starts)

    @property
    def shots_per_game(self) -> float:
        return self.shots_against / self.games if self.games else DEFAULT_SHOTS


@lru_cache(maxsize=1)
def _load_priors() -> dict:
    try:
        with open(PRIORS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def career_for(season: int | None) -> dict[str, tuple[float, float]]:
    """Each goalie's earlier-season saves and shots for ``season``: the file's, if it was
    built from the season before, else nothing (an older file says too little)."""
    data = _load_priors()
    if season is None or data.get("season") != season - 1:
        return {}
    return {
        str(pid): (float(v.get("saves") or 0), float(v.get("shots") or 0))
        for pid, v in (data.get("goalies") or {}).items()
    }


def league_save_rate(rows: list[dict]) -> float:
    saves = shots = 0.0
    for row in rows:
        if row.get("position") == GOALIE and row.get("played"):
            saves += float(row["stats"].get("saves") or 0)
            shots += float(row["stats"].get("shots_against") or 0)
    return saves / shots if shots >= MIN_LEAGUE_SHOTS else LEAGUE_SAVE_RATE


def team_goalies(
    rows: list[dict], team_id: str, career: dict[str, tuple[float, float]] | None = None
) -> TeamGoalies:
    """A side's goalies from the season's player rows (the diary's, oldest game first)."""
    out = TeamGoalies(str(team_id), career=career or {})
    by_game: dict[str, list[dict]] = {}
    order: dict[str, str] = {}
    for row in rows:
        if str(row.get("team_id")) != str(team_id) or row.get("position") != GOALIE:
            continue
        by_game.setdefault(row["game_id"], []).append(row)
        order[row["game_id"]] = row["date"]
    for game_id in sorted(by_game, key=lambda g: (order[g], g)):
        listed = by_game[game_id]
        played = [r for r in listed if r.get("played")]
        if not played:
            continue
        starter = max(played, key=lambda r: float(r["stats"].get("toi") or 0))
        out.starts.append(starter["player_id"])
        out.played.append({r["player_id"] for r in played})
        for row in listed:
            out.names[row["player_id"]] = row["name"]
        for row in played:
            pid = row["player_id"]
            faced = float(row["stats"].get("shots_against") or 0)
            out.saves[pid] = out.saves.get(pid, 0.0) + float(row["stats"].get("saves") or 0)
            out.shots[pid] = out.shots.get(pid, 0.0) + faced
            out.shots_against += faced
    return out


def quality(team: TeamGoalies, player_id: str, league: float) -> float:
    """A goalie's save rate, this season's with the side and his earlier ones faded,
    shrunk toward the league's."""
    before_saves, before_shots = team.career.get(player_id, (0.0, 0.0))
    saves = team.saves.get(player_id, 0.0) + CARRY * before_saves
    shots = team.shots.get(player_id, 0.0) + CARRY * before_shots
    return (saves + PRIOR_SHOTS * league) / (shots + PRIOR_SHOTS)


def start_chances(
    team: TeamGoalies, back_to_back: bool, unavailable: set[str] | None = None
) -> dict[str, float]:
    """Each goalie's chance of starting tonight, from what is known before the game."""
    unavailable = unavailable or set()
    recent = team.starts[-RECENT_STARTS:]
    with_side: set[str] = set().union(*team.played[-WITH_SIDE_WITHIN:]) if team.played else set()
    share = {
        pid: recent.count(pid) + 0.5  # half a start each, so a man yet to start has a chance
        for pid in with_side
        if pid not in unavailable
    }
    if not share:
        return {}
    last = team.starts[-1] if team.starts else None
    if back_to_back and last in share and len(share) > 1:
        others = {pid: s for pid, s in share.items() if pid != last}
        scale = (1.0 - REPEAT_ON_BACK_TO_BACK) / sum(others.values())
        return {last: REPEAT_ON_BACK_TO_BACK, **{pid: s * scale for pid, s in others.items()}}
    total = sum(share.values())
    return {pid: s / total for pid, s in share.items()}


def expected_edge(
    team: TeamGoalies,
    league: float,
    back_to_back: bool,
    unavailable: set[str] | None = None,
) -> dict | None:
    """The goals a side's rating is off by tonight for who is likely in goal, with the list.

    None while the side has too few games for its usual goaltending to be known, or
    when nobody is known to be available.
    """
    if team.games < MIN_TEAM_GAMES:
        return None
    chances = start_chances(team, back_to_back, unavailable)
    if not chances:
        return None
    usual = sum(quality(team, pid, league) for pid in team.starts) / team.games
    tonight = sum(chance * quality(team, pid, league) for pid, chance in chances.items())
    edge = (tonight - usual) * team.shots_per_game
    return {
        "edge": round(edge, 3),
        "back_to_back": back_to_back,
        "goalies": [
            {
                "player_id": pid,
                "name": team.names.get(pid, ""),
                "chance": round(chance, 2),
                "save_rate": round(quality(team, pid, league), 4),
            }
            for pid, chance in sorted(chances.items(), key=lambda kv: -kv[1])
        ],
        "usual_save_rate": round(usual, 4),
    }


def margin_adjustment(sport: str, home: dict | None, away: dict | None) -> dict | None:
    """Goals to add to the projected home margin for the two sides' likely goalies."""
    if sport not in SPORTS or (home is None and away is None):
        return None
    home_edge = (home or {}).get("edge", 0.0)
    away_edge = (away or {}).get("edge", 0.0)
    return {
        "home": home,
        "away": away,
        "home_edge": round(EDGE_COEFFICIENT * home_edge, 2),
        "away_edge": round(EDGE_COEFFICIENT * away_edge, 2),
        "margin": round(EDGE_COEFFICIENT * (home_edge - away_edge), 2),
    }


def describe(adjustment: dict | None, home: str, away: str) -> str:
    """One phrase for a sheet: 'BOS likely in goal: Korpisalo 90% (.897 against .908 usual)'."""
    if not adjustment or not adjustment["margin"]:
        return ""
    parts = []
    for side, label in (("home", home), ("away", away)):
        info = adjustment.get(side)
        if not info or abs(adjustment[f"{side}_edge"]) < 0.005:
            continue
        top = info["goalies"][0]
        surname = top["name"].split()[-1] if top["name"] else "the other goalie"
        parts.append(
            f"{label} likely in goal: {surname} {top['chance'] * 100:.0f}% "
            f"({_rate(top['save_rate'])} against {_rate(info['usual_save_rate'])} usual)"
        )
    return f"{'; '.join(parts)}: margin {adjustment['margin']:+.2f} goals" if parts else ""


def _rate(value: float) -> str:
    return f"{value:.3f}".lstrip("0")
