"""Fit what a missing man costs: the coefficients in scanner/injuries.py.

    python scripts/fit_injuries.py --raw DIR [--sports nba nhl nfl] [--holdout-share 0.3]

The history is the seasons of box scores scripts/fetch_box_scores.py gathered. Every
game is given an absence-blind expected margin from team ratings fit only on its
season's earlier games (with the back-to-back adjustment, as the scanner does), and
the miss, actual less expected, is what the absences have to explain. An absence is
a man who played his side's previous game and not this one: the box score lists him
as did-not-play or leaves him out. That is the same thing the live injury report
tells the scanner a day early, minus the status's uncertainty, which this fit cannot
see; the coefficient is per unit actually lost. Each absentee is valued exactly as
scanner.injuries values him before a game, from his lines up to that day, so the
units here are the units the scanner adds up live.

The margin's miss is regressed on the two sides' lost units (away less home, so a
home absence shows as a negative number), one coefficient per feature, with and
without an intercept, and the forms are compared on the games held out at the end
of the sample (the latest share of each season), so a coefficient ships only when it
lowers the miss on games it was not fit to.

The running scanner never imports this script; it needs the ``train`` extras.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.books import BookLine  # noqa: E402
from scanner.gamelog import GameRecord, PlayerLine, TeamRef  # noqa: E402
from scanner.injuries import RULES, SideContext, absentees  # noqa: E402
from scanner.projection import MIN_GAMES, fit, project  # noqa: E402
from scanner.rest import days_since_last  # noqa: E402

FEATURES = {
    "nba": ("production",),
    "cbb": ("production",),
    "nhl": ("production",),  # the goalie is fit by scripts/fit_goalies.py
    "nfl": ("quarterback", "skill_yards"),
    "cfb": ("quarterback", "skill_yards"),
}


def load(raw: Path, sport: str) -> list[GameRecord]:
    records = []
    for path in sorted(raw.glob(f"{sport}_*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                g = json.loads(line)
                if g.get("season_type") not in (None, 2):
                    continue  # the bulk files carry the playoffs: the fits are on the season
                records.append(
                    GameRecord(
                        sport=g["sport"],
                        game_id=g["game_id"],
                        season=g["season"],
                        week=g["week"],
                        date=datetime.fromisoformat(g["date"]),
                        neutral=bool(g["neutral"]),
                        home=TeamRef(g["home_id"], g["home_abbr"], g["home_abbr"]),
                        away=TeamRef(g["away_id"], g["away_abbr"], g["away_abbr"]),
                        home_score=g["home_score"],
                        away_score=g["away_score"],
                        home_lines=tuple(g["home_lines"]),
                        away_lines=tuple(g["away_lines"]),
                        home_stats=g["home_stats"],
                        away_stats=g["away_stats"],
                        book=BookLine.from_dict(g["book"]) if g.get("book") else None,
                        players=tuple(
                            PlayerLine(
                                team_id=p["team_id"],
                                player_id=p["player_id"],
                                name=p["name"],
                                position=p["position"],
                                starter=p["starter"],
                                played=p["played"],
                                stats=p["stats"],
                            )
                            for p in g["players"]
                        ),
                    )
                )
    records.sort(key=lambda r: (r.date, r.game_id))
    return records


def player_rows(record: GameRecord) -> list[dict]:
    """The diary's player_games rows for one game, as scanner.injuries reads them."""
    sides = {record.home.team_id: record.home, record.away.team_id: record.away}
    out = []
    for line in record.players:
        team = sides.get(line.team_id)
        if team is None:
            continue
        out.append(
            {
                "game_id": record.game_id,
                "date": record.date.isoformat(),
                "team_id": line.team_id,
                "team_abbr": team.abbreviation,
                "player_id": line.player_id,
                "name": line.name,
                "position": line.position,
                "played": line.played,
                "stats": line.stats,
            }
        )
    return out


def lost_units(sport: str, record: GameRecord, side: TeamRef, rows_so_far: list[dict]) -> dict:
    """Units the side lost in this game: men who played its previous game and not this one,
    valued as the scanner would value them the day before, from their lines up to then."""
    context = SideContext.build(side.team_id, rows_so_far, [], {})
    if not context.team_games:
        return {}
    previous = context.team_games[-1]
    played_previous = {
        pid for pid, lines in context.lines.items() if any(r["game_id"] == previous for r in lines)
    }
    played_now = {p.player_id for p in record.players if p.team_id == side.team_id and p.played}
    missing = played_previous - played_now
    if not missing:
        return {}
    report = [
        {
            "player_id": pid,
            "name": context.names.get(pid, pid),
            "position": context.positions.get(pid) or _position_from_lines(sport, context, pid),
            "status": "out",
        }
        for pid in missing
    ]
    totals: dict[str, float] = defaultdict(float)
    for entry in absentees(sport, report, context):
        for feature, units in entry["units"].items():
            totals[feature] += units * entry["played_share"]
    return dict(totals)


def _position_from_lines(sport: str, context: SideContext, player_id: str) -> str | None:
    """Football's box scores carry no positions: a man who threw is a quarterback, one who
    ran or caught a back or receiver (valued by his yards, as the scanner values them)."""
    if sport not in ("nfl", "cfb"):
        return None
    lines = context.lines.get(player_id, [])
    if any(float(r["stats"].get("pass_att") or 0) >= 5 for r in lines):
        return "QB"
    if any(
        float(r["stats"].get("rush_yards") or 0) or float(r["stats"].get("rec_yards") or 0)
        for r in lines
    ):
        return "WR"
    return None


def build_table(sport: str, records: list[GameRecord]) -> list[dict]:
    by_season: dict[int | None, list[GameRecord]] = defaultdict(list)
    for r in records:
        by_season[r.season].append(r)
    table = []
    for season, games in sorted(by_season.items(), key=lambda kv: kv[0] or 0):
        rows_so_far: list[dict] = []
        earlier: list[GameRecord] = []
        schedule: dict[str, list[datetime]] = defaultdict(list)
        for g in games:
            schedule[g.home.team_id].append(g.date)
            schedule[g.away.team_id].append(g.date)
        ratings, rated_through = None, None
        for index, record in enumerate(games):
            day = record.date.date()
            if rated_through != day:
                ratings = fit(earlier, sport)
                rated_through = day
            home, away = ratings.team(record.home.team_id), ratings.team(record.away.team_id)
            if (
                home is not None
                and away is not None
                and min(home["games"], away["games"]) >= MIN_GAMES
            ):
                rest = {
                    "home": days_since_last(record.date, schedule[record.home.team_id]),
                    "away": days_since_last(record.date, schedule[record.away.team_id]),
                }
                projection = project(
                    ratings,
                    record.home.team_id,
                    record.away.team_id,
                    record.neutral,
                    None,
                    {"rest": rest},
                )
                if projection is not None:
                    home_lost = lost_units(sport, record, record.home, rows_so_far)
                    away_lost = lost_units(sport, record, record.away, rows_so_far)
                    table.append(
                        {
                            "season": season,
                            "index": index / len(games),
                            "miss": (record.home_score - record.away_score)
                            - projection["raw"]["margin"],
                            **{
                                f: away_lost.get(f, 0.0) - home_lost.get(f, 0.0)
                                for f in FEATURES[sport]
                            },
                            **{f"home_{f}": home_lost.get(f, 0.0) for f in FEATURES[sport]},
                            **{f"away_{f}": away_lost.get(f, 0.0) for f in FEATURES[sport]},
                        }
                    )
            # Then the game joins the history the next one is rated from.
            earlier.append(record)
            rows_so_far.extend(player_rows(record))
        print(f"{sport} {season}: {len(games)} games, {len(table)} rated so far", flush=True)
    return table


def fit_form(train, test, columns, target="miss", intercept=True):
    def design(rows):
        cols = [np.array([r[c] for c in columns]) for r in rows] if rows else []
        x = np.array(cols) if rows else np.zeros((0, len(columns)))
        if intercept:
            x = np.column_stack([np.ones(len(rows)), x])
        return x

    y_train = np.array([r[target] for r in train])
    y_test = np.array([r[target] for r in test])
    beta, *_ = np.linalg.lstsq(design(train), y_train, rcond=None)
    slopes = beta[1:] if intercept else beta
    adjusted = y_test - design(test)[:, 1 if intercept else 0 :] @ slopes
    return slopes, float(np.sqrt(np.mean(adjusted**2))), float(np.sqrt(np.mean(y_test**2)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--sports", nargs="+", default=["nba", "nhl", "nfl"])
    parser.add_argument("--holdout-share", type=float, default=0.3)
    args = parser.parse_args()
    for sport in args.sports:
        records = load(args.raw, sport)
        if not records:
            print(f"{sport}: no history in {args.raw}")
            continue
        table = build_table(sport, records)
        features = FEATURES[sport]
        train = [r for r in table if r["index"] < 1 - args.holdout_share]
        test = [r for r in table if r["index"] >= 1 - args.holdout_share]
        rule = RULES[sport]
        print(f"\n{sport}: {len(table)} rated games, {len(train)} fit, {len(test)} held out")
        for f in features:
            with_loss = [r for r in table if r[f"home_{f}"] or r[f"away_{f}"]]
            lost = [r[f"home_{f}"] + r[f"away_{f}"] for r in with_loss]
            print(
                f"  {f}: {len(with_loss)} games with a loss, mean lost units when lost "
                f"{np.mean(lost) if lost else 0:.2f}"
            )
        _, plain, _ = fit_form(train, test, [], intercept=True)
        print(f"  miss held out, no adjustment: {plain:.3f} {rule.unit}")
        for intercept in (True, False):
            slopes, miss, _ = fit_form(train, test, list(features), intercept=intercept)
            label = "with intercept" if intercept else "through the origin"
            coeffs = ", ".join(f"{f} {b:.3f}" for f, b in zip(features, slopes, strict=True))
            print(f"  {label}: {coeffs}; miss held out {miss:.3f}")
        # Every season together, for the numbers that ship once a form has held out.
        slopes, _, _ = fit_form(table, table, list(features), intercept=False)
        coeffs = ", ".join(f"{f} {b:.3f}" for f, b in zip(features, slopes, strict=True))
        print(f"  fit on every game: {coeffs}")
        shipped = ", ".join(f"{f} {rule.coefficients.get(f, 0):.3f}" for f in features)
        print(f"  shipped now: {shipped}")
        # The shipped coefficients' own held-out miss, for the record.
        y = np.array([r["miss"] for r in test])
        adj = sum(rule.coefficients.get(f, 0.0) * np.array([r[f] for r in test]) for f in features)
        print(
            f"  miss held out with the shipped coefficients: {np.sqrt(np.mean((y - adj) ** 2)):.3f}"
        )


if __name__ == "__main__":
    main()
