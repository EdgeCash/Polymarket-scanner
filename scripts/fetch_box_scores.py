"""Fetch whole seasons of box scores, the history the injury fit is built on.

    python scripts/fetch_box_scores.py --raw DIR [--sports nba nhl nfl] [--seasons 2025]

For each season of each sport, every finished game's ESPN summary is read once
and parsed by the scanner's own parser (``scanner.gamelog.parse_game_summary``),
so the history holds exactly what the running scanner keeps in its diary: both
teams' box scores and every player's line, with who played. One compact record
per game is appended to ``DIR/<sport>_<season>.jsonl``; a rerun skips games
already there, so an interrupted fetch picks up where it stopped.

A season is named by the year it ends in for basketball and hockey (2025 is
2024-25) and by the year it starts in for football, as ESPN does. Only regular
season games are read.

The running scanner never imports this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.books import SCOREBOARD_BASE, SPORTS, scoreboard_url  # noqa: E402
from scanner.gamelog import parse_game_summary, parse_week_scoreboard  # noqa: E402

# Regular season windows, by the season's ESPN year.
DAILY_SEASONS: dict[str, dict[int, tuple[date, date]]] = {
    "nba": {
        2024: (date(2023, 10, 24), date(2024, 4, 14)),
        2025: (date(2024, 10, 22), date(2025, 4, 13)),
        2026: (date(2025, 10, 21), date(2026, 4, 12)),
    },
    "nhl": {
        2024: (date(2023, 10, 10), date(2024, 4, 18)),
        2025: (date(2024, 10, 4), date(2025, 4, 17)),
        2026: (date(2025, 10, 7), date(2026, 4, 16)),
    },
    "cbb": {
        2025: (date(2024, 11, 4), date(2025, 3, 16)),
        2026: (date(2025, 11, 3), date(2026, 3, 15)),
    },
}
WEEKS = {"nfl": 18, "cfb": 15}
GAP_SECONDS = 0.5  # ESPN's edge answers 403 to bursts; a refusal waits and tries again
AGENT = {"User-Agent": "polymarket-scanner/0.1 (read-only)"}  # the scanner's own


class Client:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=60, headers=AGENT)
        self.requests = 0

    def get(self, url: str, tries: int = 6):
        for attempt in range(tries):
            if self.requests:
                time.sleep(GAP_SECONDS)
            self.requests += 1
            try:
                response = self._client.get(url)
                if response.status_code in (403, 429):
                    time.sleep(20 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == tries - 1:
                    print(f"  failed {url}: {exc}", file=sys.stderr)
                    return None
                time.sleep(5 * (attempt + 1))
        return None


def record_dict(record) -> dict:
    return {
        "sport": record.sport,
        "game_id": record.game_id,
        "season": record.season,
        "week": record.week,
        "date": record.date.isoformat(),
        "neutral": record.neutral,
        "home_id": record.home.team_id,
        "home_abbr": record.home.abbreviation,
        "away_id": record.away.team_id,
        "away_abbr": record.away.abbreviation,
        "home_score": record.home_score,
        "away_score": record.away_score,
        "home_lines": list(record.home_lines),
        "away_lines": list(record.away_lines),
        "home_stats": record.home_stats,
        "away_stats": record.away_stats,
        "book": record.book.as_dict() if record.book else None,
        "conference_game": record.conference_game,
        "players": [
            {
                "team_id": p.team_id,
                "player_id": p.player_id,
                "name": p.name,
                "position": p.position,
                "starter": p.starter,
                "played": p.played,
                "stats": p.stats,
            }
            for p in record.players
        ],
    }


def scoreboards(client: Client, sport: str, season: int):
    """Every scoreboard of a season: by day for basketball and hockey, by week for football."""
    if sport in DAILY_SEASONS:
        first, last = DAILY_SEASONS[sport][season]
        day = first
        while day <= last:
            raw = client.get(scoreboard_url(SPORTS[sport], day.strftime("%Y%m%d")))
            if raw is not None:
                yield raw
            day += timedelta(days=1)
    else:
        for week in range(1, WEEKS[sport] + 1):
            query = f"dates={season}&seasontype=2&week={week}&limit=300"
            if SPORTS[sport].query:
                query = f"{SPORTS[sport].query}&{query}"
            raw = client.get(f"{SCOREBOARD_BASE}/{SPORTS[sport].path}/scoreboard?{query}")
            if raw is not None:
                yield raw


def fetch_season(client: Client, sport: str, season: int, out: Path) -> None:
    path = out / f"{sport}_{season}.jsonl"
    done: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["game_id"])
                except (ValueError, KeyError):
                    continue
    print(f"{sport} {season}: {len(done)} games already fetched", flush=True)
    stored = 0
    with open(path, "a", encoding="utf-8") as fh:
        for raw in scoreboards(client, sport, season):
            _, _, games = parse_week_scoreboard(raw, sport)
            for game in games:
                if not game.completed or game.game_id in done:
                    continue
                summary = client.get(
                    f"{SCOREBOARD_BASE}/{SPORTS[sport].path}/summary?event={game.game_id}"
                )
                if summary is None:
                    continue
                record = parse_game_summary(summary, sport)
                if record is None:
                    continue
                if record.season is None:
                    record = type(record)(**{**record.__dict__, "season": season})
                fh.write(json.dumps(record_dict(record)) + "\n")
                fh.flush()
                done.add(game.game_id)
                stored += 1
                if stored % 50 == 0:
                    print(
                        f"  {sport} {season}: {stored} new, {client.requests} requests", flush=True
                    )
    print(f"{sport} {season}: {stored} new games, {len(done)} in all", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True, help="where the records are kept")
    parser.add_argument("--sports", nargs="+", default=["nba", "nhl", "nfl"])
    parser.add_argument("--seasons", nargs="+", type=int, default=[2025])
    args = parser.parse_args()
    args.raw.mkdir(parents=True, exist_ok=True)
    client = Client()
    for season in args.seasons:
        for sport in args.sports:
            if sport in DAILY_SEASONS and season not in DAILY_SEASONS[sport]:
                print(f"{sport} {season}: no window known, skipped")
                continue
            fetch_season(client, sport, season, args.raw)


if __name__ == "__main__":
    main()
