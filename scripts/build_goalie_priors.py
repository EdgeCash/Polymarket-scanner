"""Build scanner/goalie_priors.json: every goalie's record before this season.

    python scripts/build_goalie_priors.py --raw DIR --season 2026

A save rate takes thousands of shots to mean much, and a season gives a starter
fifteen hundred, so scanner.goalies rates a goalie on this season's shots plus his
earlier seasons' at a discount. This script adds up those earlier seasons from the
box scores scripts/fetch_box_scores.py gathered (``DIR/nhl_<season>.jsonl``): the
named season in full, and each one before it at scanner.goalies.CARRY again, so the
file holds one faded total per goalie, keyed by ESPN's athlete id. ``--season`` is
the last completed season by the year it ends in; the scanner uses the file only
for the season after it.

The running scanner never imports this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_goalies  # noqa: E402
import fit_injuries  # noqa: E402

import scanner.goalies as goalies  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--out", type=Path, default=goalies.PRIORS_PATH)
    args = parser.parse_args()
    records = [r for r in fit_injuries.load(args.raw, "nhl") if (r.season or 0) <= args.season]
    seasons = sorted({r.season for r in records})
    if args.season not in seasons:
        raise SystemExit(f"no nhl_{args.season}.jsonl in {args.raw}")
    career: dict[str, tuple[float, float]] = {}
    names: dict[str, str] = {}
    for season in seasons:
        rows = []
        for record in records:
            if record.season != season:
                continue
            for row in fit_injuries.player_rows(record):
                if row["position"] == goalies.GOALIE:
                    rows.append(row)
                    names[row["player_id"]] = row["name"]
        career = fit_goalies.season_career(rows, career)
    out = {
        "season": args.season,
        "built": date.today().isoformat(),
        "seasons": seasons,
        "goalies": {
            pid: {"name": names.get(pid, ""), "saves": round(s, 1), "shots": round(n, 1)}
            for pid, (s, n) in sorted(career.items())
            if n >= 50
        },
    }
    args.out.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(out['goalies'])} goalies from seasons {seasons} written to {args.out}")


if __name__ == "__main__":
    main()
