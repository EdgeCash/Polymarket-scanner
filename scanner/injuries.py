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
supplies; hockey counts a skater's points a game with a little for his ice time,
and the starting goalie (the one with the most ice time this season) as a unit
of his own; football counts the starting quarterback as a unit of his own, a
back's or receiver's yards a game, and any other starter on the depth chart
as a head. A status is a chance of missing the game, not a certainty: "out",
injured reserve and a suspension count in full, "doubtful" nearly so,
"questionable" and "day-to-day" less than half. The ratings were fit on the
games the side actually played, so a man who has already missed most of them
is already priced in: his worth is scaled by the share of the side's recent
games he played. The sum, times the sport's coefficient a unit, comes off the
side's projected points; the margin moves, the total does not.

The coefficients are set by hand from what lines move when a star sits (a
top-five NBA player about five points, a starting quarterback four to five,
a starting goalie a quarter of a goal), until scripts/fit_injuries.py has
enough seasons of box scores to fit them; the diary records every adjustment
beside the projection's miss with and without it, so the scorecard says
whether they earn their place. Nothing here is sent or placed.
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
}
UNKNOWN_STATUS_WEIGHT = 0.5
RECENT_TEAM_GAMES = 10  # the window a player's share of his side's games is counted over
RECENT_PLAYER_GAMES = 10  # his production is the mean of this many of his latest lines
SKILL_POSITIONS = ("RB", "FB", "WR", "TE")
QUARTERBACK = "QB"
GOALIE = "G"


@dataclass(frozen=True, slots=True)
class SportRule:
    unit: str  # the margin's unit: points or goals
    cap: float  # the most one side can lose to absences
    replacement: float  # production a game a replacement supplies (basketball, hockey)
    coefficients: dict[str, float]  # margin per unit of each feature


RULES: dict[str, SportRule] = {
    "nba": SportRule("points", 12.0, 8.0, {"production": 0.15}),
    "cbb": SportRule("points", 12.0, 6.0, {"production": 0.12}),
    "nhl": SportRule("goals", 1.5, 0.6, {"production": 0.25, "goalie": 0.25}),
    "nfl": SportRule(
        "points", 14.0, 0.0, {"quarterback": 4.5, "skill_yards": 0.012, "starters": 0.4}
    ),
    "cfb": SportRule(
        "points", 14.0, 0.0, {"quarterback": 4.5, "skill_yards": 0.012, "starters": 0.4}
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

    def goalie_one(self) -> str | None:
        """The starting goalie: the one with the most ice time this season."""
        best, most = None, 0.0
        for player_id, rows in self.lines.items():
            if (self.positions.get(player_id) or "") != GOALIE:
                continue
            toi = sum(float(r["stats"].get("toi") or 0) for r in rows)
            if toi > most:
                best, most = player_id, toi
        return best


def absentees(sport: str, report: list[dict] | None, context: SideContext) -> list[dict]:
    """One side's likely absentees with their worth in the sport's units.

    The report is the game's injury list for the side; the roster adds anyone on
    injured reserve or suspended that the report leaves out. Each entry carries the
    man's status, the chance it keeps him out, his units by feature and how much of
    the side's margin that costs, before the side's cap.
    """
    if sport not in RULES:
        return []
    rule = RULES[sport]
    listed: dict[str, dict] = {}
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
    goalie = context.goalie_one() if sport == "nhl" else None
    out: list[dict] = []
    for player_id, entry in listed.items():
        weight = status_weight(entry["status"])
        position = entry.get("position") or context.positions.get(player_id)
        units: dict[str, float] = {}
        if sport in ("nba", "cbb"):
            production = context.recent(player_id, basketball_production)
            units["production"] = max(production - rule.replacement, 0.0)
        elif sport == "nhl":
            if (position or "") == GOALIE or context.positions.get(player_id) == GOALIE:
                units["goalie"] = 1.0 if player_id == goalie else 0.0
            else:
                production = context.recent(player_id, hockey_production)
                units["production"] = max(production - rule.replacement, 0.0)
        else:
            if player_id == quarterback or (
                quarterback is None
                and (position or "") == QUARTERBACK
                and context.is_starter(player_id)
            ):
                units["quarterback"] = 1.0
            elif (position or "") in SKILL_POSITIONS:
                units["skill_yards"] = context.recent(player_id, football_yards)
            elif context.is_starter(player_id):
                units["starters"] = 1.0
        share = (
            context.played_share(player_id)
            if context.lines.get(player_id)
            else (1.0 if context.is_starter(player_id) or player_id == goalie else 0.0)
        )
        if not context.team_games:
            share = 1.0 if units else 0.0
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
