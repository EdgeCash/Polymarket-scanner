"""Who is out, and what it does to a game: injury reports, rosters and depth charts.

ESPN's summary of a game still to come carries each side's injury report (the
NFL, the NBA and the NHL publish one; college does not), and each team's roster
page says who is on injured reserve or suspended, with a depth chart in the NFL
naming the starter at every spot. This module turns those into what the model
can use: for each side, the players likely to miss the game, how likely, and
how much of the side's usual strength goes with them.

A player's worth is measured from his own lines in the game log, in units the
fit is in. Basketball counts his production a game (points, with assists at 0.7
and rebounds at 0.5, the usual box-score weights) above what a replacement
supplies; hockey counts a skater's points a game with a little for his ice time
(a goalie is listed at no cost here: who is likely in net, and what the gap to
the side's usual goaltending is worth, is scanner.goalies' business, which reads
this list for who is ruled out); football counts the starting quarterback as a unit of his own, a
back's or receiver's yards a game, and any other starter on the depth chart
as a head. A status is a chance of missing the game, not a certainty: "out",
injured reserve and a suspension count in full, "doubtful" nearly so,
"questionable" and "day-to-day" less than half. The ratings were fit on the
games the side actually played, so a man who has already missed most of them
is already priced in: his worth is scaled by the share of the side's recent
games he played. The sum, times the sport's coefficient a unit, comes off the
side's projected points, so the margin moves and the total with it.

The coefficients are fit by scripts/fit_injuries.py on seasons of box scores
(scripts/fetch_box_scores.py and scripts/bulk_box_scores.py), where an absence
is a man who played his side's previous game and not this one, valued exactly
as this module values him the day before, against an absence-blind expected margin from ratings fit
on the season's earlier games, held out on the last 30% of each season. A unit
of basketball production costs 0.115 points (5,913 NBA games over five seasons;
the held-out miss falls from 14.22 to 14.09); a unit of a skater's production
0.23 goals (2,490 NHL games over two seasons, 2.627 to 2.619); the starting
quarterback 4.9 points (1,119 NFL games over 2021-2025, 212 with a
starter lost; held out 13.22 to 13.10). His worth does not move with how good
he is by any measure the box score offers: a term for the starter's adjusted
yards an attempt, and one for the backup's, each came out at nothing (371 games
started by another man). What history cannot settle keeps a hand-set prior: a
back's or receiver's yards (the box score names no positions; every
yard-earner's absence counted together gives 0.016 a yard over all five seasons
and nothing on the part held out) and the other starters on the depth chart,
which has no past.
College basketball publishes no report, so the last box score stands in for
one: a regular who sat out his side's last game is listed as "missed last game"
at the chance he is still out (0.63 of 15,162 such absences), and a unit of his
production is worth 0.22 points, nearly twice the NBA's, college rotations being
shorter (scripts/fit_absence_proxy.py on 20,642 games; held out 11.79 to 11.77).
College football's stand-in would be the usual quarterback who did not throw last
time; he is still out 64.5% of the time, but what that cost came out at 3.2 points
one season and -0.7 the next, so PROXY_SPORTS leaves football out.

The diary records every adjustment beside the projection's miss with and
without it, so the scorecard says whether they earn their place. Nothing here
is sent or placed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# How likely a listed status is to keep a man out of the game.
STATUS_WEIGHT: dict[str, float] = {
    "out": 1.0,
    "injured_reserve": 1.0,
    "suspension": 1.0,
    "doubtful": 0.9,
    "day_to_day": 0.45,
    "questionable": 0.4,
    "probable": 0.1,
    "active": 0.0,
    # No report at all (college): a regular who sat out his side's last game is still
    # out for the next one about this often (scripts/fit_absence_proxy.py).
    "missed_last_game": 0.63,
}
UNKNOWN_STATUS_WEIGHT = 0.5
# Sports with no injury report, where the last box score has to stand in for one.
PROXY_SPORTS = ("cbb",)
FOOTBALL = ("nfl", "cfb")
USUAL_PASSER_GAMES = 6  # the usual quarterback is judged over this many games before the last
USUAL_PASSER_ATTEMPTS = 15  # and threw at least this often in at least half of them
PROXY_MIN_TEAM_GAMES = 4  # before this a side has no regulars to speak of
PROXY_MIN_SHARE = 0.5  # a regular played at least this share of the side's recent games
RECENT_TEAM_GAMES = 10  # the window a player's share of his side's games is counted over
RECENT_PLAYER_GAMES = 10  # his production is the mean of this many of his latest lines
SKILL_POSITIONS = ("RB", "FB", "WR", "TE")
# Football positions the box score never lists, so a starter there has no lines to count.
UNLISTED_POSITIONS = ("OT", "OG", "OL", "C", "G", "T", "LT", "RT", "LG", "RG", "LS")
QUARTERBACK = "QB"
GOALIE = "G"


@dataclass(frozen=True, slots=True)
class SportRule:
    unit: str  # the margin's unit: points or goals
    cap: float  # the most one side can lose to absences
    replacement: float  # production a game a replacement supplies (basketball, hockey)
    coefficients: dict[str, float]  # margin per unit of each feature


RULES: dict[str, SportRule] = {
    "nba": SportRule("points", 12.0, 8.0, {"production": 0.115}),
    "cbb": SportRule("points", 12.0, 6.0, {"production": 0.22}),
    "nhl": SportRule("goals", 1.5, 0.6, {"production": 0.23}),  # goalies: scanner.goalies
    "nfl": SportRule(
        "points", 14.0, 0.0, {"quarterback": 4.9, "skill_yards": 0.012, "starters": 0.4}
    ),
    "cfb": SportRule(  # the NFL's; college publishes no report, so nothing moves
        "points", 14.0, 0.0, {"quarterback": 4.9, "skill_yards": 0.012, "starters": 0.4}
    ),
}
SPORTS = tuple(RULES)


# -- parsing ------------------------------------------------------------------------------


def normalise_status(text: Any) -> str:
    """ESPN's status as one of STATUS_WEIGHT's keys, or its own words when it is new."""
    words = re.sub(r"[^a-z]+", "_", str(text or "").strip().lower()).strip("_")
    aliases = {
        "o": "out",
        "ir": "injured_reserve",
        "ir_r": "injured_reserve",
        "injured_reserve_r": "injured_reserve",
        "pup": "injured_reserve",
        "pup_r": "injured_reserve",
        "reserve_ex": "injured_reserve",
        "nfi": "injured_reserve",
        "inactive": "out",
        "d": "doubtful",
        "q": "questionable",
        "gtd": "day_to_day",
        "game_time_decision": "day_to_day",
        "dd": "day_to_day",
        "suspended": "suspension",
        "p": "probable",
        "healthy": "active",
    }
    return aliases.get(words, words) or "active"


def status_weight(status: str) -> float:
    return STATUS_WEIGHT.get(status, UNKNOWN_STATUS_WEIGHT)


def _athlete_id(athlete: dict) -> str:
    if athlete.get("id") is not None:
        return str(athlete["id"])
    for link in athlete.get("links") or []:
        match = re.search(r"/id/(\d+)", str((link or {}).get("href") or ""))
        if match:
            return match.group(1)
    return ""


def parse_injury_report(raw: Any, home_id: str, away_id: str) -> dict[str, list[dict]] | None:
    """Each side's injury report from a game summary, or None when the summary has none.

    A side with an empty list has a report that lists nobody; None means the sport
    (or this game) publishes none, which is a different thing.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("injuries"), list):
        return None
    out: dict[str, list[dict]] = {"home": [], "away": []}
    for block in raw["injuries"]:
        if not isinstance(block, dict):
            continue
        team = block.get("team") if isinstance(block.get("team"), dict) else {}
        team_id = str(team.get("id") or "")
        side = "home" if team_id == home_id else "away" if team_id == away_id else None
        if side is None:
            continue
        for entry in block.get("injuries") or []:
            if not isinstance(entry, dict):
                continue
            athlete = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else {}
            player_id = _athlete_id(athlete)
            if not player_id:
                continue
            position = athlete.get("position") if isinstance(athlete.get("position"), dict) else {}
            details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
            kind = entry.get("type") if isinstance(entry.get("type"), dict) else {}
            status = normalise_status(entry.get("status") or kind.get("description"))
            if status == "active":
                continue  # a practice report: the man is playing
            out[side].append(
                {
                    "player_id": player_id,
                    "name": str(athlete.get("displayName") or athlete.get("shortName") or ""),
                    "position": str(position.get("abbreviation") or "") or None,
                    "status": status,
                    "detail": str(details.get("type") or "") or None,
                    "return_date": details.get("returnDate"),
                    "reported_at": entry.get("date"),
                }
            )
    return out


ROSTER_GROUP_STATUS = {
    "injuredreserveorout": "injured_reserve",
    "suspended": "suspension",
    "practicesquad": "practice_squad",
}


def parse_roster(raw: Any) -> list[dict]:
    """Every player on a team's roster page: id, name, position, status and experience."""
    if not isinstance(raw, dict):
        return []
    athletes = raw.get("athletes")
    groups: list[tuple[str | None, list]] = []
    if isinstance(athletes, list):
        if (
            athletes
            and isinstance(athletes[0], dict)
            and isinstance(athletes[0].get("items"), list)
        ):
            groups = [(str(g.get("position") or ""), g.get("items") or []) for g in athletes]
        else:
            groups = [(None, athletes)]
    out: list[dict] = []
    for group, items in groups:
        group_status = ROSTER_GROUP_STATUS.get((group or "").lower())
        for athlete in items:
            if not isinstance(athlete, dict):
                continue
            player_id = _athlete_id(athlete)
            if not player_id:
                continue
            position = athlete.get("position") if isinstance(athlete.get("position"), dict) else {}
            status_raw = athlete.get("status") if isinstance(athlete.get("status"), dict) else {}
            status = normalise_status(status_raw.get("type") or status_raw.get("name"))
            injuries = athlete.get("injuries") if isinstance(athlete.get("injuries"), list) else []
            if injuries and isinstance(injuries[0], dict) and injuries[0].get("status"):
                listed = normalise_status(injuries[0]["status"])
                if status_weight(listed) > status_weight(status):
                    status = listed
            if group_status and (status == "active" or group_status == "practice_squad"):
                status = group_status
            experience = (
                athlete.get("experience") if isinstance(athlete.get("experience"), dict) else {}
            )
            out.append(
                {
                    "player_id": player_id,
                    "name": str(athlete.get("displayName") or athlete.get("fullName") or ""),
                    "position": str(position.get("abbreviation") or "") or None,
                    "status": status,
                    "group": group or None,
                    "jersey": athlete.get("jersey"),
                    "experience": experience.get("years"),
                }
            )
    return out


def parse_depth_chart(raw: Any) -> dict[str, dict]:
    """The best rank each player holds on a team's depth chart: {player_id: {rank, slot, name}}.

    The NFL publishes one per formation (a base defence, a three-receiver offence,
    special teams); a man's rank is the best he has anywhere. A player listed only
    in a slot without a rank is taken as a starter, as ESPN lists starters first.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("depthchart"), list):
        return {}
    out: dict[str, dict] = {}
    for chart in raw["depthchart"]:
        if not isinstance(chart, dict):
            continue
        positions = chart.get("positions") if isinstance(chart.get("positions"), dict) else {}
        for slot, spot in positions.items():
            if not isinstance(spot, dict):
                continue
            for index, entry in enumerate(spot.get("athletes") or []):
                if not isinstance(entry, dict):
                    continue
                athlete = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else entry
                player_id = _athlete_id(athlete)
                if not player_id:
                    continue
                rank = entry.get("rank")
                rank = (
                    int(rank)
                    if isinstance(rank, int | float | str) and str(rank).isdigit()
                    else index + 1
                )
                current = out.get(player_id)
                if current is None or rank < current["rank"]:
                    out[player_id] = {
                        "rank": rank,
                        "slot": str(slot).upper(),
                        "name": str(athlete.get("displayName") or athlete.get("shortName") or ""),
                    }
    return out


# -- worth ---------------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def basketball_production(stats: dict) -> float:
    return (
        float(stats.get("points") or 0)
        + 0.7 * float(stats.get("assists") or 0)
        + 0.5 * float(stats.get("rebounds") or 0)
    )


def hockey_production(stats: dict) -> float:
    toi_minutes = float(stats.get("toi") or 0) / 60.0
    return float(stats.get("goals") or 0) + float(stats.get("assists") or 0) + 0.03 * toi_minutes


def football_yards(stats: dict) -> float:
    return float(stats.get("rush_yards") or 0) + float(stats.get("rec_yards") or 0)


@dataclass
class SideContext:
    """What is known of one side's men before the game, from the log, roster and depth chart."""

    team_id: str
    team_games: list[str]  # the side's game ids this season, oldest first
    lines: dict[str, list[dict]]  # player id -> his played lines, oldest first
    names: dict[str, str]
    positions: dict[str, str | None]
    depth: dict[str, dict]  # player id -> {rank, slot} from the depth chart
    roster: dict[str, dict]  # player id -> roster row

    @classmethod
    def build(
        cls, team_id: str, rows: list[dict], roster: list[dict], depth: dict[str, dict]
    ) -> SideContext:
        games: dict[str, str] = {}
        lines: dict[str, list[dict]] = {}
        names: dict[str, str] = {}
        positions: dict[str, str | None] = {}
        for row in rows:
            if str(row.get("team_id")) != str(team_id):
                continue
            games[row["game_id"]] = row["date"]
            names[row["player_id"]] = row["name"]
            if row.get("position"):
                positions[row["player_id"]] = row["position"]
            if row.get("played"):
                lines.setdefault(row["player_id"], []).append(row)
        by_player = {r["player_id"]: r for r in roster}
        for player_id, row in by_player.items():
            names.setdefault(player_id, row["name"])
            if row.get("position") and not positions.get(player_id):
                positions[player_id] = row["position"]
        ordered = [g for g, _ in sorted(games.items(), key=lambda kv: (kv[1], kv[0]))]
        return cls(str(team_id), ordered, lines, names, positions, depth, by_player)

    def played_share(self, player_id: str) -> float:
        """The share of the side's last few games the man played; 1 when the log is empty."""
        window = self.team_games[-RECENT_TEAM_GAMES:]
        if not window:
            return 1.0
        played = {r["game_id"] for r in self.lines.get(player_id, [])}
        return sum(1 for g in window if g in played) / len(window)

    def recent(self, player_id: str, measure) -> float:
        rows = self.lines.get(player_id, [])[-RECENT_PLAYER_GAMES:]
        return _mean([measure(r["stats"]) for r in rows])

    def is_starter(self, player_id: str) -> bool:
        spot = self.depth.get(player_id)
        return spot is not None and spot["rank"] == 1

    def threw_in(self, player_id: str, game_id: str, at_least: float = 1.0) -> bool:
        return any(
            row["game_id"] == game_id and float(row["stats"].get("pass_att") or 0) >= at_least
            for row in self.lines.get(player_id, [])
        )

    def usual_quarterback(self) -> str | None:
        """The side's quarterback going into its last game: the man who threw most over
        the games before it, if he was the regular starter in them."""
        before = self.team_games[-(USUAL_PASSER_GAMES + 1) : -1]
        if len(before) < 3:
            return None
        attempts: dict[str, float] = {}
        started: dict[str, int] = {}
        for player_id, rows in self.lines.items():
            for row in rows:
                thrown = float(row["stats"].get("pass_att") or 0)
                if row["game_id"] in before and thrown > 0:
                    attempts[player_id] = attempts.get(player_id, 0.0) + thrown
                    if thrown >= USUAL_PASSER_ATTEMPTS:
                        started[player_id] = started.get(player_id, 0) + 1
        if not attempts:
            return None
        passer = max(attempts, key=attempts.get)
        return passer if started.get(passer, 0) * 2 >= len(before) else None

    def quarterback_one(self) -> str | None:
        """The starting quarterback: rank one at QB on the depth chart, else the man who threw
        most in the side's last game."""
        for player_id, spot in self.depth.items():
            if spot["rank"] == 1 and spot["slot"].startswith(QUARTERBACK):
                return player_id
        if not self.team_games:
            return None
        last = self.team_games[-1]
        best, attempts = None, 0
        for player_id, rows in self.lines.items():
            for row in rows:
                if row["game_id"] == last and float(row["stats"].get("pass_att") or 0) > attempts:
                    best, attempts = player_id, float(row["stats"]["pass_att"])
        return best


def missed_last_game(sport: str, context: SideContext) -> list[dict]:
    """Report entries for the regulars who sat out a side's last game, where the sport
    publishes no report: the nearest thing to one the box scores hold.

    A regular has played at least PROXY_MIN_SHARE of the side's recent games (the one
    he missed counted against him) and produces more than a replacement.
    """
    if sport not in PROXY_SPORTS or len(context.team_games) < PROXY_MIN_TEAM_GAMES:
        return []
    last = context.team_games[-1]
    if sport in FOOTBALL:
        passer = context.usual_quarterback()
        if passer is None or context.threw_in(passer, last):
            return []
        return [
            {
                "player_id": passer,
                "name": context.names.get(passer, ""),
                "position": QUARTERBACK,
                "status": "missed_last_game",
                "detail": None,
                "return_date": None,
                "reported_at": None,
                "role": QUARTERBACK,
            }
        ]
    out = []
    for player_id, lines in context.lines.items():
        if any(row["game_id"] == last for row in lines):
            continue
        if context.played_share(player_id) < PROXY_MIN_SHARE:
            continue
        if context.recent(player_id, basketball_production) <= RULES[sport].replacement:
            continue
        out.append(
            {
                "player_id": player_id,
                "name": context.names.get(player_id, ""),
                "position": context.positions.get(player_id),
                "status": "missed_last_game",
                "detail": None,
                "return_date": None,
                "reported_at": None,
            }
        )
    return out


def absentees(
    sport: str, report: list[dict] | None, context: SideContext, has_report: bool = True
) -> list[dict]:
    """One side's likely absentees with their worth in the sport's units.

    The report is the game's injury list for the side; the roster adds anyone on
    injured reserve or suspended that the report leaves out, and where the sport
    publishes no report at all (``has_report`` false) the regulars who sat out the
    side's last game stand in for one. Each entry carries the man's status, the
    chance it keeps him out, his units by feature and how much of the side's margin
    that costs, before the side's cap.
    """
    if sport not in RULES:
        return []
    rule = RULES[sport]
    listed: dict[str, dict] = {}
    if not has_report:
        for entry in missed_last_game(sport, context):
            listed[entry["player_id"]] = entry
    for entry in report or []:
        listed[entry["player_id"]] = dict(entry)
    for player_id, row in context.roster.items():
        status = row.get("status") or "active"
        if status in ("active", "practice_squad") or status_weight(status) <= 0:
            continue
        if player_id in listed:
            if status_weight(status) > status_weight(listed[player_id]["status"]):
                listed[player_id]["status"] = status
            continue
        listed[player_id] = {
            "player_id": player_id,
            "name": row["name"],
            "position": row.get("position"),
            "status": status,
            "detail": None,
            "return_date": None,
            "reported_at": None,
        }
    quarterback = context.quarterback_one() if sport in ("nfl", "cfb") else None
    out: list[dict] = []
    for player_id, entry in listed.items():
        weight = status_weight(entry["status"])
        position = entry.get("position") or context.positions.get(player_id)
        units: dict[str, float] = {}
        if sport in ("nba", "cbb"):
            production = context.recent(player_id, basketball_production)
            units["production"] = max(production - rule.replacement, 0.0)
        elif sport == "nhl":
            # Who is in goal is scanner.goalies' business: a goalie is listed, at no cost.
            if GOALIE not in ((position or ""), context.positions.get(player_id)):
                production = context.recent(player_id, hockey_production)
                units["production"] = max(production - rule.replacement, 0.0)
        else:
            if (
                entry.get("role") == QUARTERBACK
                or player_id == quarterback
                or (
                    quarterback is None
                    and (position or "") == QUARTERBACK
                    and context.is_starter(player_id)
                )
            ):
                units["quarterback"] = 1.0
            elif (position or "") in SKILL_POSITIONS:
                units["skill_yards"] = context.recent(player_id, football_yards)
            elif context.is_starter(player_id):
                units["starters"] = 1.0
        if context.lines.get(player_id):
            share = context.played_share(player_id)
        elif not context.team_games:
            share = 1.0 if units else 0.0  # nothing played yet: take the list at its word
        elif sport in ("nfl", "cfb") and (position or "") in UNLISTED_POSITIONS:
            share = 1.0 if context.is_starter(player_id) else 0.0  # a lineman has no lines
        else:
            share = 0.0  # listed, never played this season: the ratings never had him
        worth = sum(rule.coefficients.get(k, 0.0) * v for k, v in units.items())
        cost = round(weight * share * worth, 2)
        out.append(
            {
                "player_id": player_id,
                "name": entry.get("name") or context.names.get(player_id, ""),
                "position": position,
                "status": entry["status"],
                "detail": entry.get("detail"),
                "weight": weight,
                "played_share": round(share, 2),
                "units": {k: round(v, 2) for k, v in units.items()},
                "cost": cost,
            }
        )
    out.sort(key=lambda e: (-e["cost"], e["name"]))
    return out


def margin_adjustment(sport: str, home: list[dict] | None, away: list[dict] | None) -> dict | None:
    """Points to add to the projected home margin for both sides' absentees, with the lists.

    None outside the sports with a report, or when neither side lists anyone. Each
    side's loss is capped at the sport's cap; the margin is the away side's loss less
    the home side's.
    """
    if sport not in RULES or not (home or away):
        return None
    rule = RULES[sport]
    home_loss = min(sum(e["cost"] for e in home or []), rule.cap)
    away_loss = min(sum(e["cost"] for e in away or []), rule.cap)
    keep = ("name", "position", "status", "detail", "weight", "cost")
    return {
        "home": [{k: e.get(k) for k in keep} for e in home or []],
        "away": [{k: e.get(k) for k in keep} for e in away or []],
        "home_loss": round(home_loss, 2),
        "away_loss": round(away_loss, 2),
        "margin": round(away_loss - home_loss, 2),
    }


def availability(absent: list[dict]) -> dict[str, float]:
    """Each listed man's chance of missing the game, for the player model."""
    return {e["player_id"]: e["weight"] for e in absent}


def describe(adjustment: dict | None, home: str, away: str, unit: str) -> str:
    """One phrase for a sheet: 'BOS without Tatum (out), Brown (questionable): margin -4.1'."""
    if not adjustment or not (adjustment["home_loss"] or adjustment["away_loss"]):
        return ""
    parts = []
    for side, label in (("home", home), ("away", away)):
        costly = [e for e in adjustment[side] if e["cost"] > 0][:3]
        if not costly:
            continue
        names = ", ".join(
            f"{_surname(e['name'])} ({e['status'].replace('_', ' ')})" for e in costly
        )
        more = len([e for e in adjustment[side] if e["cost"] > 0]) - len(costly)
        parts.append(f"{label} without {names}" + (f" and {more} more" if more > 0 else ""))
    return f"{'; '.join(parts)}: margin {adjustment['margin']:+.1f} {unit}"


def _surname(name: str) -> str:
    bits = name.split()
    if len(bits) > 1 and bits[-1].rstrip(".").lower() in ("jr", "sr", "ii", "iii", "iv"):
        return " ".join(bits[-2:])
    return bits[-1] if bits else name
