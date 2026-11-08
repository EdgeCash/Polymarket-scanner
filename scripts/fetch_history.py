"""Fetch the game history the simulator's tables are built and graded on.

    python scripts/fetch_history.py --raw DIR [--sports nfl cfb nba cbb nhl] [--skip-download]

Writes one compact file per sport to ``tests/fixtures/history/<sport>.csv.gz``: each
finished game with what was expected of it (the closing spread and total for
the NFL; for the others, team ratings fit only on that season's earlier games,
the way the scanner itself sees a game before it starts, shots and yards
included where ``--volumes`` has fetched them), its score at the end of
regulation, its final score and whether it went to overtime. Hockey adds each
side's empty-net goals in regulation and whether a shootout settled it.

Sources (raw responses are kept in ``--raw`` so a rerun need not refetch):
- NFL: nflverse games.csv, regular season 2010 on, with closing lines.
- College football: ESPN scoreboards 2018-2025 (quarter scores, so regulation
  and overtime are exact), kept to FBS against FBS by cfbfastR's schedules.
- NBA and men's college basketball: ESPN scoreboards by day.
- NHL: the league's own score API (goals with their period and modifiers).

The running scanner never imports this script; it needs the ``train`` extras.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.projection import VOLUME_SHARE, tuning_for  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "history"
NFL_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CFB_SCHEDULE_URL = (
    "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/"
    "schedules/csv/cfb_schedules_{year}.csv"
)
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?{query}"
NHL_SCORE = "https://api-web.nhle.com/v1/score/{day}"
AGENT = {"User-Agent": "Mozilla/5.0 (research script)"}
CFB_SEASONS = range(2018, 2026)
NBA_SEASONS = [
    (date(2021, 10, 19), date(2022, 4, 10)),
    (date(2022, 10, 18), date(2023, 4, 9)),
    (date(2023, 10, 24), date(2024, 4, 14)),
    (date(2024, 10, 22), date(2025, 4, 13)),
]
CBB_SEASONS = [
    (date(2022, 11, 7), date(2023, 3, 12)),
    (date(2023, 11, 6), date(2024, 3, 17)),
    (date(2024, 11, 4), date(2025, 3, 16)),
]
NHL_SEASONS = [
    (date(2021, 10, 12), date(2022, 4, 29)),
    (date(2022, 10, 7), date(2023, 4, 14)),
    (date(2023, 10, 10), date(2024, 4, 18)),
    (date(2024, 10, 4), date(2025, 4, 17)),
]
MIN_GAMES = 3  # a side needs this many earlier games to be rated, as on the scanner


def _get(url: str, tries: int = 4):
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


def _days(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _espn_rows(payload: dict) -> list[dict]:
    rows = []
    for event in payload.get("events", []):
        comp = event["competitions"][0]
        if comp["status"]["type"].get("name") != "STATUS_FINAL":
            continue
        side = {}
        for team in comp["competitors"]:
            side[team["homeAway"]] = {
                "id": team["team"]["id"],
                "score": int(float(team.get("score") or 0)),
                "lines": [line.get("value") for line in team.get("linescores", [])],
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


def download(raw: Path, sports: list[str]) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    if "nfl" in sports and not (raw / "nfl_games.csv").exists():
        (raw / "nfl_games.csv").write_bytes(_get(NFL_URL))
    if "cfb" in sports:
        for year in CFB_SEASONS:
            path = raw / f"cfb_schedules_{year}.csv"
            if not path.exists():
                path.write_bytes(_get(CFB_SCHEDULE_URL.format(year=year)))
        if not (raw / "cfb.jsonl").exists():
            with open(raw / "cfb.jsonl", "w") as out:
                for year in CFB_SEASONS:
                    for kind, weeks in ((2, range(1, 17)), (3, (1,))):
                        for week in weeks:
                            query = (
                                f"dates={year}&seasontype={kind}&week={week}&groups=80&limit=400"
                            )
                            body = _get(ESPN.format(path="football/college-football", query=query))
                            for row in _espn_rows(json.loads(body or b"{}")):
                                out.write(json.dumps(row) + "\n")
                            time.sleep(0.2)
    for sport, path, extra, seasons in (
        ("nba", "basketball/nba", "", NBA_SEASONS),
        ("cbb", "basketball/mens-college-basketball", "&groups=50&limit=500", CBB_SEASONS),
    ):
        if sport not in sports or (raw / f"{sport}.jsonl").exists():
            continue
        with open(raw / f"{sport}.jsonl", "w") as out:
            for first, last in seasons:
                for day in _days(first, last):
                    body = _get(ESPN.format(path=path, query=f"dates={day:%Y%m%d}{extra}"))
                    for row in _espn_rows(json.loads(body or b"{}")):
                        out.write(json.dumps(row) + "\n")
                    time.sleep(0.15)
    if "nhl" in sports and not (raw / "nhl.jsonl").exists():
        with open(raw / "nhl.jsonl", "w") as out:
            for first, last in NHL_SEASONS:
                for day in _days(first, last):
                    payload = json.loads(_get(NHL_SCORE.format(day=day)) or b"{}")
                    for game in payload.get("games", []):
                        if game.get("gameType") != 2 or game.get("gameState") not in (
                            "OFF",
                            "FINAL",
                        ):
                            continue
                        goals = [
                            {
                                "p": g["periodDescriptor"]["periodType"],
                                "team": g.get("teamAbbrev"),
                                "mod": g.get("goalModifier"),
                            }
                            for g in game.get("goals", [])
                        ]
                        row = {
                            "id": game["id"],
                            "date": str(day),
                            "season": game["season"],
                            "home": game["homeTeam"]["abbrev"],
                            "away": game["awayTeam"]["abbrev"],
                            "hs": game["homeTeam"].get("score"),
                            "as": game["awayTeam"].get("score"),
                            "last": (game.get("gameOutcome") or {}).get("lastPeriodType"),
                            "goals": goals,
                        }
                        out.write(json.dumps(row) + "\n")
                    time.sleep(0.15)


NHL_BOXSCORE = "https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore"
CFB_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={game_id}"
VOLUME_THREADS = 3
VOLUME_PAUSE = 0.25


def _cfb_volume(game_id: str) -> tuple[int | None, int | None]:
    payload = json.loads(_get(CFB_SUMMARY.format(game_id=game_id)) or b"{}")
    out: dict[str, int | None] = {"home": None, "away": None}
    for team in (payload.get("boxscore") or {}).get("teams") or []:
        for stat in team.get("statistics") or []:
            if stat.get("name") == "totalYards":
                try:
                    out[team["homeAway"]] = int(stat["displayValue"])
                except (KeyError, ValueError):
                    pass
    return out["home"], out["away"]


def _nhl_volume(game_id: str) -> tuple[int | None, int | None]:
    payload = json.loads(_get(NHL_BOXSCORE.format(game_id=game_id)) or b"{}")
    return (payload.get("homeTeam") or {}).get("sog"), (payload.get("awayTeam") or {}).get("sog")


def download_volumes(raw: Path, sports: list[str]) -> None:
    """Each game's volume stat (shots on goal, total yards), the half of a performance the
    scanner's ratings read beside the score. Kept in ``<sport>_volume.jsonl``, resumable."""
    from concurrent.futures import ThreadPoolExecutor

    for sport, fetch in (("cfb", _cfb_volume), ("nhl", _nhl_volume)):
        if sport not in sports:
            continue
        source, target = raw / f"{sport}.jsonl", raw / f"{sport}_volume.jsonl"
        ids = [str(json.loads(line)["id"]) for line in open(source)]
        done = set()
        if target.exists():
            done = {str(json.loads(line)["id"]) for line in open(target)}
        todo = [i for i in dict.fromkeys(ids) if i not in done]
        print(f"{sport}: {len(todo)} games' volumes to fetch", flush=True)

        def one(game_id: str, fetch=fetch):
            home, away = fetch(game_id)
            time.sleep(VOLUME_PAUSE)
            return {"id": game_id, "home": home, "away": away}

        with open(target, "a") as out, ThreadPoolExecutor(VOLUME_THREADS) as pool:
            for n, row in enumerate(pool.map(one, todo), start=1):
                out.write(json.dumps(row) + "\n")
                if n % 500 == 0:
                    out.flush()
                    print(f"{sport}: {n}/{len(todo)}", flush=True)
        print(f"{sport}: volumes done", flush=True)


def _volumes(raw: Path, sport: str) -> dict[str, tuple[int | None, int | None]]:
    path = raw / f"{sport}_volume.jsonl"
    if not path.exists():
        return {}
    out = {}
    for line in open(path):
        row = json.loads(line)
        out[str(row["id"])] = (row["home"], row["away"])
    return out


def rate_before(games: pd.DataFrame, sport: str, pseudo_games: float | None = None) -> pd.DataFrame:
    """Each game's expected margin and total from ratings fit on its season's earlier days.

    The same model as ``projection.fit``: a performance is half the score and half
    what the volume stat (yards, shots) says at the league's rate so far, when the
    games carry one (``home_vol``, ``away_vol``); offence and defence above average,
    shrunk by ``pseudo_games`` (the sport's own unless given), with the sport's fixed
    home edge.
    """
    tuning = tuning_for(sport)
    hfa = tuning.hfa
    pseudo = tuning.pseudo_games if pseudo_games is None else pseudo_games
    has_volume = "home_vol" in games and games.home_vol.notna().any()
    out = []
    for _, season in games.groupby("season"):
        season = season.sort_values("date")
        teams = sorted(set(season.home) | set(season.away))
        index = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        played: dict[str, int] = dict.fromkeys(teams, 0)
        # (offence team, defence team, h, points, volume or nan)
        rows: list[tuple[int, int, float, float, float]] = []
        for _day, todays in season.groupby("date", sort=True):
            if rows:
                off_i = np.array([r[0] for r in rows])
                def_i = np.array([r[1] for r in rows])
                h_term = np.array([r[2] for r in rows])
                pts = np.array([r[3] for r in rows])
                if has_volume:
                    vol = np.array([r[4] for r in rows])
                    known = ~np.isnan(vol) & (vol > 0)
                    per_volume = pts[known].sum() / vol[known].sum() if known.any() else None
                    if per_volume is not None:
                        pts = np.where(
                            known, (1 - VOLUME_SHARE) * pts + VOLUME_SHARE * per_volume * vol, pts
                        )
                league = pts.mean()
                resid = pts - league - h_term
                # Ridge normal equations for [offence; defence].
                a = np.zeros((2 * n, 2 * n))
                b = np.zeros(2 * n)
                np.add.at(a, (off_i, off_i), 1.0)
                np.add.at(a, (n + def_i, n + def_i), 1.0)
                np.add.at(a, (off_i, n + def_i), 1.0)
                np.add.at(a, (n + def_i, off_i), 1.0)
                np.add.at(b, off_i, resid)
                np.add.at(b, n + def_i, resid)
                a[np.diag_indices(2 * n)] += pseudo
                x = np.linalg.solve(a, b)
                offence, defence = x[:n], x[n:]
                for game in todays.itertuples():
                    if played[game.home] < MIN_GAMES or played[game.away] < MIN_GAMES:
                        continue
                    h = 0.0 if game.neutral else hfa / 2.0
                    hi, ai = index[game.home], index[game.away]
                    home = league + offence[hi] + defence[ai] + h
                    away = league + offence[ai] + defence[hi] - h
                    out.append((game.Index, home - away, home + away))
            for game in todays.itertuples():
                h = 0.0 if game.neutral else hfa / 2.0
                hi, ai = index[game.home], index[game.away]
                home_vol = float(game.home_vol) if has_volume else float("nan")
                away_vol = float(game.away_vol) if has_volume else float("nan")
                rows.append((hi, ai, h, float(game.home_final), home_vol))
                rows.append((ai, hi, -h, float(game.away_final), away_vol))
                played[game.home] += 1
                played[game.away] += 1
    expected = pd.DataFrame(out, columns=["idx", "exp_margin", "exp_total"]).set_index("idx")
    return games.join(expected, how="inner")


def _regulation(lines: list, periods: int) -> int | None:
    if len(lines) < periods or any(v is None for v in lines[:periods]):
        return None
    return int(sum(lines[:periods]))


def compact_espn(raw: Path, sport: str, periods: int) -> pd.DataFrame:
    rows = []
    for line in open(raw / f"{sport}.jsonl"):
        g = json.loads(line)
        home_reg = _regulation(g["home"]["lines"], periods)
        away_reg = _regulation(g["away"]["lines"], periods)
        if home_reg is None or away_reg is None:
            continue
        ot = int(len(g["home"]["lines"]) > periods)
        if ot and home_reg != away_reg:
            continue  # the line scores do not add up; leave the game out
        rows.append(
            {
                "id": g["id"],
                "season": g["season"],
                "date": g["date"],
                "neutral": g["neutral"],
                "home": g["home"]["id"],
                "away": g["away"]["id"],
                "home_reg": home_reg,
                "away_reg": away_reg,
                "home_final": g["home"]["score"],
                "away_final": g["away"]["score"],
                "ot": ot,
            }
        )
    return pd.DataFrame(rows).drop_duplicates("id")


def _fbs_pairs(raw: Path) -> set[str]:
    divisions = pd.concat(
        pd.read_csv(
            raw / f"cfb_schedules_{y}.csv", usecols=["game_id", "home_division", "away_division"]
        )
        for y in CFB_SEASONS
    )
    both = (divisions.home_division == "fbs") & (divisions.away_division == "fbs")
    return set(divisions[both].game_id.astype(str))


def load_games(raw: Path, sport: str) -> pd.DataFrame:
    """Every game of a sport that is rated from history (not the NFL, which has lines),
    with its volume stat attached where fetched."""
    if sport == "nhl":
        rows = []
        for line in open(raw / "nhl.jsonl"):
            g = json.loads(line)
            reg = [x for x in g["goals"] if x["p"] == "REG"]
            home_reg = sum(1 for x in reg if x["team"] == g["home"])
            away_reg = sum(1 for x in reg if x["team"] == g["away"])
            rows.append(
                {
                    "id": g["id"],
                    "season": int(str(g["season"])[:4]),
                    "date": g["date"],
                    "neutral": False,
                    "home": g["home"],
                    "away": g["away"],
                    "home_reg": home_reg,
                    "away_reg": away_reg,
                    "home_final": g["hs"],
                    "away_final": g["as"],
                    "ot": int(g["last"] in ("OT", "SO")),
                    "shootout": int(g["last"] == "SO"),
                    "home_en": sum(
                        1 for x in reg if x["team"] == g["home"] and x["mod"] == "empty-net"
                    ),
                    "away_en": sum(
                        1 for x in reg if x["team"] == g["away"] and x["mod"] == "empty-net"
                    ),
                }
            )
        games = pd.DataFrame(rows).drop_duplicates("id")
        games = games[(games.ot == 1) | (games.home_reg != games.away_reg)]
    else:
        periods = 4 if sport in ("cfb", "nba") else 2
        games = compact_espn(raw, sport, periods)
    games = games.reset_index(drop=True)
    volumes = _volumes(raw, sport)
    if volumes:
        games["home_vol"] = pd.to_numeric(
            [volumes.get(str(i), (None, None))[0] for i in games["id"]], errors="coerce"
        )
        games["away_vol"] = pd.to_numeric(
            [volumes.get(str(i), (None, None))[1] for i in games["id"]], errors="coerce"
        )
    return games


def graded_subset(raw: Path, sport: str, rated: pd.DataFrame) -> pd.DataFrame:
    """The rated games that are graded: FBS against FBS in college football."""
    if sport == "cfb":
        return rated[rated["id"].astype(str).isin(_fbs_pairs(raw))]
    return rated


def build(raw: Path, sport: str) -> pd.DataFrame:
    if sport == "nfl":
        g = pd.read_csv(raw / "nfl_games.csv")
        g = g[(g.game_type == "REG") & (g.season >= 2010) & g.result.notna()]
        g = g[g.spread_line.notna() & g.total_line.notna()]
        level = np.minimum(g.home_score, g.away_score)
        ot = g.overtime.astype(int)
        return pd.DataFrame(
            {
                "season": g.season,
                "date": g.gameday,
                "exp_margin": g.spread_line,  # positive: the home side favoured
                "exp_total": g.total_line,
                "exp_source": "close",
                # nflverse has no quarter scores: an overtime game was level at the lower
                # final score, which misses only a loser's points in overtime.
                "home_reg": np.where(ot == 1, level, g.home_score),
                "away_reg": np.where(ot == 1, level, g.away_score),
                "home_final": g.home_score,
                "away_final": g.away_score,
                "ot": ot,
            }
        )
    games = load_games(raw, sport)
    rated = graded_subset(raw, sport, rate_before(games, sport))
    rated = rated.assign(exp_source="ratings")
    keep = [
        "season", "date", "exp_margin", "exp_total", "exp_source",
        "home_reg", "away_reg", "home_final", "away_final", "ot",
    ]  # fmt: skip
    if sport == "nhl":
        keep += ["shootout", "home_en", "away_en"]
    return rated[keep].round({"exp_margin": 2, "exp_total": 2})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True, help="where raw responses are kept")
    parser.add_argument("--sports", nargs="+", default=["nfl", "cfb", "nba", "cbb", "nhl"])
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument(
        "--volumes", action="store_true", help="also fetch each game's shots or yards (slow)"
    )
    parser.add_argument("--volumes-only", action="store_true")
    args = parser.parse_args()
    if args.volumes_only:
        download_volumes(args.raw, args.sports)
        return
    if not args.skip_download:
        download(args.raw, args.sports)
    if args.volumes:
        download_volumes(args.raw, args.sports)
    OUT.mkdir(parents=True, exist_ok=True)
    for sport in args.sports:
        frame = build(args.raw, sport)
        for column in ("home_reg", "away_reg", "home_final", "away_final", "ot"):
            frame[column] = frame[column].astype(int)
        frame.to_csv(OUT / f"{sport}.csv.gz", index=False)
        print(sport, len(frame), "games", flush=True)


if __name__ == "__main__":
    main()
