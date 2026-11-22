"""The NHL's own pre-game sheet: who is scratched tonight.

Half an hour or so before the puck drops the league posts each side's scratches
(api-web.nhle.com, the gamecenter's right rail): the men on the roster who will
not dress, hurt or healthy. ESPN's injury report misses the healthy ones and is
often a day behind on the rest, so this is the last and best word on who plays.
The scanner asks for it only for games about to start, matches the league's game
to ESPN's by the two sides and the hour, and matches each scratched man to the
player the log knows by his name. A scratch counts as "out" in full; it joins the
injury report and goes through the same valuation (scanner.injuries), and a
scratched goalie is ruled out of tonight's start (scanner.goalies).

This module only reads and parses. Nothing here is sent or placed.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any

NHL_API = "https://api-web.nhle.com/v1"
# ESPN's abbreviation -> the league's, where they differ.
NHL_ALIASES = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}
MATCH_WINDOW = timedelta(minutes=90)  # how far apart the two feeds' start times may sit


def normalise_name(name: Any) -> str:
    """A name as letters only, without accents or case: 'Tim Stützle' -> 'tim stutzle'."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z ]+", "", text.lower().replace("-", " ")).strip()


def _time(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_nhl_schedule(raw: Any) -> list[dict]:
    """Every game in the league's schedule response: id, start and the two abbreviations."""
    out: list[dict] = []
    if not isinstance(raw, dict):
        return out
    for day in raw.get("gameWeek") or []:
        for game in (day or {}).get("games") or []:
            if not isinstance(game, dict):
                continue
            start = _time(game.get("startTimeUTC"))
            home = (game.get("homeTeam") or {}).get("abbrev")
            away = (game.get("awayTeam") or {}).get("abbrev")
            if game.get("id") is None or start is None or not home or not away:
                continue
            out.append({"game_id": str(game["id"]), "start": start, "home": home, "away": away})
    return out


def match_game(
    schedule: list[dict], home_abbr: str, away_abbr: str, kickoff: datetime
) -> str | None:
    """The league's id for ESPN's game: the same two sides, starting within the window."""
    home = NHL_ALIASES.get(home_abbr, home_abbr)
    away = NHL_ALIASES.get(away_abbr, away_abbr)
    for game in schedule:
        if game["home"] == home and game["away"] == away:
            if abs(game["start"] - kickoff) <= MATCH_WINDOW:
                return game["game_id"]
    return None


def parse_scratches(raw: Any) -> dict[str, list[str]] | None:
    """Each side's scratched men by name, or None while the sheet has not been posted.

    The sheet is taken as posted once it names the officials or anybody scratched:
    before that every list is empty, which must not read as "nobody is scratched".
    """
    info = raw.get("gameInfo") if isinstance(raw, dict) else None
    if not isinstance(info, dict):
        return None
    out: dict[str, list[str]] = {}
    for side, key in (("home", "homeTeam"), ("away", "awayTeam")):
        team = info.get(key) if isinstance(info.get(key), dict) else {}
        names = []
        for man in team.get("scratches") or []:
            if not isinstance(man, dict):
                continue
            first = (man.get("firstName") or {}).get("default") or ""
            last = (man.get("lastName") or {}).get("default") or ""
            name = f"{first} {last}".strip()
            if name:
                names.append(name)
        out[side] = names
    posted = bool(info.get("referees") or info.get("linesmen") or out["home"] or out["away"])
    return out if posted else None


def as_report(
    names: list[str], known: dict[str, str], positions: dict[str, str | None]
) -> list[dict]:
    """Scratched men as injury-report entries, for the ones the log or roster knows by name.

    ``known`` is player id -> name for the side; a name it does not hold is left out,
    since a man the model has never seen costs nothing anyway.
    """
    by_name = {normalise_name(name): pid for pid, name in known.items() if name}
    out = []
    for name in names:
        player_id = by_name.get(normalise_name(name))
        if player_id is None:
            continue
        out.append(
            {
                "player_id": player_id,
                "name": known[player_id],
                "position": positions.get(player_id),
                "status": "out",
                "detail": "scratch",
                "return_date": None,
                "reported_at": None,
            }
        )
    return out
