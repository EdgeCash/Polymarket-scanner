"""Fit a roster-based prior for college basketball: who is back, and who has arrived.

    python scripts/fit_roster_prior.py --raw DIR [--walk] [--pick N]

A college side is not last season's side: two fifths of its minutes come back in a
usual year, and the transfer portal moves the rest around. Last season's rating,
which is what scanner/priors.json holds, knows none of that. The box scores do:
every player carries ESPN's athlete id from one season and one team to the next
(scripts/bulk_box_scores.py), so for each side the history says how much of last
season's minutes are back, how much those men produced, and what the newcomers
did where they were.

For each team-season this script builds, from the season before:

- the side's rating, split by the share of its minutes that return;
- that share itself;
- the newcomers, in starter-equivalents (a man who played a fifth of his old
  side's minutes is one), and the same weighted by the old side's rating;
- the production a minute, against the league's, of the men returning and of the
  men arriving, in starter-equivalents;
- the level of the side's conference, for the share of minutes that left: a side
  that turns over restocks from its league's pool, not from the country's.

and fits the side's final rating on them, leaving each season out in turn. The
first table is how well each form predicts a season's final rating on the season
left out. ``--walk`` then plays whole seasons through the scanner's own ratings with
the roster prior in place of the plain one, at several weights, and scores every
game's margin, early and over the season; that is the test a prior has to pass.
``scripts/build_roster_priors.py`` writes the prior the fit recommends.

Who is on a side is taken from who played for it that season, which is known in
November only for the men who have appeared; the build script uses the same rule
on the season so far. The running scanner never imports this script; it needs the
``train`` extras.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_injuries  # noqa: E402
import fit_ratings  # noqa: E402

ROSTER = ("rating_back", "rating_gone", "share", "arrivals", "arrivals_rating",
          "arrivals_production", "back_production")  # fmt: skip
FEATURES = (*ROSTER, "conference_gone")
FORMS = {
    "last season's rating": ("rating",),
    "split by who returns": ("rating_back", "rating_gone", "share"),
    "and who arrives": ("rating_back", "rating_gone", "share", "arrivals", "arrivals_rating"),
    "and what they produced": ROSTER,
    "and the conference's level": FEATURES,
}
MIN_TEAM_MINUTES = 4000.0  # a side with less on record last season is not rated by its roster
UNKNOWN_OLD_RATING = -8.0  # a newcomer from a side the history does not rate (a lower division)
TOTAL_WEIGHT = 0.9  # the share of last season's scoring level a side keeps, as before
STARTERS = 5.0  # starter-equivalents a side can field


@dataclass
class Season:
    """What one season's box scores say, as the next season's prior needs it."""

    season: int
    offense: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    games: dict[str, int] = field(default_factory=dict)
    minutes: dict[tuple[str, str], float] = field(default_factory=dict)  # (player, team)
    production: dict[tuple[str, str], float] = field(default_factory=dict)
    team_minutes: dict[str, float] = field(default_factory=dict)
    roster: dict[str, set[str]] = field(default_factory=dict)
    conference: dict[str, str] = field(default_factory=dict)  # team -> its conference
    unknown: dict = field(default_factory=dict)

    def conference_rating(self, conference: str | None) -> float | None:
        """The mean net rating of a conference's sides this season, or None if unknown."""
        nets = [
            self.offense[t] - self.defense[t]
            for t, c in self.conference.items()
            if c == conference and t in self.offense
        ]
        return sum(nets) / len(nets) if conference is not None and len(nets) >= 4 else None

    @property
    def league_rate(self) -> float:
        total = sum(self.minutes.values())
        return sum(self.production.values()) / total if total else 0.0


def summarise(records, season: int) -> Season:
    """A season's final ratings and every player's minutes and production by team."""
    prior = fit_ratings.final_prior(records, "cbb", season)
    out = Season(season, unknown=prior["unknown"])
    for team, v in prior["teams"].items():
        out.offense[team], out.defense[team] = v["offense"], v["defense"]
        out.games[team] = int(v.get("games") or 0)
    minutes: dict = defaultdict(float)
    production: dict = defaultdict(float)
    team_minutes: dict = defaultdict(float)
    roster: dict = defaultdict(set)
    for record in records:
        for p in record.players:
            if not p.played:
                continue
            m = float(p.stats.get("minutes") or 0)
            key = (p.player_id, p.team_id)
            minutes[key] += m
            team_minutes[p.team_id] += m
            roster[p.team_id].add(p.player_id)
            production[key] += (
                float(p.stats.get("points") or 0)
                + 0.7 * float(p.stats.get("assists") or 0)
                + 0.5 * float(p.stats.get("rebounds") or 0)
            )
    out.minutes, out.production = dict(minutes), dict(production)
    out.team_minutes, out.roster = dict(team_minutes), dict(roster)
    return out


def features(
    before: Season, team: str, players: set[str], conference: str | None = None
) -> dict | None:
    """One side's roster features for the season after ``before``, or None when the
    side has too little on record to say. ``conference`` is the side's conference in
    the new season (last season's when not given)."""
    if team not in before.offense or before.team_minutes.get(team, 0.0) < MIN_TEAM_MINUTES:
        return None
    rating = before.offense[team] - before.defense[team]
    level = before.conference_rating(conference or before.conference.get(team))
    if level is None:
        level = rating
    league = before.league_rate
    was: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (player, old), m in before.minutes.items():
        if player in players:
            was[player].append((old, m))
    back = arrivals = arrivals_rating = arrivals_production = back_production = 0.0
    for player, stints in was.items():
        for old, m in stints:
            if m <= 0:
                continue
            starters = 5.0 * m / max(before.team_minutes.get(old, 1.0), 1.0)
            above = before.production[(player, old)] / m - league
            if old == team:
                back += m
                back_production += starters * above
            else:
                arrivals += starters
                old_rating = (
                    before.offense[old] - before.defense[old]
                    if old in before.offense
                    else UNKNOWN_OLD_RATING
                )
                arrivals_rating += starters * old_rating
                arrivals_production += starters * above
    share = back / before.team_minutes[team]
    # Five men play at a time: arrivals beyond the minutes the returning men leave open
    # cannot all be used, so they are scaled to what is open (a side that brings in
    # seven starters' worth of transfers does not field seven).
    room = max(STARTERS - STARTERS * share, 0.0)
    if arrivals > room > 0:
        scale = room / arrivals
        arrivals, arrivals_rating = arrivals * scale, arrivals_rating * scale
        arrivals_production *= scale
    return {
        "rating": rating,
        "rating_back": rating * share,
        "rating_gone": rating * (1.0 - share),
        "share": share,
        # What the men who left take with them is replaced at the conference's level:
        # a side that turns over in a strong league restocks from a deeper pool.
        "conference_gone": level * (1.0 - share),
        "arrivals": arrivals,
        "arrivals_rating": arrivals_rating,
        "arrivals_production": arrivals_production,
        "back_production": back_production,
    }


def table(seasons: dict[int, Season]) -> list[dict]:
    rows = []
    for season, now in sorted(seasons.items()):
        before = seasons.get(season - 1)
        if before is None:
            continue
        for team in now.offense:
            f = features(before, team, now.roster.get(team, set()), now.conference.get(team))
            if f is not None:
                rows.append(
                    {
                        "season": season,
                        "team": team,
                        "final": now.offense[team] - now.defense[team],
                        **f,
                    }
                )
    return rows


def fit_form(rows: list[dict], columns) -> np.ndarray:
    x = np.column_stack([np.ones(len(rows))] + [[r[c] for r in rows] for c in columns])
    beta, *_ = np.linalg.lstsq(x, np.array([r["final"] for r in rows]), rcond=None)
    return beta


def predict(beta: np.ndarray, f: dict, columns) -> float:
    return float(beta[0] + sum(b * f[c] for b, c in zip(beta[1:], columns, strict=True)))


def roster_prior(
    before: Season,
    players_by_team: dict[str, set[str]],
    beta,
    columns,
    min_games: int = 0,
    conferences: dict[str, str] | None = None,
) -> dict:
    """The next season's prior in the shape of scanner/priors.json: each side's predicted
    net rating, on last season's scoring level, with last season's default for a stranger.
    A side with fewer than ``min_games`` last season is left out, to be a stranger."""
    teams = {}
    for team in before.offense:
        if before.games.get(team, 0) < min_games:
            continue
        f = features(before, team, players_by_team.get(team, set()), (conferences or {}).get(team))
        level = TOTAL_WEIGHT * (before.offense[team] + before.defense[team])
        if f is None:
            net = TOTAL_WEIGHT * (before.offense[team] - before.defense[team])
        else:
            net = predict(beta, f, columns)
        teams[team] = {"offense": (level + net) / 2.0, "defense": (level - net) / 2.0, "name": team}
    return {
        "season": before.season,
        "teams": teams,
        "unknown": {k: TOTAL_WEIGHT * v for k, v in before.unknown.items()},
        "weighted": True,  # a forecast of this season's ratings: scanner.projection takes it whole
    }


def conferences(raw: Path) -> dict[int, dict[str, str]]:
    """Each side's conference by season, from the bulk records (which carry both)."""
    out: dict[int, dict[str, str]] = defaultdict(dict)
    for path in sorted(raw.glob("cbb_*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                g = json.loads(line)
                for side in ("home", "away"):
                    if g.get(f"{side}_conference") is not None:
                        out[g["season"]][g[f"{side}_id"]] = str(g[f"{side}_conference"])
    return out


def load_seasons(raw: Path) -> tuple[dict[int, Season], dict[int, list]]:
    records = fit_injuries.load(raw, "cbb")
    by_season: dict[int, list] = defaultdict(list)
    for record in records:
        by_season[record.season].append(record)
    leagues = conferences(raw)
    seasons = {}
    for season, games in sorted(by_season.items()):
        seasons[season] = summarise(games, season)
        seasons[season].conference = leagues.get(season, {})
        print(
            f"cbb {season}: {len(games)} games, {len(seasons[season].offense)} sides rated",
            flush=True,
        )
    return seasons, by_season


def rms(values) -> float:
    values = list(values)
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else float("nan")


COMBINED = {"hfa_conference": 3.0, "hfa_nonconference": 5.0, "half_life": 20.0}
WALKS = (
    ("the plain prior, as shipped", None, {}),
    ("roster prior, 4 pseudo-games", "roster", {}),
    ("roster prior, 6 pseudo-games", "roster", {"pseudo_games": 6.0}),
    ("roster prior, 8 pseudo-games", "roster", {"pseudo_games": 8.0}),
    ("roster prior, 3 pseudo-games", "roster", {"pseudo_games": 3.0}),
    # With the rest of what college basketball's fits chose, to see that they add up.
    ("plain prior, split home edge and fade", None, COMBINED),
    ("roster prior, split home edge and fade", "roster", COMBINED),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--walk", action="store_true", help="score whole seasons with the prior")
    parser.add_argument("--pick", type=int, default=None, help="run one of the walks")
    args = parser.parse_args()
    seasons, by_season = load_seasons(args.raw)
    rows = table(seasons)
    years = sorted({r["season"] for r in rows})
    print(f"\n{len(rows)} team-seasons over {years}; mean share of minutes returning "
          f"{np.mean([r['share'] for r in rows]):.2f}, mean arrivals "
          f"{np.mean([r['arrivals'] for r in rows]):.2f} starter-equivalents")  # fmt: skip
    print("a side's final rating, predicted on the season left out (rms miss):")
    for name, columns in FORMS.items():
        misses = []
        for year in years:
            beta = fit_form([r for r in rows if r["season"] != year], columns)
            misses += [r["final"] - predict(beta, r, columns) for r in rows if r["season"] == year]
        beta = fit_form(rows, columns)
        coefs = ", ".join(f"{c} {b:+.3f}" for c, b in zip(("const", *columns), beta, strict=True))
        print(f"  {name:24} {rms(misses):.3f}   every season: {coefs}")
    if not args.walk:
        return
    columns = FEATURES
    priors = {}
    for year in years:
        beta = fit_form([r for r in rows if r["season"] != year], columns)
        priors[year] = roster_prior(
            seasons[year - 1],
            seasons[year].roster,
            beta,
            columns,
            conferences=seasons[year].conference,
        )
    scored = [r for season in years for r in by_season[season]]
    earlier = [r for r in by_season[years[0] - 1]]
    walks = WALKS if args.pick is None else WALKS[args.pick : args.pick + 1]
    print("\nevery game's margin, each season played through the scanner's ratings (rms miss):")
    for name, kind, overrides in walks:
        out = fit_ratings.walk(
            "cbb", earlier + scored, overrides, priors if kind == "roster" else None
        )
        out = [r for r in out if r["season"] in years]
        early = [r["margin"] for r in out if r["day"] <= 45]
        print(
            f"  {name:42} season {rms(r['margin'] for r in out):.3f}   first 45 days "
            f"{rms(early):.3f}   total {rms(r['total'] for r in out):.3f}   "
            f"({len(out)} games, {len(early)} early)",
            flush=True,
        )


if __name__ == "__main__":
    main()
