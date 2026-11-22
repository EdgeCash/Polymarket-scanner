"""Write college basketball's roster prior into scanner/priors.json.

    python scripts/build_roster_priors.py --raw DIR [--season 2027]

``scripts/build_priors.py`` gives every sport last season's final ratings as its
prior. For college basketball that is a poor guide on its own (two fifths of a
side's minutes return in a usual year), and ``scripts/fit_roster_prior.py`` shows
what does better: the rating split by who is back, the share of minutes back, the
newcomers and what both groups produced. This script fits that on every season in
``DIR`` (the ``cbb_<season>.jsonl`` files of scripts/bulk_box_scores.py), takes each
side's roster for ``--season`` from who has played for it so far that season, and
replaces the ``cbb`` entry of scanner/priors.json with the forecast: each side's
predicted net rating on last season's scoring level, marked ``weighted`` so the
scanner takes it whole instead of at its usual share. A side with too little on
record keeps the plain prior at its usual weight, and the default for a side the
file does not know is carried over from the entry it replaces (run
``build_priors.py`` first).

``--season`` is the season to build for, by the year it ends in; the default is the
latest in ``DIR``. Run it once the season is a week or two old, so the rosters have
shown themselves. The running scanner never imports this script.
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
import fit_roster_prior as roster  # noqa: E402

from scanner.projection import tuning_for  # noqa: E402

OUT = ROOT / "scanner" / "priors.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    seasons, by_season = roster.load_seasons(args.raw)
    target = args.season or max(seasons)
    before = seasons.get(target - 1)
    if before is None or target not in seasons:
        raise SystemExit(f"need cbb_{target - 1}.jsonl and cbb_{target}.jsonl in {args.raw}")
    # Fit on every completed pair of seasons; the target season only lends its rosters.
    rows = [r for r in roster.table(seasons) if r["season"] < target]
    if len(rows) < 300:
        raise SystemExit(f"only {len(rows)} team-seasons to fit on; add earlier seasons")
    beta = roster.fit_form(rows, roster.FEATURES)
    # Ten games last season to have a rating of one's own, as scripts/build_priors.py asks.
    prior = roster.roster_prior(
        before,
        seasons[target].roster,
        beta,
        roster.FEATURES,
        min_games=10,
        conferences=seasons[target].conference,
    )
    existing = json.loads(args.out.read_text()) if args.out.exists() else {}
    old = existing.get("cbb") or {}
    weight = tuning_for("cbb").prior_weight
    labels = {}
    for record in by_season[target - 1]:
        for team in (record.home, record.away):
            labels[team.team_id] = team.abbreviation
    teams = {}
    moved = []
    for team, v in prior["teams"].items():
        f = roster.features(
            before,
            team,
            seasons[target].roster.get(team, set()),
            seasons[target].conference.get(team),
        )
        teams[team] = {
            "offense": round(v["offense"], 3),
            "defense": round(v["defense"], 3),
            "abbr": labels.get(team),
            "name": (old.get("teams") or {}).get(team, {}).get("name"),
            "returning": None if f is None else round(f["share"], 2),
            "arrivals": None if f is None else round(f["arrivals"], 2),
        }
        if f is not None:
            moved.append((v["offense"] - v["defense"]) - weight * f["rating"])
    unknown = old.get("unknown") or before.unknown
    # A second run finds its own entry there, whose default is already at the weight.
    unknown_weight = 1.0 if old.get("weighted") else weight
    existing["cbb"] = {
        "season": target - 1,
        "built": date.today().isoformat(),
        "weighted": True,
        "games": len(by_season[target - 1]),
        "league_points": old.get("league_points"),
        "strangers": old.get("strangers"),
        "teams": teams,
        "unknown": {k: round(unknown_weight * float(v), 3) for k, v in unknown.items()},
        "roster": {
            "fit_on": sorted({r["season"] for r in rows}),
            "team_seasons": len(rows),
            "coefficients": {
                name: round(float(b), 4)
                for name, b in zip(("const", *roster.FEATURES), beta, strict=True)
            },
        },
    }
    args.out.write_text(json.dumps(existing, indent=1, sort_keys=True) + "\n")
    spread = (sum(m * m for m in moved) / len(moved)) ** 0.5 if moved else 0.0
    print(
        f"cbb: roster prior for {target} from {len(rows)} team-seasons; {len(teams)} sides, "
        f"{len(moved)} moved by their rosters, {spread:.1f} points rms against the plain prior"
    )
    print("wrote", args.out)


if __name__ == "__main__":
    main()
