"""Seasons of basketball box scores in bulk, for the fits that want many of them.

    python scripts/bulk_box_scores.py --raw DIR [--sports cbb nba] [--seasons 2024 2025 2026]

scripts/fetch_box_scores.py reads ESPN one game at a time, which is right for a
season or two and slow for more: a college basketball season is six thousand
games. The sportsdataverse project publishes the same ESPN box scores as season
files (schedules, team lines and player lines, keyed by ESPN's own game, team and
athlete ids), and this script turns those into the same ``<sport>_<season>.jsonl``
records, so every fit script reads either source. Each record also carries the
two conferences and the venue's city and state, which the college fits use.

A season is named by the year it ends in. The downloaded files are kept in
``DIR/bulk`` so a rerun need not fetch them again. The running scanner never
imports this script; it needs the ``train`` extras and pyarrow.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
SOURCES = {
    "cbb": ("espn_mens_college_basketball", "mbb"),
    "nba": ("espn_nba", "nba"),
}
AGENT = {"User-Agent": "polymarket-scanner/0.1 (read-only)"}


def _download(url: str, path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    request = urllib.request.Request(url, headers=AGENT)
    with urllib.request.urlopen(request, timeout=300) as response:
        path.write_bytes(response.read())


def _lines(text) -> list[int]:
    """Period scores from the schedule's line-score column, which is a printed list."""
    return [int(float(v)) for v in re.findall(r"'value': ([0-9.]+)", str(text or ""))]


def _int(value) -> int | None:
    try:
        return None if pd.isna(value) else int(float(value))
    except (TypeError, ValueError):
        return None


def _text(value) -> str | None:
    return None if value is None or pd.isna(value) else str(value)


def team_stats(row, score: int, lines: list[int], half: int) -> dict:
    fg_att, off_reb = _int(row.field_goals_attempted), _int(row.offensive_rebounds)
    turnovers = _int(row.total_turnovers)
    if turnovers is None:
        turnovers = _int(row.turnovers)
    ft_att = _int(row.free_throws_attempted)
    possessions = None
    if None not in (fg_att, off_reb, turnovers, ft_att):
        possessions = fg_att - off_reb + turnovers + 0.44 * ft_att
    return {
        "fg_made": _int(row.field_goals_made),
        "fg_att": fg_att,
        "three_made": _int(row.three_point_field_goals_made),
        "three_att": _int(row.three_point_field_goals_attempted),
        "ft_made": _int(row.free_throws_made),
        "ft_att": ft_att,
        "off_reb": off_reb,
        "def_reb": _int(row.defensive_rebounds),
        "rebounds": _int(row.total_rebounds),
        "assists": _int(row.assists),
        "steals": _int(row.steals),
        "blocks": _int(row.blocks),
        "turnovers": turnovers,
        "fouls": _int(row.fouls),
        "possessions": possessions,
        "points": score,
        "first_half_points": sum(lines[:half]) if len(lines) >= half else None,
    }


def convert(sport: str, season: int, bulk: Path, out: Path) -> int:
    family, short = SOURCES[sport]
    files = {
        "sched": f"{BASE}/{family}_schedules/{short}_schedule_{season}.parquet",
        "team": f"{BASE}/{family}_team_boxscores/team_box_{season}.parquet",
        "player": f"{BASE}/{family}_player_boxscores/player_box_{season}.parquet",
    }
    frames = {}
    for kind, url in files.items():
        path = bulk / f"{sport}_{kind}_{season}.parquet"
        _download(url, path)
        frames[kind] = pd.read_parquet(path)
    sched = frames["sched"]
    sched = sched[sched.status_type_completed.fillna(False).astype(bool)]
    team_rows = {(int(r.game_id), int(r.team_id)): r for r in frames["team"].itertuples()}
    players: dict[int, list] = {}
    for r in frames["player"].itertuples():
        players.setdefault(int(r.game_id), []).append(r)
    half = 2 if sport == "nba" else 1  # quarters, or halves
    written = 0
    with open(out / f"{sport}_{season}.jsonl", "w", encoding="utf-8") as fh:
        for g in sched.sort_values("date").itertuples():
            game_id = int(g.game_id)
            home_id, away_id = _int(g.home_id), _int(g.away_id)
            home_score, away_score = _int(g.home_score), _int(g.away_score)
            home_box = team_rows.get((game_id, home_id))
            away_box = team_rows.get((game_id, away_id))
            if None in (home_id, away_id, home_score, away_score) or home_box is None:
                continue
            if away_box is None or home_score == away_score:
                continue
            home_lines = _lines(getattr(g, "home_linescores", None))
            away_lines = _lines(getattr(g, "away_linescores", None))
            lines = []
            for p in players.get(game_id, []):
                minutes = _int(p.minutes)
                played = not bool(p.did_not_play) and bool(minutes)
                stats = {}
                if played:
                    stats = {
                        "minutes": minutes,
                        "points": _int(p.points),
                        "fg_made": _int(p.field_goals_made),
                        "fg_att": _int(p.field_goals_attempted),
                        "three_made": _int(p.three_point_field_goals_made),
                        "three_att": _int(p.three_point_field_goals_attempted),
                        "ft_made": _int(p.free_throws_made),
                        "ft_att": _int(p.free_throws_attempted),
                        "rebounds": _int(p.rebounds),
                        "off_reb": _int(p.offensive_rebounds),
                        "assists": _int(p.assists),
                        "turnovers": _int(p.turnovers),
                        "steals": _int(p.steals),
                        "blocks": _int(p.blocks),
                        "fouls": _int(p.fouls),
                    }
                position = p.athlete_position_abbreviation
                lines.append(
                    {
                        "team_id": str(_int(p.team_id)),
                        "player_id": str(_int(p.athlete_id)),
                        "name": str(p.athlete_display_name or ""),
                        "position": None if pd.isna(position) else str(position),
                        "starter": bool(p.starter) if not pd.isna(p.starter) else None,
                        "played": played,
                        "stats": stats,
                    }
                )
            record = {
                "sport": sport,
                "game_id": str(game_id),
                "season": season,
                "week": None,
                "date": str(g.date).replace("Z", "+00:00"),
                "neutral": bool(g.neutral_site),
                "home_id": str(home_id),
                "home_abbr": str(g.home_abbreviation),
                "away_id": str(away_id),
                "away_abbr": str(g.away_abbreviation),
                "home_score": home_score,
                "away_score": away_score,
                "home_lines": home_lines,
                "away_lines": away_lines,
                "home_stats": team_stats(home_box, home_score, home_lines, half),
                "away_stats": team_stats(away_box, away_score, away_lines, half),
                "book": None,
                "players": lines,
                "season_type": _int(getattr(g, "season_type", None)),
                "home_conference": _int(getattr(g, "home_conference_id", None)),
                "away_conference": _int(getattr(g, "away_conference_id", None)),
                "venue_city": _text(getattr(g, "venue_address_city", None)),
                "venue_state": _text(getattr(g, "venue_address_state", None)),
            }
            fh.write(json.dumps(record) + "\n")
            written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["cbb", "nba"])
    parser.add_argument("--seasons", nargs="+", type=int, default=[2024, 2025, 2026])
    args = parser.parse_args()
    bulk = args.raw / "bulk"
    bulk.mkdir(parents=True, exist_ok=True)
    for sport in args.sports:
        for season in args.seasons:
            try:
                count = convert(sport, season, bulk, args.raw)
            except Exception as exc:  # noqa: BLE001 - a missing season is reported, not fatal
                print(f"{sport} {season}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            print(f"{sport} {season}: {count} games", flush=True)


if __name__ == "__main__":
    main()
