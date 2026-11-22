"""Who is starting in goal tonight, as the people who watch the morning skate report it.

scanner.goalies has to guess the starter from the schedule and the box scores. Daily
Faceoff does not guess: its starting-goalies page lists every game on the day's
slate with each side's man and how sure the report is: "Confirmed" (he led the side
out for warmups, or the coach said so), "Likely" (a beat writer's read of the
morning skate), or nothing yet (the site's own projection, which is no better than
the scanner's and is not used). The page embeds its data as JSON, and this module
reads only that: the two sides, the start time, each goalie's name and the report's
strength, source and time. One request covers the whole slate, so the scanner asks
at most every half hour on a day with a game inside the window.

A report is a chance, not a certainty: over ten days in November 2026 the man
marked confirmed was the one who played most in 61 games of 64, and the other
three look like starters pulled early, so "confirmed" is taken at 0.97; "likely"
is set by hand at 0.85 until there is a record to fit it on (the page keeps no
history of what it said before a game, only what it ended up saying). What the
named man is worth is scanner.goalies' business: scripts/fit_goalies.py found the
goalie who did start explains the margin's miss at 1.16 goals a goal of edge, three
times what the guess alone is worth.

This module only reads and parses the public page (the site's robots file allows
it and closes only its API and CMS paths, which are not touched). Nothing here is
sent or placed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from scanner.scratches import MATCH_WINDOW, NHL_ALIASES, normalise_name

STARTERS_URL = "https://www.dailyfaceoff.com/starting-goalies"
# How likely the named man is to start, by the strength of the report.
STATUS_CHANCE = {"confirmed": 0.97, "likely": 0.85}
_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _time(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _goalie(game: dict, side: str) -> dict | None:
    name = game.get(f"{side}GoalieName")
    status = str(game.get(f"{side}NewsStrengthName") or "").strip().lower()
    if not isinstance(name, str) or not name.strip() or status not in STATUS_CHANCE:
        return None  # nobody named, or only the site's own projection
    return {
        "name": name.strip(),
        "status": status,
        "detail": (str(game.get(f"{side}NewsDetails") or "").strip() or None),
        "source": (str(game.get(f"{side}NewsSourceName") or "").strip() or None),
        "reported_at": game.get(f"{side}NewsCreatedAt"),
    }


def parse_starting_goalies(html: Any) -> list[dict]:
    """Every game on the page: its start, the two sides and each side's reported starter
    (None where there is no report yet). An unreadable page gives an empty list."""
    match = _DATA.search(html) if isinstance(html, str) else None
    if match is None:
        return []
    try:
        games = json.loads(match.group(1))["props"]["pageProps"]["data"]
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        start = _time(game.get("dateGmt"))
        home, away = game.get("homeTeamAbbreviation"), game.get("awayTeamAbbreviation")
        if start is None or not home or not away:
            continue
        out.append(
            {
                "start": start,
                "home": str(home),
                "away": str(away),
                "home_goalie": _goalie(game, "home"),
                "away_goalie": _goalie(game, "away"),
            }
        )
    return out


def match_starters(
    listing: list[dict], home_abbr: str, away_abbr: str, kickoff: datetime
) -> dict | None:
    """ESPN's game on the page: the same two sides, starting within the window. Returns
    {"home": report or None, "away": report or None}, or None when the game is not listed."""
    home = NHL_ALIASES.get(home_abbr, home_abbr)
    away = NHL_ALIASES.get(away_abbr, away_abbr)
    for game in listing:
        if game["home"] == home and game["away"] == away:
            if abs(game["start"] - kickoff) <= MATCH_WINDOW:
                return {"home": game["home_goalie"], "away": game["away_goalie"]}
    return None


def named_starter(report: dict | None, known: dict[str, str]) -> dict | None:
    """A side's reported starter as scanner.goalies wants him: the player id the log or
    the carry-over file knows him by (a made-up one for a man neither has seen, who is
    then rated at the league's rate), his name, the report's strength and its chance."""
    if not report or report.get("status") not in STATUS_CHANCE:
        return None
    wanted = normalise_name(report["name"])
    player_id = next((pid for pid, name in known.items() if normalise_name(name) == wanted), None)
    return {
        "player_id": player_id or f"name:{wanted}",
        "name": report["name"],
        "status": report["status"],
        "chance": STATUS_CHANCE[report["status"]],
    }
