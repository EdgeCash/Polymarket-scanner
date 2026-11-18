"""Build scanner/priors.json: last season's final ratings, the prior each team starts from.

    python scripts/build_priors.py --raw DIR [--sports nba cbb nhl nfl cfb] [--season 2026]

For each sport the previous season's finished games are read from ESPN's scoreboards
(by day for basketball and hockey, by week for football; kept in ``--raw`` so a rerun
need not refetch) and rated once, on points alone, with the ridge the scanner uses
(two pseudo-games, so the final ratings are nearly unshrunk): offence and defence in
points above average for every team that played ten games, by ESPN team id. A team
the file does not know starts where the season's own strangers finished: the
typical net rating, unshrunk, of the sides that played under ten games against
this division (a visitor from outside it, about 25 points below average in both
college sports); where there were none, the weakest tenth of the set.

``--season`` is the ESPN season label of the season to build (the ending year: the
2025-26 NBA season is 2026); the default is the season now under way for each sport.
The running scanner reads the file through ``scanner.priors``; it never imports this.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.projection import tuning_for  # noqa: E402

OUT = ROOT / "scanner" / "priors.json"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?{query}"
AGENT = {"User-Agent": "Mozilla/5.0 (research script)"}
PATHS = {
    "nba": "basketball/nba",
    "cbb": "basketball/mens-college-basketball",
    "nhl": "hockey/nhl",
    "nfl": "football/nfl",
    "cfb": "football/college-football",
}
EXTRA = {"cbb": "&groups=50&limit=500", "cfb": "&groups=80&limit=400"}
# Regular-season windows by ESPN season label (the ending year for the winter sports).
DAILY_SEASONS = {
    "nba": {
        2025: (date(2024, 10, 22), date(2025, 4, 13)),
        2026: (date(2025, 10, 21), date(2026, 4, 12)),
    },
    "cbb": {
        2025: (date(2024, 11, 4), date(2025, 3, 16)),
        2026: (date(2025, 11, 3), date(2026, 3, 15)),
    },
    "nhl": {
        2025: (date(2024, 10, 4), date(2025, 4, 17)),
        2026: (date(2025, 10, 7), date(2026, 4, 16)),
    },
}
WEEKS = {"nfl": range(1, 19), "cfb": range(1, 17)}
MIN_GAMES = 10
PSEUDO = 2.0
UNKNOWN_QUANTILE = 0.1


def _get(url: str, tries: int = 4) -> bytes | None:
    import urllib.request

    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers=AGENT)
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - a research fetch retries anything
            if attempt == tries - 1:
                print("failed", url, exc, flush=True)
                return None
            time.sleep(2**attempt)
    return None


def _rows(payload: dict) -> list[dict]:
    rows = []
    for event in payload.get("events", []):
        comp = event["competitions"][0]
        if comp["status"]["type"].get("name") != "STATUS_FINAL":
            continue
        side = {}
        for team in comp["competitors"]:
            side[team["homeAway"]] = {
                "id": str(team["team"]["id"]),
                "abbr": team["team"].get("abbreviation"),
                "name": team["team"].get("displayName"),
                "score": int(float(team.get("score") or 0)),
            }
        if "home" in side and "away" in side:
            rows.append(
                {
                    "id": event["id"],
                    "date": event["date"][:10],
                    "season": event["season"]["year"],
                    "neutral": bool(comp.get("neutralSite")),
                    "home": side["home"],
                    "away": side["away"],
                }
            )
    return rows


def download(raw: Path, sport: str, season: int) -> list[dict]:
    path = raw / f"{sport}_{season}.jsonl"
    if not path.exists():
        rows = []
        if sport in DAILY_SEASONS:
            first, last = DAILY_SEASONS[sport][season]
            day = first
            while day <= last:
                query = f"dates={day:%Y%m%d}{EXTRA.get(sport, '')}"
                body = _get(ESPN.format(path=PATHS[sport], query=query))
                rows += _rows(json.loads(body or b"{}"))
                day += timedelta(days=1)
                time.sleep(0.15)
        else:
            for week in WEEKS[sport]:
                query = f"dates={season}&seasontype=2&week={week}{EXTRA.get(sport, '')}"
                body = _get(ESPN.format(path=PATHS[sport], query=query))
                rows += _rows(json.loads(body or b"{}"))
                time.sleep(0.15)
        with open(path, "w") as out:
            for row in rows:
                out.write(json.dumps(row) + "\n")
    seen, rows = set(), []
    for line in open(path):
        row = json.loads(line)
        if row["id"] not in seen and int(row["season"]) == season:
            seen.add(row["id"])
            rows.append(row)
    return rows


def rate(rows: list[dict], sport: str) -> dict:
    """Final ratings for a season: ridge offence and defence toward average, points only."""
    hfa = tuning_for(sport).hfa
    teams = sorted({r["home"]["id"] for r in rows} | {r["away"]["id"] for r in rows})
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    off_i, def_i, h_t, pts = [], [], [], []
    labels: dict[str, dict] = {}
    for r in rows:
        h = 0.0 if r["neutral"] else hfa / 2.0
        hi, ai = index[r["home"]["id"]], index[r["away"]["id"]]
        off_i += [hi, ai]
        def_i += [ai, hi]
        h_t += [h, -h]
        pts += [float(r["home"]["score"]), float(r["away"]["score"])]
        for side in ("home", "away"):
            labels[r[side]["id"]] = {"abbr": r[side]["abbr"], "name": r[side]["name"]}
    off_i, def_i = np.array(off_i, dtype=int), np.array(def_i, dtype=int)
    h_t, pts = np.array(h_t), np.array(pts)
    league = pts.mean()
    resid = pts - league - h_t
    a = np.zeros((2 * n, 2 * n))
    b = np.zeros(2 * n)
    np.add.at(a, (off_i, off_i), 1.0)
    np.add.at(a, (n + def_i, n + def_i), 1.0)
    np.add.at(a, (off_i, n + def_i), 1.0)
    np.add.at(a, (n + def_i, off_i), 1.0)
    np.add.at(b, off_i, resid)
    np.add.at(b, n + def_i, resid)
    a[np.diag_indices(2 * n)] += PSEUDO
    x = np.linalg.solve(a, b)
    played = np.zeros(n)
    np.add.at(played, off_i, 1)
    out = {}
    strangers = []
    for t, i in index.items():
        if played[i] < MIN_GAMES:
            if played[i] > 0:
                # Undo the shrinkage: a side with a game or two is held near average.
                strangers.append(float(x[i] - x[n + i]) * (played[i] + PSEUDO) / played[i])
            continue
        if played[i] >= MIN_GAMES:
            out[t] = {
                "offense": round(float(x[i]), 3),
                "defense": round(float(x[n + i]), 3),
                "games": int(played[i]),
                "abbr": labels[t]["abbr"],
                "name": labels[t]["name"],
            }
    nets = np.array([v["offense"] - v["defense"] for v in out.values()])
    if len(strangers) >= 10:
        low = float(np.median(strangers))
    else:
        low = float(np.quantile(nets, UNKNOWN_QUANTILE))
    return {
        "teams": out,
        "unknown": {"offense": round(low / 2.0, 3), "defense": round(-low / 2.0, 3)},
        "strangers": len(strangers),
        "league_points": round(float(league), 3),
        "games": len(rows),
    }


def current_season(sport: str, today: date) -> int:
    """ESPN's label for the season under way: the ending year for the winter sports."""
    if sport in ("nfl", "cfb"):
        return today.year if today.month >= 8 else today.year - 1
    return today.year + 1 if today.month >= 8 else today.year


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["nba", "cbb", "nhl", "nfl", "cfb"])
    parser.add_argument("--season", type=int, default=None, help="the season to build for")
    args = parser.parse_args()
    args.raw.mkdir(parents=True, exist_ok=True)
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    for sport in args.sports:
        season = args.season or current_season(sport, date.today())
        previous = season - 1
        rows = download(args.raw, sport, previous)
        rated = rate(rows, sport)
        existing[sport] = {
            "season": previous,
            "built": datetime.now().date().isoformat(),
            **rated,
        }
        print(
            f"{sport}: {previous} season, {rated['games']} games, {len(rated['teams'])} teams "
            f"rated, unknown side at net {rated['unknown']['offense'] * 2:+.1f}",
            flush=True,
        )
    OUT.write_text(json.dumps(existing, indent=1, sort_keys=True) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
