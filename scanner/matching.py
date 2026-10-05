"""Pairs each Polymarket game with the same game in the ESPN feed.

A pair needs both teams to match and the kickoffs to agree. Team names are
compared by abbreviation or by normalized name against ESPN's location and
display name, with a short alias table for the handful of schools the two
feeds spell differently. Anything ambiguous is left unmatched and logged:
an unmatched game costs an opportunity, a wrong pair costs money.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta

from scanner.models import GameState, MarketTeam, PolymarketGame, Side, Team

log = logging.getLogger(__name__)

MAX_KICKOFF_GAP = timedelta(hours=2)

# Polymarket spelling -> ESPN spelling, applied to whole normalized names.
NAME_ALIASES = {
    "nm state": "new mexico state",
    "appalachian state": "app state",
    "umass": "massachusetts",
    "hawaii": "hawai i",
    "miami fl": "miami",
    "miami oh": "miami oh",
    "ole miss": "ole miss",
    "pitt": "pittsburgh",
    "uconn": "uconn",
    "southern mississippi": "southern miss",
    "san jose state": "san jose state",
    "la tech": "louisiana tech",
    "ul monroe": "ul monroe",
    "louisiana monroe": "ul monroe",
    "louisiana lafayette": "louisiana",
    "nc state": "nc state",
    "north carolina state": "nc state",
    "texas san antonio": "utsa",
    "texas el paso": "utep",
    "central florida": "ucf",
    "southern cal": "usc",
    "southern california": "usc",
    "brigham young": "byu",
    "mississippi": "ole miss",
    "washington football team": "washington commanders",
}

_TOKEN_ALIASES = {"st": "state", "saint": "st", "and": "and", "&": "and"}


def normalize(text: str | None) -> str:
    """Lowercase, strip accents and punctuation, expand 'St' to 'State'."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ").replace("-", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [_TOKEN_ALIASES.get(tok, tok) for tok in text.split()]
    joined = " ".join(tokens)
    return NAME_ALIASES.get(joined, joined)


def _abbr(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def team_matches(market_team: MarketTeam, team: Team) -> bool:
    """Whether a Polymarket team and an ESPN team are the same team.

    Matches on equal abbreviations, or on the Polymarket name equalling ESPN's
    location ("Louisiana") or display name ("Washington Commanders"), or on
    Polymarket's name plus nickname equalling ESPN's display name. Never on a
    prefix, so "Miami" cannot pass for "Miami (OH)".
    """
    pm_abbr = _abbr(market_team.abbreviation)
    if pm_abbr and pm_abbr == _abbr(team.abbreviation):
        return True
    pm_name = normalize(market_team.name)
    if not pm_name:
        return False
    espn_names = {normalize(team.name), normalize(team.location)} - {""}
    if pm_name in espn_names:
        return True
    pm_full = normalize(f"{market_team.name} {market_team.nickname}".strip())
    return bool(market_team.nickname) and pm_full in espn_names


@dataclass(frozen=True, slots=True)
class Match:
    polymarket: PolymarketGame
    espn: GameState
    home_team: MarketTeam  # the Polymarket team that is ESPN's home team
    away_team: MarketTeam

    def market_team_for(self, side: Side) -> MarketTeam:
        return self.home_team if side is Side.HOME else self.away_team


@dataclass(slots=True)
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    unmatched_polymarket: list[PolymarketGame] = field(default_factory=list)
    unmatched_espn: list[GameState] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


def _orientation(game: PolymarketGame, state: GameState) -> tuple[MarketTeam, MarketTeam] | None:
    """(home, away) Polymarket teams if both teams match this ESPN game, else None."""
    if len(game.teams) != 2:
        return None
    first, second = game.teams
    if team_matches(first, state.home) and team_matches(second, state.away):
        if team_matches(first, state.away) or team_matches(second, state.home):
            return None  # both orderings fit: cannot tell who is home
        return first, second
    if team_matches(second, state.home) and team_matches(first, state.away):
        return second, first
    return None


def _kickoff_ok(game: PolymarketGame, state: GameState) -> bool:
    if game.start_time is None or state.kickoff is None:
        return False
    return abs(game.start_time - state.kickoff) <= MAX_KICKOFF_GAP


def match_games(games: list[PolymarketGame], states: list[GameState]) -> MatchResult:
    """Pair Polymarket games with ESPN games for one league."""
    result = MatchResult()
    candidates: dict[str, list[tuple[GameState, MarketTeam, MarketTeam]]] = {}
    for game in games:
        for state in states:
            if state.league != game.league or not _kickoff_ok(game, state):
                continue
            orientation = _orientation(game, state)
            if orientation is not None:
                candidates.setdefault(game.event_slug, []).append((state, *orientation))

    # A Polymarket game with several ESPN candidates is ambiguous. So is an ESPN
    # game wanted by several Polymarket games. Neither is paired.
    ambiguous: set[str] = set()
    claims: dict[str, list[str]] = {}
    for slug, found in candidates.items():
        if len(found) > 1:
            ambiguous.add(slug)
            log.warning("%s matches %d ESPN games, left unmatched", slug, len(found))
        else:
            claims.setdefault(found[0][0].feed_id, []).append(slug)
    for feed_id, slugs in claims.items():
        if len(slugs) > 1:
            ambiguous.update(slugs)
            log.warning("ESPN game %s wanted by %s, all left unmatched", feed_id, slugs)

    for game in games:
        found = candidates.get(game.event_slug, [])
        if len(found) == 1 and game.event_slug not in ambiguous:
            state, home_team, away_team = found[0]
            result.matches.append(Match(game, state, home_team, away_team))
        else:
            result.unmatched_polymarket.append(game)
            log.info("unmatched Polymarket game: %s", game.event_slug)
    result.ambiguous = sorted(ambiguous)
    matched_ids = {m.espn.feed_id for m in result.matches}
    result.unmatched_espn = [s for s in states if s.feed_id not in matched_ids]
    return result
