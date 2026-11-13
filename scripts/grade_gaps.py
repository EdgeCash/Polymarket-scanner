"""Who is right when the model and the book sit far apart: the gap report, re-runnable.

    python scripts/grade_gaps.py [--sports cbb nba cfb nfl] [--json OUT] [--sample N]

Every game in ``tests/fixtures/history/<sport>_lines.csv.gz``
(``fetch_history.py --spreads``) carries the raw model's margin from ratings fit
only on that season's earlier games, the book's closing margin (and the open
where ESPN kept it), each side's games so far and the final. The NFL's ratings
are on points alone, nflverse carrying no yards, so its raw model is a touch
rougher than the scanner's. The blend is laid
over it as the scanner does (``projection.BOOK_PSEUDO_GAMES``: the model's
weight is games over games plus six). Then, by how far the raw model sat from
the book:

- how often the model's side covered the closing spread, and how far the real
  margin landed from the close in the model's direction (zero when the close
  is right, positive when the model was);
- how far each of the raw model, the blend and the close missed the margin;
- what the score grid gave the model's side, raw and blended, against how often
  it came in: the card's edge, graded.

Then the big spreads on their own (CARD_BIG_LINE_SIGMAS of the sport's spread
from level): the underdog's cover rate at the close, early and late, and by how
many games each side had behind its rating. ``tests/test_gap_history.py`` runs
the same grading on the fixture and fails when a number drifts past its
tolerance.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner.config import CARD_BIG_LINE_GAMES, CARD_BIG_LINE_SIGMAS  # noqa: E402
from scanner.grid import cover, grid, without_push  # noqa: E402
from scanner.projection import BOOK_PSEUDO_GAMES, tuning_for  # noqa: E402

HISTORY = ROOT / "tests" / "fixtures" / "history"
SPORTS = ("cbb", "nba", "cfb", "nfl")
GAP_BANDS = {
    "cbb": ((0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 99)),
    "nba": ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15), (15, 99)),
    "cfb": ((0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 99)),
    "nfl": ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15), (15, 99)),
}
SPREAD_BANDS = {
    "cbb": ((0, 12), (12, 24), (24, 30), (30, 99)),
    "nba": ((0, 7), (7, 14), (14, 99)),
    "cfb": ((0, 16), (16, 32.5), (32.5, 40), (40, 99)),
    "nfl": ((0, 7), (7, 14), (14, 99)),
}
# The first weeks of a season, where the ratings have the least behind them.
EARLY_DAYS = {"cbb": 17, "nba": 14, "cfb": 21, "nfl": 21}
ROUNDING = (0.25, 0.5)  # expectations are rounded this finely so nearby games share a grid
SPORT = "cbb"  # the sport being graded; set by grade()


def lines_path(sport: str) -> Path:
    return HISTORY / f"{sport}_lines.csv.gz"


def load(path: Path, sample: int | None = None, sport: str = "cbb") -> list[dict]:
    with gzip.open(path, "rt") as handle:
        rows = list(csv.DictReader(handle))
    out = []
    for row in rows:
        game = {
            "season": int(row["season"]),
            "date": row["date"],
            "neutral": int(row["neutral"]),
            "home_games": int(row["home_games"]),
            "away_games": int(row["away_games"]),
            "exp_margin": float(row["exp_margin"]),
            "exp_total": float(row["exp_total"]),
            "book_margin": float(row["book_margin"]),
            "book_margin_open": _float(row.get("book_margin_open")),
            "book_total": _float(row.get("book_total")),
            "home_final": int(row["home_final"]),
            "away_final": int(row["away_final"]),
        }
        game["margin"] = game["home_final"] - game["away_final"]
        games = min(game["home_games"], game["away_games"])
        weight = games / (games + BOOK_PSEUDO_GAMES)
        game["weight"] = weight
        game["blend_margin"] = weight * game["exp_margin"] + (1 - weight) * game["book_margin"]
        out.append(game)
    first = {}
    for game in out:
        first[game["season"]] = min(first.get(game["season"], game["date"]), game["date"])
    for game in out:
        game["early"] = _days_between(first[game["season"]], game["date"]) < EARLY_DAYS[sport]
    if sample:
        step = max(1, len(out) // sample)
        out = out[::step][:sample]
    return out


def _float(text) -> float | None:
    return None if text in (None, "") else float(text)


def _days_between(first: str, day: str) -> int:
    from datetime import date

    return (date.fromisoformat(day) - date.fromisoformat(first)).days


def _round(value: float, step: float) -> float:
    return round(value / step) * step


def side_chance(margin: float, total: float, line: float) -> float | None:
    """The grid's chance, given no push, that the home side covers ``line`` (the home
    handicap) when the margin is expected at ``margin``; None when the grid cannot be laid out."""
    tuning = tuning_for(SPORT)
    g = grid(
        SPORT,
        _round(margin, ROUNDING[0]),
        _round(total, ROUNDING[1]),
        tuning.sigma,
        tuning.sigma_total,
    )
    if g is None:
        return None
    split = without_push(cover(g, line))
    return None if split is None else split[0]


def _record(games: list[dict], key: str = "exp_margin") -> dict:
    """The record of the side ``key``'s margin takes against the closing spread."""
    wins = losses = pushes = 0
    toward = []
    for game in games:
        gap = game[key] - game["book_margin"]
        if gap == 0:
            continue
        landed = game["margin"] - game["book_margin"]
        toward.append(landed if gap > 0 else -landed)
        if landed == 0:
            pushes += 1
        elif (landed > 0) == (gap > 0):
            wins += 1
        else:
            losses += 1
    decided = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "cover": wins / decided if decided else None,
        "toward_model": sum(toward) / len(toward) if toward else None,
    }


def _rmse(games: list[dict], key: str) -> float | None:
    if not games:
        return None
    return math.sqrt(sum((g["margin"] - g[key]) ** 2 for g in games) / len(games))


def _dog_record(games: list[dict]) -> dict:
    """How often the underdog covered the closing spread."""
    wins = losses = pushes = 0
    for game in games:
        book = game["book_margin"]
        if book == 0:
            continue
        landed = game["margin"] - book  # positive: the home side beat the spread
        dog_covered = landed < 0 if book > 0 else landed > 0
        if landed == 0:
            pushes += 1
        elif dog_covered:
            wins += 1
        else:
            losses += 1
    decided = wins + losses
    return {
        "games": len(games),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "cover": wins / decided if decided else None,
    }


def _calibration(games: list[dict], key: str) -> dict:
    """What the grid gave the side ``key`` takes against the close, by 10% bin, against how
    often that side came in (pushes left out)."""
    bins: dict[str, list[tuple[float, float]]] = {}
    for game in games:
        gap = game[key] - game["book_margin"]
        if gap == 0:
            continue
        chance = side_chance(game[key], game["exp_total"], -game["book_margin"])
        if chance is None:
            continue
        if gap < 0:
            chance = 1.0 - chance
        landed = game["margin"] - game["book_margin"]
        if landed == 0:
            continue
        came_in = 1.0 if (landed > 0) == (gap > 0) else 0.0
        low = min(int(chance * 10) * 10, 90)
        bins.setdefault(f"{low}-{low + 10}", []).append((chance, came_in))
    out = {}
    for name, pairs in sorted(bins.items(), key=lambda kv: int(kv[0].split("-")[0])):
        out[name] = {
            "games": len(pairs),
            "given": sum(c for c, _ in pairs) / len(pairs),
            "came_in": sum(w for _, w in pairs) / len(pairs),
        }
    return out


def grade(games: list[dict], sport: str = "cbb") -> dict:
    global SPORT
    SPORT = sport
    sigma = tuning_for(SPORT).sigma
    big = CARD_BIG_LINE_SIGMAS * sigma
    by_gap = {}
    for low, high in GAP_BANDS[sport]:
        part = [g for g in games if low <= abs(g["exp_margin"] - g["book_margin"]) < high]
        by_gap[f"{low}-{high}"] = {
            "games": len(part),
            "model_side": _record(part),
            "blend_side": _record(part, "blend_margin"),
            "rmse": {
                "model": _rmse(part, "exp_margin"),
                "blend": _rmse(part, "blend_margin"),
                "close": _rmse(part, "book_margin"),
            },
            "grid_gave_model_side": _mean_chance(part, "exp_margin"),
            "grid_gave_blend_side": _mean_chance(part, "blend_margin"),
        }
    big_games = [g for g in games if abs(g["book_margin"]) >= big]
    by_spread = {}
    for low, high in SPREAD_BANDS[sport]:
        part = [g for g in games if low <= abs(g["book_margin"]) < high]
        by_spread[f"{low}-{high}"] = {
            "underdog": _dog_record(part),
            "model_side": _record(part),
            "early_underdog": _dog_record([g for g in part if g["early"]]),
        }
    few = [g for g in big_games if min(g["home_games"], g["away_games"]) < CARD_BIG_LINE_GAMES]
    many = [g for g in big_games if min(g["home_games"], g["away_games"]) >= CARD_BIG_LINE_GAMES]
    return {
        "sport": sport,
        "games": len(games),
        "seasons": sorted({g["season"] for g in games}),
        "rmse": {
            "model": _rmse(games, "exp_margin"),
            "blend": _rmse(games, "blend_margin"),
            "close": _rmse(games, "book_margin"),
        },
        "by_gap": by_gap,
        "by_spread": by_spread,
        "big_line": big,
        "big_spreads": {
            "all": _dog_record(big_games),
            "model_side": _record(big_games),
            "blend_side": _record(big_games, "blend_margin"),
            f"under_{CARD_BIG_LINE_GAMES}_games": {
                **_dog_record(few),
                "model_side": _record(few),
                "blend_side": _record(few, "blend_margin"),
                "rmse_model": _rmse(few, "exp_margin"),
                "rmse_blend": _rmse(few, "blend_margin"),
                "rmse_close": _rmse(few, "book_margin"),
            },
            f"from_{CARD_BIG_LINE_GAMES}_games": {
                **_dog_record(many),
                "model_side": _record(many),
                "blend_side": _record(many, "blend_margin"),
                "rmse_model": _rmse(many, "exp_margin"),
                "rmse_blend": _rmse(many, "blend_margin"),
                "rmse_close": _rmse(many, "book_margin"),
            },
        },
        "calibration": {
            "model": _calibration(games, "exp_margin"),
            "blend": _calibration(games, "blend_margin"),
        },
    }


def _mean_chance(games: list[dict], key: str) -> float | None:
    chances = []
    for game in games:
        gap = game[key] - game["book_margin"]
        if gap == 0:
            continue
        chance = side_chance(game[key], game["exp_total"], -game["book_margin"])
        if chance is not None:
            chances.append(chance if gap > 0 else 1.0 - chance)
    return sum(chances) / len(chances) if chances else None


def _pct(value: float | None) -> str:
    return "   –  " if value is None else f"{value * 100:5.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "  –  " if value is None else f"{value:5.{digits}f}"


def report(result: dict) -> str:
    lines = [
        f"## {result['sport'].upper()}: {result['games']} games with a closing spread, seasons "
        f"{', '.join(str(s) for s in result['seasons'])}.",
        f"Margin miss: model {_num(result['rmse']['model'])}, blend "
        f"{_num(result['rmse']['blend'])}, close {_num(result['rmse']['close'])}.",
        "",
        "By how far the raw model sat from the close (model's side against the close):",
        "   gap   games  model side ATS   cover  toward   grid gave | blend side ATS   cover"
        "  grid gave | miss model/blend/close",
    ]
    for band, part in result["by_gap"].items():
        m, b, r = part["model_side"], part["blend_side"], part["rmse"]
        lines.append(
            f"{band:>6} {part['games']:6d}  {m['wins']:4d}-{m['losses']:<4d}-{m['pushes']:<3d}"
            f" {_pct(m['cover'])} {_num(m['toward_model']):>6}  "
            f"{_pct(part['grid_gave_model_side'])}"
            f"  | {b['wins']:4d}-{b['losses']:<4d}-{b['pushes']:<3d} {_pct(b['cover'])}"
            f"  {_pct(part['grid_gave_blend_side'])}  | {_num(r['model'])}/{_num(r['blend'])}/"
            f"{_num(r['close'])}"
        )
    lines += ["", "By the size of the closing spread (the underdog against the close):"]
    lines.append("  spread  games  dog ATS          cover | early dog cover | model side cover")
    for band, part in result["by_spread"].items():
        d, e, m = part["underdog"], part["early_underdog"], part["model_side"]
        lines.append(
            f"{band:>7} {d['games']:6d}  {d['wins']:4d}-{d['losses']:<4d}-{d['pushes']:<3d}"
            f" {_pct(d['cover'])} | {_pct(e['cover'])} ({e['games']:4d}) | {_pct(m['cover'])}"
        )
    big = result["big_spreads"]
    lines += [
        "",
        f"Spreads of {result['big_line']:g} or more (CARD_BIG_LINE_SIGMAS of the sport's spread):",
        f"  all {big['all']['games']} games: the dog covered {_pct(big['all']['cover'])}, "
        f"the model's side {_pct(big['model_side']['cover'])}, the blend's side "
        f"{_pct(big['blend_side']['cover'])}.",
    ]
    for key, label in (
        (f"under_{CARD_BIG_LINE_GAMES}_games", f"under {CARD_BIG_LINE_GAMES} games a side"),
        (f"from_{CARD_BIG_LINE_GAMES}_games", f"{CARD_BIG_LINE_GAMES} games a side or more"),
    ):
        part = big[key]
        lines.append(
            f"  {label}: {part['games']} games, dog {_pct(part['cover'])}, model side "
            f"{_pct(part['model_side']['cover'])} (toward model "
            f"{_num(part['model_side']['toward_model'])}), blend side "
            f"{_pct(part['blend_side']['cover'])}; miss model {_num(part['rmse_model'])}, "
            f"blend {_num(part['rmse_blend'])}, close {_num(part['rmse_close'])}."
        )
    lines += ["", "A side the grid gives X against the close comes in Y (model | blend):"]
    cal = result["calibration"]
    for name in sorted(set(cal["model"]) | set(cal["blend"]), key=lambda k: int(k.split("-")[0])):
        m, b = cal["model"].get(name), cal["blend"].get(name)
        lines.append(
            f"  {name:>7}%  model {_pct(m and m['given'])} -> {_pct(m and m['came_in'])}"
            f" ({(m or {}).get('games', 0):5d}) | blend {_pct(b and b['given'])} -> "
            f"{_pct(b and b['came_in'])} ({(b or {}).get('games', 0):5d})"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sports", nargs="+", default=list(SPORTS))
    parser.add_argument("--lines", type=Path, default=None, help="a lines file, one sport")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--sample", type=int, default=None)
    args = parser.parse_args()
    results = {}
    for sport in args.sports:
        path = args.lines or lines_path(sport)
        if not path.exists():
            print(f"## {sport.upper()}: no lines file at {path}\n")
            continue
        results[sport] = grade(load(path, args.sample, sport), sport)
        print(report(results[sport]))
        print()
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
