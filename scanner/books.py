"""Sportsbook lines from ESPN's scoreboards, and the vig-free probabilities behind them.

ESPN's scoreboard for every major sport carries one book's line (DraftKings as of
October 2026): a moneyline for each side, a total with over and under prices, a
spread with a price for each side, and a draw price for soccer. A book's two
prices on one market add up to more than 100%; the excess is the book's margin.
Dividing each implied probability by their sum removes it and leaves the book's
own estimate, which is the yardstick the pre-game scan measures Polymarket against.

This module only reads. It knows nothing about alerts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from scanner.models import League, Team

log = logging.getLogger(__name__)

SCOREBOARD_BASE = "https://site.api.espn.com/apis/site/v2/sports"


@dataclass(frozen=True, slots=True)
class Sport:
    """One ESPN scoreboard and the Polymarket league it pairs with."""

    key: str  # "mlb"; also the League value
    path: str  # "baseball/mlb"
    query: str  # extra query, e.g. "groups=80" for FBS only
    polymarket: tuple[str, ...]  # slugs, names or abbreviations Polymarket may use
    two_way_moneyline: bool  # False where a draw is possible (soccer)

    @property
    def league(self) -> League:
        return League(self.key)


SPORTS: dict[str, Sport] = {
    s.key: s
    for s in (
        Sport("mlb", "baseball/mlb", "", ("mlb",), True),
        Sport("nba", "basketball/nba", "", ("nba",), True),
        Sport("wnba", "basketball/wnba", "", ("wnba",), True),
        Sport("cbb", "basketball/mens-college-basketball", "groups=50", ("cbb", "ncaab"), True),
        Sport("nhl", "hockey/nhl", "", ("nhl",), True),
        Sport("nfl", "football/nfl", "", ("nfl",), True),
        Sport("cfb", "football/college-football", "groups=80", ("cfb", "ncaaf"), True),
        Sport("epl", "soccer/eng.1", "", ("epl",), False),
        Sport("mls", "soccer/usa.1", "", ("mls",), False),
        Sport("ucl", "soccer/uefa.champions", "", ("ucl",), False),
        Sport("laliga", "soccer/esp.1", "", ("lal", "laliga", "la liga"), False),
        Sport("bundesliga", "soccer/ger.1", "", ("bun", "bundesliga"), False),
        Sport("seriea", "soccer/ita.1", "", ("sea", "seriea", "serie a"), False),
        Sport("ligue1", "soccer/fra.1", "", ("fl1", "ligue1", "ligue 1"), False),
    )
}


class BookFeedError(Exception):
    """A scoreboard could not be read."""


# -- odds arithmetic -------------------------------------------------------------


def american_to_int(value: Any) -> int | None:
    """'-142' -> -142, '+120' -> 120, 'EVEN' -> 100, 400 -> 400; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value != 0 else None
    if isinstance(value, float):
        return int(value) if value == int(value) and value != 0 else None
    if isinstance(value, str):
        text = value.strip().upper().replace(" ", "")
        if text in ("EVEN", "EV", "PK", "+100", "100"):
            return 100
        if re.fullmatch(r"[+-]?\d+", text):
            number = int(text)
            return number if number != 0 else None
    return None


def implied(odds: int) -> float:
    """The probability a US price implies, margin included."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def fair(*odds: int) -> tuple[float, ...]:
    """Vig-free probabilities for the sides of one market, scaled to add to one."""
    implied_all = [implied(o) for o in odds]
    total = sum(implied_all)
    return tuple(p / total for p in implied_all)


def _signed_line(value: Any) -> float | None:
    """'-1.5' -> -1.5, '+0.5' -> 0.5, 'o2.5'/'u2.5' -> 2.5, 170.5 -> 170.5."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        if text[:1] in ("o", "u"):
            text = text[1:]
        try:
            return float(text)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class BookLine:
    """One book's current line for a game, as ESPN reports it."""

    provider: str
    home_ml: int | None
    away_ml: int | None
    draw_ml: int | None
    total: float | None
    over_odds: int | None
    under_odds: int | None
    home_spread: float | None  # the home team's handicap, signed (-2.5 = favoured by 2.5)
    home_spread_odds: int | None
    away_spread_odds: int | None
    home_ml_open: int | None = None
    away_ml_open: int | None = None

    def fair_moneyline(self) -> tuple[float, float] | None:
        """(home, away) with the margin removed. None for a three-way (draw) market."""
        if self.home_ml is None or self.away_ml is None or self.draw_ml is not None:
            return None
        home, away = fair(self.home_ml, self.away_ml)
        return home, away

    def fair_total(self) -> tuple[float, float] | None:
        """(over, under) at ``total``."""
        if self.total is None or self.over_odds is None or self.under_odds is None:
            return None
        over, under = fair(self.over_odds, self.under_odds)
        return over, under

    def fair_spread(self) -> tuple[float, float] | None:
        """(home covers, away covers) at ``home_spread``."""
        if (
            self.home_spread is None
            or self.home_spread_odds is None
            or self.away_spread_odds is None
        ):
            return None
        home, away = fair(self.home_spread_odds, self.away_spread_odds)
        return home, away

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> BookLine | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(**{k: data.get(k) for k in cls.__slots__})  # type: ignore[arg-type]
        except TypeError:
            return None


def _close(block: Any, key: str = "odds") -> Any:
    """ESPN nests the current price under "close" and the opener under "open"."""
    if not isinstance(block, dict):
        return None
    current = block.get("close")
    if isinstance(current, dict) and current.get(key) is not None:
        return current.get(key)
    return None


def _open(block: Any, key: str = "odds") -> Any:
    if not isinstance(block, dict):
        return None
    opener = block.get("open")
    if isinstance(opener, dict):
        return opener.get(key)
    return None


def parse_book_line(odds_entry: dict[str, Any]) -> BookLine | None:
    """The book's current line from one entry of ESPN's ``competitions[].odds``."""
    provider = odds_entry.get("provider")
    name = provider.get("name") if isinstance(provider, dict) else None
    moneyline = odds_entry.get("moneyline") if isinstance(odds_entry.get("moneyline"), dict) else {}
    home_ml = american_to_int(_close(moneyline.get("home")))
    away_ml = american_to_int(_close(moneyline.get("away")))
    draw = odds_entry.get("drawOdds")
    draw_ml = american_to_int(draw.get("moneyLine")) if isinstance(draw, dict) else None

    total_block = odds_entry.get("total") if isinstance(odds_entry.get("total"), dict) else {}
    over = total_block.get("over")
    under = total_block.get("under")
    total = _signed_line(_close(over, "line"))
    if total is None:
        total = _signed_line(odds_entry.get("overUnder"))
    over_odds = american_to_int(_close(over))
    under_odds = american_to_int(_close(under))

    spread_block = (
        odds_entry.get("pointSpread") if isinstance(odds_entry.get("pointSpread"), dict) else {}
    )
    home_side = spread_block.get("home")
    away_side = spread_block.get("away")
    home_spread = _signed_line(_close(home_side, "line"))
    if home_spread is None:
        home_spread = _signed_line(odds_entry.get("spread"))
    home_spread_odds = american_to_int(_close(home_side))
    away_spread_odds = american_to_int(_close(away_side))
    away_line = _signed_line(_close(away_side, "line"))
    if home_spread is not None and away_line is not None and abs(home_spread + away_line) > 1e-9:
        log.info("spread sides disagree (%s vs %s); spread ignored", home_spread, away_line)
        home_spread = home_spread_odds = away_spread_odds = None

    line = BookLine(
        provider=str(name or "unknown"),
        home_ml=home_ml,
        away_ml=away_ml,
        draw_ml=draw_ml,
        total=total,
        over_odds=over_odds,
        under_odds=under_odds,
        home_spread=home_spread,
        home_spread_odds=home_spread_odds,
        away_spread_odds=away_spread_odds,
        home_ml_open=american_to_int(_open(moneyline.get("home"))),
        away_ml_open=american_to_int(_open(moneyline.get("away"))),
    )
    if line.home_ml is None and line.total is None and line.home_spread is None:
        return None
    return line


# -- the scoreboard, for any sport ------------------------------------------------


@dataclass(frozen=True, slots=True)
class PregameEvent:
    """A game as the scoreboard lists it, with the book's line when one is posted.

    Shaped so that :func:`scanner.matching.match_games` can pair it with a
    Polymarket game: ``league``, ``feed_id``, ``home``, ``away`` and ``kickoff``.
    """

    league: League
    feed_id: str
    home: Team
    away: Team
    kickoff: datetime | None
    status: str  # "pre", "live", "final", "postponed" or "other"
    home_score: int | None
    away_score: int | None
    book: BookLine | None
    fetched_at: datetime


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


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


def _team(competitor: dict[str, Any]) -> Team | None:
    team = competitor.get("team")
    if not isinstance(team, dict):
        return None
    abbreviation = team.get("abbreviation")
    name = team.get("displayName") or team.get("name")
    if not isinstance(abbreviation, str) or not abbreviation.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    location = team.get("location")
    nickname = team.get("name")
    return Team(
        name=name.strip(),
        abbreviation=abbreviation.strip().upper(),
        feed_id=str(team.get("id", competitor.get("id"))),
        location=location.strip() if isinstance(location, str) and location.strip() else None,
        nickname=nickname.strip() if isinstance(nickname, str) and nickname.strip() else None,
    )


def _status(raw: dict[str, Any]) -> str:
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    kind = status.get("type") if isinstance(status.get("type"), dict) else {}
    name = str(kind.get("name") or "")
    if name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_ABANDONED"):
        return "postponed"
    state = str(kind.get("state") or "")
    if state == "pre":
        return "pre"
    if state == "in":
        return "live"
    if state == "post":
        return "final" if kind.get("completed", True) else "other"
    return "other"


def parse_pregame_event(
    raw: dict[str, Any], league: League, fetched_at: datetime
) -> PregameEvent | None:
    feed_id = raw.get("id")
    competitions = raw.get("competitions")
    competition = competitions[0] if isinstance(competitions, list) and competitions else None
    if feed_id is None or not isinstance(competition, dict):
        return None
    home_raw = away_raw = None
    for competitor in competition.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        if competitor.get("homeAway") == "home":
            home_raw = competitor
        elif competitor.get("homeAway") == "away":
            away_raw = competitor
    if home_raw is None or away_raw is None:
        return None
    home, away = _team(home_raw), _team(away_raw)
    if home is None or away is None:
        return None
    book = None
    odds = competition.get("odds")
    if isinstance(odds, list) and odds and isinstance(odds[0], dict):
        book = parse_book_line(odds[0])
    return PregameEvent(
        league=league,
        feed_id=str(feed_id),
        home=home,
        away=away,
        kickoff=_time(competition.get("date") or raw.get("date")),
        status=_status(raw),
        home_score=_int(home_raw.get("score")),
        away_score=_int(away_raw.get("score")),
        book=book,
        fetched_at=fetched_at,
    )


def parse_pregame_scoreboard(raw: Any, league: League, fetched_at: datetime) -> list[PregameEvent]:
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        raise BookFeedError("scoreboard has no events list")
    out = []
    for event in raw["events"]:
        if isinstance(event, dict):
            parsed = parse_pregame_event(event, league, fetched_at)
            if parsed is not None:
                out.append(parsed)
    return out


def scoreboard_url(sport: Sport, date: str) -> str:
    """ESPN wants one day per request, as YYYYMMDD in US Eastern time."""
    query = f"dates={date}&limit=300"
    if sport.query:
        query = f"{sport.query}&{query}"
    return f"{SCOREBOARD_BASE}/{sport.path}/scoreboard?{query}"


class BookFeed:
    """Reads one sport's scoreboard for one day. One request per call."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: float = 12.0,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": "polymarket-scanner/0.1 (read-only)"}
        )
        self._now = now
        self.request_count = 0

    def fetch(self, sport: Sport, date: str) -> list[PregameEvent]:
        self.request_count += 1
        try:
            response = self._client.get(scoreboard_url(sport, date))
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BookFeedError(f"{sport.key} {date}: {type(exc).__name__}: {exc}") from exc
        return parse_pregame_scoreboard(raw, sport.league, self._now())
