"""Test what a rating should be made of, on seasons of box scores.

    python scripts/fit_ratings.py --raw DIR --sports nfl nba nhl [--only luck cap decay]

The history is the seasons scripts/fetch_box_scores.py gathered. Every game is
projected by the scanner's own ``projection.fit`` and ``project`` from the season's
earlier days, with the season before as its prior where the history has one, exactly
as the running scanner would have seen it; only the sport's tuning is changed, one
dial at a time, and each setting is scored by the root-mean-square miss of the
margin and of the total:

- ``home``: the home edge inside a conference and outside one (college);
- ``luck``: the points one lost fumble or interception is taken to be worth, removed
  from the day's score before it is rated (football only; a recovered fumble does
  not carry to next week);
- ``cap``: the widest margin one game may count for;
- ``decay``: the half-life, in a team's own games, of an old game's weight.

Each dial is scored on the earlier seasons and again on the last, which chose
nothing: a setting ships only when it wins on both. The running scanner never
imports this script.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_injuries  # noqa: E402

import scanner.projection as projection  # noqa: E402
from scanner.gamelog import GameRecord  # noqa: E402
from scanner.rest import days_since_last  # noqa: E402

HOME_EDGES = {
    "cbb": (
        (None, None),
        (3.0, 6.3),
        (3.0, 5.5),
        (2.8, 7.0),
        (3.3, 5.0),
        (3.0, 4.8),
        (3.5, 5.5),
        (3.2, 5.2),
    ),  # fmt: skip
    "cfb": ((None, None), (2.2, 4.5), (2.5, 4.5), (2.0, 5.5), (2.5, 3.5)),
}
GRIDS: dict[str, dict[str, list[dict]]] = {
    "home": {
        sport: [{"hfa_conference": c, "hfa_nonconference": n} for c, n in grid]
        for sport, grid in HOME_EDGES.items()
    },
    "luck": {
        "nfl": [
            {"fumble_points": f, "interception_points": i}
            for f, i in ((0, 0), (2, 0), (4, 0), (4, 2), (4, 4), (2, 2), (6, 3))
        ],
        "cfb": [
            {"fumble_points": f, "interception_points": i}
            for f, i in ((0, 0), (2, 0), (4, 0), (4, 2), (4, 4), (2, 2), (6, 3))
        ],
    },
    "cap": {
        "nfl": [{"margin_cap": c} for c in (None, 35, 28, 21, 17)],
        "cfb": [{"margin_cap": c} for c in (None, 42, 35, 28, 21)],
        "nba": [{"margin_cap": c} for c in (None, 35, 28, 22, 18)],
        "cbb": [{"margin_cap": c} for c in (None, 35, 28, 22, 18)],
        "nhl": [{"margin_cap": c} for c in (None, 5, 4, 3)],
    },
    "decay": {
        sport: [{"half_life": h} for h in grid]
        for sport, grid in {
            "nfl": (None, 16, 10, 6),
            "cfb": (None, 12, 8, 5),
            "nba": (None, 40, 25, 15),
            "cbb": (None, 30, 20, 12),
            "nhl": (None, 60, 40, 25, 15),
        }.items()
    },
}


def final_prior(records: list[GameRecord], sport: str, season) -> dict:
    """A season's final ratings in the shape of scanner/priors.json: points only, toward
    average, as scripts/build_priors.py makes them."""
    saved = projection.TUNING[sport]
    projection.TUNING[sport] = replace(
        saved,
        volume_stat=None,
        prior_weight=0.0,
        pseudo_games=projection.PSEUDO_GAMES,
        fumble_points=0.0,
        interception_points=0.0,
        margin_cap=None,
        half_life=None,
    )
    try:
        ratings = projection.fit(records, sport)
    finally:
        projection.TUNING[sport] = saved
    teams = {
        team: {
            "offense": ratings.offense[team],
            "defense": ratings.defense[team],
            "name": team,
            "games": ratings.games.get(team, 0),
        }
        for team in ratings.offense
        if ratings.games.get(team, 0) >= projection.MIN_GAMES
    }
    nets = sorted(v["offense"] - v["defense"] for v in teams.values())
    low = nets[len(nets) // 10] if nets and sport in ("cfb", "cbb") else 0.0
    return {
        "season": season,
        "teams": teams,
        "unknown": {"offense": low / 2.0, "defense": -low / 2.0},
    }


def walk(
    sport: str, records: list[GameRecord], overrides: dict, priors: dict | None = None
) -> list[dict]:
    """Every rated game's miss under one setting of the sport's tuning.

    ``priors`` gives each season's prior outright (by season); without it a season
    starts from the final ratings of the one before, as scripts/build_priors.py makes them.
    """
    saved = projection.TUNING[sport]
    by_season: dict = defaultdict(list)
    for record in records:
        by_season[record.season].append(record)
    out: list[dict] = []
    prior = None
    try:
        for season in sorted(by_season, key=lambda s: s or 0):
            games = by_season[season]
            schedule: dict[str, list[datetime]] = defaultdict(list)
            for g in games:
                schedule[g.home.team_id].append(g.date)
                schedule[g.away.team_id].append(g.date)
            earlier: list[GameRecord] = []
            ratings, rated_through = None, None
            projection.TUNING[sport] = replace(saved, **overrides)
            if priors is not None:
                prior = priors.get(season)
            for index, record in enumerate(games):
                day = record.date.date()
                if rated_through != day:
                    ratings = projection.fit(earlier, sport, prior)
                    rated_through = day
                home = ratings.team(record.home.team_id)
                away = ratings.team(record.away.team_id)
                if (
                    home is not None
                    and away is not None
                    and min(home["games"], away["games"]) >= projection.MIN_GAMES
                ):
                    rest = {
                        "home": days_since_last(record.date, schedule[record.home.team_id]),
                        "away": days_since_last(record.date, schedule[record.away.team_id]),
                    }
                    p = projection.project(
                        ratings,
                        record.home.team_id,
                        record.away.team_id,
                        record.neutral,
                        None,
                        {"rest": rest, "conference_game": record.conference_game},
                    )
                    if p is not None:
                        out.append(
                            {
                                "season": season,
                                "game_id": record.game_id,
                                "index": index / len(games),
                                "day": (record.date - games[0].date).days,
                                "margin": record.home_score
                                - record.away_score
                                - p["raw"]["margin"],
                                "total": record.home_score + record.away_score - p["raw"]["total"],
                            }
                        )
                earlier.append(record)
            projection.TUNING[sport] = saved
            prior = final_prior(games, sport, season)
    finally:
        projection.TUNING[sport] = saved
    return out


def rms(rows: list[dict], key: str) -> float:
    return math.sqrt(sum(r[key] ** 2 for r in rows) / len(rows)) if rows else float("nan")


def label(overrides: dict) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in overrides.items())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["nfl", "nba", "nhl"])
    parser.add_argument("--only", nargs="+", default=list(GRIDS))
    parser.add_argument("--pick", type=int, default=None, help="run one setting of the grid")
    args = parser.parse_args()
    for sport in args.sports:
        records = fit_injuries.load(args.raw, sport)
        seasons = sorted({r.season for r in records}, key=lambda s: s or 0)
        if len(seasons) < 2:
            print(f"{sport}: needs two seasons in {args.raw}, has {seasons}")
            continue
        last = seasons[-1]
        print(f"\n{sport}: {len(records)} games, seasons {seasons}; the last chose nothing")
        for dial in args.only:
            grid = GRIDS[dial].get(sport)
            if not grid:
                continue
            print(f"  {dial}")
            if args.pick is not None:
                grid = grid[args.pick : args.pick + 1]
            for overrides in grid:
                rows = walk(sport, records, overrides)
                before = [r for r in rows if r["season"] != last]
                after = [r for r in rows if r["season"] == last]
                print(
                    f"    {label(overrides):44} margin {rms(before, 'margin'):.3f} / "
                    f"{rms(after, 'margin'):.3f}   total {rms(before, 'total'):.3f} / "
                    f"{rms(after, 'total'):.3f}   ({len(before)} / {len(after)} games)",
                    flush=True,
                )


if __name__ == "__main__":
    main()
