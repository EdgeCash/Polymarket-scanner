"""Grade the player-line model on a sample of box scores, and refit its dispersions.

    python scripts/fit_players.py --raw DIR --sports nba cbb nhl nfl cfb [--fetch]

``--raw`` holds one ``<sport>.jsonl`` a sport: finished games with every player's
line as ``scanner.gamelog.parse_players`` reads it from ESPN's summary (``--fetch``
builds them from the scoreboards in ``--raw`` written by ``fetch_history.py``'s
layout: ``<sport>_games.jsonl`` with id, date, season, neutral, home, away and the
scores). The model is walked forward a day at a time exactly as the scanner runs
it (ratings and lines from the days before, nothing from the day itself), every
line is graded, and the scorecard's numbers come out stat by stat: the miss, the
bias, the share above the median, the share inside the 80% band and the PIT
deciles. Then each stat's negative-binomial dispersion is refit by maximum
likelihood over a grid, so ``players.DISPERSION_BY_SPORT`` can be set from the
data rather than by hand.

The running scanner never imports this script.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner import players as P  # noqa: E402
from scanner.gamelog import GameRecord, TeamRef, parse_players  # noqa: E402
from scanner.projection import fit, project  # noqa: E402

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={game_id}"
PATHS = {
    "nba": "basketball/nba",
    "cbb": "basketball/mens-college-basketball",
    "nhl": "hockey/nhl",
    "nfl": "football/nfl",
    "cfb": "football/college-football",
}
GRID = (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 14.0, 20.0, 30.0, 50.0)
WARMUP_DAYS = 7


def fetch(raw: Path, sport: str) -> None:
    import urllib.request

    source, target = raw / f"{sport}_games.jsonl", raw / f"{sport}.jsonl"
    done = set()
    if target.exists():
        done = {json.loads(line)["id"] for line in open(target)}
    with open(target, "a") as out:
        for line in open(source):
            g = json.loads(line)
            if g["id"] in done:
                continue
            request = urllib.request.Request(
                SUMMARY.format(path=PATHS[sport], game_id=g["id"]),
                headers={"User-Agent": "Mozilla/5.0 (research script)"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read())
            players = parse_players(payload, sport, str(g["home"]), str(g["away"]))
            out.write(json.dumps({**g, "players": [dataclasses.asdict(p) for p in players]}) + "\n")
            time.sleep(0.1)


def load(raw: Path, sport: str) -> list[dict]:
    games = [json.loads(line) for line in open(raw / f"{sport}.jsonl")]
    games.sort(key=lambda g: g["date"])
    return games


def walk(games: list[dict], sport: str) -> list[tuple[dict, int, dict]]:
    """Every graded line: (the line as projected, the actual, its grade)."""
    graded = []
    days = sorted({g["date"] for g in games})
    for day in days[WARMUP_DAYS:]:
        rows, records = [], []
        for g in games:
            if g["date"] >= day:
                break
            when = datetime.fromisoformat(g["date"]).replace(tzinfo=UTC)
            records.append(
                GameRecord(
                    sport,
                    g["id"],
                    g["season"],
                    None,
                    when,
                    g["neutral"],
                    TeamRef(str(g["home"]), "", ""),
                    TeamRef(str(g["away"]), "", ""),
                    g["hs"],
                    g["as"],
                    (),
                    (),
                    {"points": g["hs"]},
                    {"points": g["as"]},
                )  # fmt: skip
            )
            for pl in g["players"]:
                rows.append(
                    {
                        "date": g["date"],
                        "game_id": g["id"],
                        "team_id": pl["team_id"],
                        "team_abbr": pl["team_id"],
                        "player_id": pl["player_id"],
                        "name": pl["name"],
                        "position": pl["position"],
                        "played": pl["played"],
                        "stats": pl["stats"],
                    }
                )
        if not rows:
            continue
        ratings = fit(records, sport)
        averages = P.team_averages(records, sport)
        for g in (x for x in games if x["date"] == day):
            slate = {
                "game_id": g["id"],
                "home": {"team_id": str(g["home"]), "abbreviation": str(g["home"])},
                "away": {"team_id": str(g["away"]), "abbreviation": str(g["away"])},
            }
            projection = project(ratings, str(g["home"]), str(g["away"]), g["neutral"], None)
            when = datetime.fromisoformat(g["date"]).replace(tzinfo=UTC)
            lines = P.project_game(sport, slate, when, projection, rows, averages, when)
            actual = {pl["player_id"]: pl for pl in g["players"]}
            for line in lines:
                pl = actual.get(line["player_id"])
                if pl is None or not pl["played"]:
                    continue
                value = int(pl["stats"].get(line["stat"]) or 0)
                grade = P.grade_line(line, value)
                graded.append((line, value, grade))
    return graded


def report(sport: str, graded: list) -> None:
    rows = [{**grade, "sport": sport, "stat": line["stat"]} for line, _, grade in graded]
    summary = P.summarise_grades(rows)
    print(f"## {sport}: {len(graded)} graded lines")
    for stat, t in summary["by_sport"][sport]["by_stat"].items():
        deciles = " ".join(f"{d:.2f}" for d in t["pit_deciles"])
        print(
            f"  {stat:10s} n={t['graded']:5d} mae {t['mae']:6.2f} bias {t['bias']:+6.2f} "
            f"centred {t['centred']:.2f} in band {t['in_band']:.2f}  pit {deciles}"
        )
    print("  dispersion by maximum likelihood (mean log-likelihood a line):")
    by_stat: dict[str, list] = defaultdict(list)
    for line, value, _ in graded:
        by_stat[line["stat"]].append((line["params"], value))
    for stat, items in by_stat.items():
        scores = []
        for k in GRID:
            total = 0.0
            for params, value in items:
                pmf = P.distribution({**params, "k": k})
                total += math.log(max(pmf.get(value, 0.0), 1e-9))
            scores.append((k, total / len(items)))
        best = max(scores, key=lambda s: s[1])
        now = P.dispersion_for(sport, stat)
        print(
            f"    {stat:10s} now {now:g}, best {best[0]:g}: "
            + " ".join(f"{k:g}:{v:.3f}" for k, v in scores)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["nba", "cbb", "nhl", "nfl", "cfb"])
    parser.add_argument("--fetch", action="store_true", help="fetch the summaries first")
    args = parser.parse_args()
    for sport in args.sports:
        if args.fetch:
            fetch(args.raw, sport)
        report(sport, walk(load(args.raw, sport), sport))


if __name__ == "__main__":
    main()
