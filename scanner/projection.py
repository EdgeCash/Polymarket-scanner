"""A projection model built from the game log, graded against the book.

What it does, in one breath: for every team, half of what it scored and half of
what its volume says it should have scored (yards in football, shots in hockey;
basketball uses points alone), each adjusted for who it played, shrunk toward
average while games are few, plus home advantage. Two ratings per team come out
(points above average on offence and points above average allowed on defence);
a game's projection is the league average plus the two ratings that meet, and
the win probability is the projected margin against the usual spread of results
in that sport. TUNING holds what differs between sports.

The diary records every projection and grades it against the book's line, so the
scorecard can say whether the model beats the book before anyone acts on it.
Nothing here is a recommendation and nothing here places anything.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import NormalDist

from scanner.books import BookLine
from scanner.gamelog import GameRecord

MODEL_VERSION = "v1"
ITERATIONS = 30  # rounds of opponent adjustment
PSEUDO_GAMES = 4.0  # shrinkage: a rating is its residual sum over (games + this)
BOOK_PSEUDO_GAMES = 6.0  # blend: the model's weight is games / (games + this)
VOLUME_SHARE = 0.5  # half of a performance is points, half volume-based points
MIN_GAMES = 3  # below this the projection is marked thin
GAP_BUCKETS = (1.0, 2.0, 3.0, 5.0)  # the scorecard's "model differs from the book by" rows
MONEYLINE_CLAMP = 0.02  # a book win probability is kept this far from 0 and 1 before inverting


@dataclass(frozen=True, slots=True)
class SportTuning:
    hfa: float  # home advantage, in points (goals) of margin
    sigma: float  # standard deviation of the final margin around the projection
    sigma_total: float  # the same for the final total
    volume_stat: str | None = None  # the box-score stat whose league rate makes the volume half
    first_half_share: float = 0.48  # used until the log has first-half (first-period) points
    margin_from_moneyline: bool = False  # read the book's margin off its moneyline, not its spread
    buckets: tuple[float, ...] = GAP_BUCKETS  # the scorecard's gap rows, in this sport's unit


TUNING = {
    "nfl": SportTuning(hfa=1.8, sigma=13.5, sigma_total=10.5, volume_stat="total_yards"),
    "cfb": SportTuning(hfa=2.8, sigma=16.5, sigma_total=13.0, volume_stat="total_yards"),
    # Basketball: points alone, and a home edge that has shrunk to a few points.
    "nba": SportTuning(hfa=2.5, sigma=12.5, sigma_total=19.0, first_half_share=0.5),
    # Hockey: half goals, half what the shots say. The book's puck line sits at 1.5
    # whatever the matchup, so its margin is read off the moneyline instead; the
    # "first half" is the first period; gaps are in goals, so the buckets are finer.
    "nhl": SportTuning(
        hfa=0.2,
        sigma=2.3,
        sigma_total=2.4,
        volume_stat="shots",
        first_half_share=0.31,
        margin_from_moneyline=True,
        buckets=(0.25, 0.5, 0.75, 1.0),
    ),
}


def tuning_for(sport: str | None) -> SportTuning:
    return TUNING.get(sport or "", TUNING["cfb"])


@dataclass
class Ratings:
    """One sport's fitted ratings for a season."""

    sport: str
    season: int | None
    league_points: float  # average points per team-game
    points_per_volume: float | None  # league points per unit of the volume stat
    first_half_share: float
    hfa: float
    sigma: float
    sigma_total: float
    games_fitted: int
    offense: dict[str, float] = field(default_factory=dict)  # points above average scored
    defense: dict[str, float] = field(default_factory=dict)  # points above average allowed
    games: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def team(self, team_id: str) -> dict | None:
        if team_id not in self.games:
            return None
        return {
            "offense": self.offense[team_id],
            "defense": self.defense[team_id],
            "games": self.games[team_id],
        }


def win_probability(margin: float, sigma: float) -> float:
    """The chance the home side wins when the final margin is normal around ``margin``."""
    return 0.5 * (1.0 + math.erf(margin / (sigma * math.sqrt(2.0))))


def book_margin(book: BookLine | None, sport: str) -> float | None:
    """The home margin the book expects: its spread, or in hockey what its moneyline says."""
    if book is None:
        return None
    tuning = tuning_for(sport)
    if not tuning.margin_from_moneyline:
        return None if book.home_spread is None else -book.home_spread
    fair = book.fair_moneyline()
    if fair is None:
        return None
    p_home = min(max(fair[0], MONEYLINE_CLAMP), 1.0 - MONEYLINE_CLAMP)
    return tuning.sigma * NormalDist().inv_cdf(p_home)


def fit(records: list[GameRecord], sport: str) -> Ratings:
    """Fit offence and defence ratings from finished games (any number, even none)."""
    tuning = tuning_for(sport)
    season = records[-1].season if records else None
    points_sum = volume_sum = 0.0
    half_sum = half_total = 0.0
    for record in records:
        for score, stats in (
            (record.home_score, record.home_stats),
            (record.away_score, record.away_stats),
        ):
            volume = stats.get(tuning.volume_stat) if tuning.volume_stat else None
            if isinstance(volume, int | float) and volume > 0:
                points_sum += score
                volume_sum += volume
            half = stats.get("first_half_points")
            if isinstance(half, int | float):
                half_sum += half
                half_total += score
    points_per_volume = points_sum / volume_sum if volume_sum > 0 else None
    first_half_share = half_sum / half_total if half_total > 0 else tuning.first_half_share

    # One entry per side of each game: (team, opponent, offence, defence, home term).
    entries: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
    names: dict[str, str] = {}
    performances: list[float] = []
    for record in records:
        perf = {}
        for side, score, stats in (
            ("home", record.home_score, record.home_stats),
            ("away", record.away_score, record.away_stats),
        ):
            volume = stats.get(tuning.volume_stat) if tuning.volume_stat else None
            if points_per_volume is not None and isinstance(volume, int | float) and volume > 0:
                perf[side] = (1 - VOLUME_SHARE) * score + VOLUME_SHARE * points_per_volume * volume
            else:
                perf[side] = float(score)
        h = 0.0 if record.neutral else tuning.hfa / 2.0
        home, away = record.home.team_id, record.away.team_id
        names[home], names[away] = record.home.name, record.away.name
        entries[home].append((away, perf["home"], perf["away"], h))
        entries[away].append((home, perf["away"], perf["home"], -h))
        performances.extend((perf["home"], perf["away"]))
    league = sum(performances) / len(performances) if performances else 0.0

    offense = dict.fromkeys(entries, 0.0)
    defense = dict.fromkeys(entries, 0.0)
    for _ in range(ITERATIONS):
        new_offense = {}
        for team, games in entries.items():
            # What the team scored, less the average, less how soft its opponents
            # were and less its home edge, over its games plus the pseudo-games.
            residual = sum(off - league - defense[opp] - h for opp, off, _, h in games)
            new_offense[team] = residual / (len(games) + PSEUDO_GAMES)
        new_defense = {}
        for team, games in entries.items():
            residual = sum(dfn - league - new_offense[opp] + h for opp, _, dfn, h in games)
            new_defense[team] = residual / (len(games) + PSEUDO_GAMES)
        offense, defense = new_offense, new_defense

    return Ratings(
        sport=sport,
        season=season,
        league_points=league,
        points_per_volume=points_per_volume,
        first_half_share=first_half_share,
        hfa=tuning.hfa,
        sigma=tuning.sigma,
        sigma_total=tuning.sigma_total,
        games_fitted=len(records),
        offense=offense,
        defense=defense,
        games={team: len(games) for team, games in entries.items()},
        names=names,
    )


def _book_parts(book: BookLine | None, sport: str) -> dict | None:
    if book is None:
        return None
    fair = book.fair_moneyline()
    parts = {
        "margin": book_margin(book, sport),
        "total": book.total,
        "home_win": None if fair is None else fair[0],
    }
    return parts if any(v is not None for v in parts.values()) else None


def project(
    ratings: Ratings, home_id: str, away_id: str, neutral: bool, book: BookLine | None
) -> dict | None:
    """One game's projection, raw and blended with the book, or None without ratings."""
    home, away = ratings.team(home_id), ratings.team(away_id)
    if home is None or away is None:
        return None
    h = 0.0 if neutral else ratings.hfa / 2.0
    home_points = ratings.league_points + home["offense"] + away["defense"] + h
    away_points = ratings.league_points + away["offense"] + home["defense"] - h
    margin = home_points - away_points
    total = home_points + away_points
    games_used = min(home["games"], away["games"])
    weight = games_used / (games_used + BOOK_PSEUDO_GAMES)
    raw = {
        "home": home_points,
        "away": away_points,
        "margin": margin,
        "total": total,
        "home_win": win_probability(margin, ratings.sigma),
        "first_half": {
            "home": home_points * ratings.first_half_share,
            "away": away_points * ratings.first_half_share,
        },
    }
    book_parts = _book_parts(book, ratings.sport)

    def blend_of(key: str) -> float:
        book_value = book_parts.get(key) if book_parts else None
        return raw[key] if book_value is None else weight * raw[key] + (1 - weight) * book_value

    blend_margin, blend_total = blend_of("margin"), blend_of("total")
    blend = {
        "weight": weight,
        "home": (blend_total + blend_margin) / 2.0,
        "away": (blend_total - blend_margin) / 2.0,
        "margin": blend_margin,
        "total": blend_total,
        "home_win": blend_of("home_win"),
    }
    gap = None
    if book_parts:
        gap = {
            key: None if book_parts.get(key) is None else raw[key] - book_parts[key]
            for key in ("margin", "total", "home_win")
        }
    return {
        "model": MODEL_VERSION,
        "sport": ratings.sport,
        "games_used": {"home": home["games"], "away": away["games"]},
        "thin": games_used < MIN_GAMES,
        "ratings": {
            "home": {"offense": home["offense"], "defense": home["defense"]},
            "away": {"offense": away["offense"], "defense": away["defense"]},
        },
        "league": {
            "points": ratings.league_points,
            "hfa": ratings.hfa,
            "sigma": ratings.sigma,
            "sigma_total": ratings.sigma_total,
            "points_per_volume": ratings.points_per_volume,
            "first_half_share": ratings.first_half_share,
            "games": ratings.games_fitted,
        },
        "neutral": bool(neutral),
        "raw": raw,
        "blend": blend,
        "book": book_parts,
        "gap": gap,
    }


# -- grading --------------------------------------------------------------------------


def _pick(
    model_value: float | None, line: float | None, actual: float, high: str, low: str
) -> dict | None:
    """Which side the model takes against a line, and how that side did."""
    if model_value is None or line is None:
        return None
    gap = model_value - line
    if gap == 0:
        return None
    side = high if gap > 0 else low
    if actual == line:
        result = "push"
    else:
        result = "win" if (actual > line) == (side == high) else "loss"
    return {"side": side, "gap": abs(gap), "result": result}


def grade(projection: dict, final_home: int, final_away: int, close: BookLine | None) -> dict:
    """How a projection did against the final score, the book's line and the close."""
    actual_margin = float(final_home - final_away)
    actual_total = float(final_home + final_away)
    outcome = 1.0 if actual_margin > 0 else 0.0 if actual_margin < 0 else 0.5
    parts = {
        "raw": projection.get("raw") or {},
        "blend": projection.get("blend") or {},
        "book": projection.get("book") or {},
    }

    def error(key: str, actual: float) -> dict:
        return {
            name: None if part.get(key) is None else abs(part[key] - actual)
            for name, part in parts.items()
        }

    brier = {
        name: None if part.get("home_win") is None else (part["home_win"] - outcome) ** 2
        for name, part in parts.items()
    }
    raw, book = parts["raw"], parts["book"]
    ats = _pick(raw.get("margin"), book.get("margin"), actual_margin, "home", "away")
    ou = _pick(raw.get("total"), book.get("total"), actual_total, "over", "under")
    clv = None
    close_parts = _book_parts(close, str(projection.get("sport") or ""))
    if close_parts:
        clv = {"margin": None, "total": None}
        if ats and close_parts.get("margin") is not None:
            moved = close_parts["margin"] - book["margin"]
            clv["margin"] = moved if ats["side"] == "home" else -moved
        if ou and close_parts.get("total") is not None:
            moved = close_parts["total"] - book["total"]
            clv["total"] = moved if ou["side"] == "over" else -moved
    return {
        "final": {"home": final_home, "away": final_away},
        "margin_error": error("margin", actual_margin),
        "total_error": error("total", actual_total),
        "brier": brier,
        "ats": ats,
        "ou": ou,
        "clv": clv,
    }


def _mean(values: list) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _record(picks: list[dict], at_least: float) -> dict:
    chosen = [p for p in picks if p["gap"] >= at_least]
    return {
        "wins": sum(1 for p in chosen if p["result"] == "win"),
        "losses": sum(1 for p in chosen if p["result"] == "loss"),
        "pushes": sum(1 for p in chosen if p["result"] == "push"),
    }


def summarise(grades: list[dict], sport: str | None = None) -> dict:
    """The scorecard's numbers over a set of graded projections.

    The gap rows use the sport's own buckets (points, or quarter goals in hockey),
    so a summary across sports keeps only the misses, Brier scores and line moves.
    """
    sources = ("raw", "blend", "book")
    buckets = tuning_for(sport).buckets if sport else GAP_BUCKETS
    ats = [g["ats"] for g in grades if g.get("ats")]
    ou = [g["ou"] for g in grades if g.get("ou")]
    clv_margin = [(g.get("clv") or {}).get("margin") for g in grades]
    clv_total = [(g.get("clv") or {}).get("total") for g in grades]
    closed = [v for v in clv_margin if v is not None]
    return {
        "graded": len(grades),
        "buckets": [f"{b:g}" for b in buckets],
        "margin_error": {s: _mean([g["margin_error"].get(s) for g in grades]) for s in sources},
        "total_error": {s: _mean([g["total_error"].get(s) for g in grades]) for s in sources},
        "brier": {s: _mean([g["brier"].get(s) for g in grades]) for s in sources},
        "ats": {f"{b:g}": _record(ats, b) for b in buckets},
        "ou": {f"{b:g}": _record(ou, b) for b in buckets},
        "clv": {
            "margin": _mean(clv_margin),
            "total": _mean(clv_total),
            "closed": len(closed),
            "positive_share": (sum(1 for v in closed if v > 0) / len(closed)) if closed else None,
        },
    }
