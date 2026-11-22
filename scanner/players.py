"""Player projections: each player's line for an upcoming game, as a whole distribution.

What it does, in one breath. A player's rate for a stat is what he has produced
per minute (per second on the ice in hockey, per game in football, where the
box score gives no playing time), shrunk toward the league's rate for his
position while his games are few. His playing time is projected from his last
few games. The game projection says how the team's scoring should differ from
its average against this opponent at this pace, and that scales the rate. The
stat is then a negative binomial around that mean: a Poisson count whose rate
is itself uncertain, with a dispersion per stat that makes a 25-point scorer
spread over the 15-to-35 range real games show, mixed over the uncertainty in
his minutes. The distribution is computed exactly for the diary (fast enough
for every player of every game each pass) and drawn 100,000 times for the
pages, from the same parameters, so the pictures are the model's own shape.

Only players who played in their team's last game are projected: a man who sat
out is most likely hurt, and the box score cannot say. Goalies are projected as
if they start. Every projected line is graded when the game lands: the miss
against the actual, whether the actual fell above the median, whether it fell
inside the 80% band, and where it fell in the distribution, so the scorecard
can say whether the bands mean what they claim. Nothing here is a
recommendation and nothing is sent or placed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from scanner.config import PLAYER_MAX_PMF, PLAYER_MIN_GAMES, SIM_DRAWS

MODEL_VERSION = "p1"

# Each sport's projected stats: the box-score key, and how many players a team
# gets projected for it (the leaders by projected mean). Exposure is the stat
# the rate is per (minutes, seconds on ice); None means per game.
SPORT_STATS: dict[str, dict[str, int]] = {
    "nba": {"points": 8, "rebounds": 8, "assists": 8, "three_made": 6},
    "cbb": {"points": 7, "rebounds": 7, "assists": 5},
    "nhl": {"shots": 10, "goals": 10, "assists": 10, "saves": 1},
    "nfl": {"pass_yards": 1, "rush_yards": 2, "rec_yards": 4, "receptions": 4, "pass_td": 1},
    "cfb": {"pass_yards": 1, "rush_yards": 2, "rec_yards": 4, "receptions": 4, "pass_td": 1},
}
EXPOSURE: dict[str, str | None] = {
    "nba": "minutes",
    "cbb": "minutes",
    "nhl": "toi",
    "nfl": None,
    "cfb": None,
}
EXPOSURE_CAP = {"nba": 48.0, "cbb": 40.0, "nhl": 65 * 60.0}
EXPOSURE_FLOOR_SD = {"nba": 4.0, "cbb": 4.0, "nhl": 120.0}
# Shrinkage: how much league-average play is added to a player's own before
# his rate is trusted, in exposure units (or games, for football). Fit on a sample
# of 2025-26 box scores (scripts/fit_players.py): basketball's miss is flat from
# half to double these; hockey's falls a little with more; football's falls as the
# prior lightens now that it is shrunk toward its role's rate rather than toward
# every man who touched the ball (two games: the college passing miss 77 yards
# against 80 at four, the NFL's 59 against 60).
PRIOR_EXPOSURE = {"nba": 72.0, "cbb": 60.0, "nhl": 7200.0, "nfl": 2.0, "cfb": 2.0}
# The negative binomial's dispersion per stat: variance = mean + mean² / k.
# Smaller is wider. Set by hand, then moved to what the first days of grading
# showed (7 November 2026, 3,400 lines): basketball and hockey counting stats
# were too wide (90-96% of actuals inside the 80% band), points a touch narrow
# (75%), football yards far too narrow (27-57%). A stat near zero (goals) is
# close to Poisson whatever k is. The scorecard's "inside the band" is the dial.
DISPERSION: dict[str, float] = {
    "points": 10.0,
    "rebounds": 14.0,
    "assists": 12.0,
    "three_made": 8.0,
    "shots": 16.0,
    "goals": 3.0,
    "saves": 15.0,
    "pass_yards": 5.0,
    "rush_yards": 2.5,
    "rec_yards": 2.0,
    "receptions": 8.0,
    "pass_td": 4.0,
}
# Where a sport's own grading disagrees with the shared dial, its own number wins
# (fit by maximum likelihood on a sample of 2025-26 box scores, scripts/fit_players.py).
DISPERSION_BY_SPORT: dict[str, dict[str, float]] = {
    # Basketball's counting stats were too wide (87-93% inside the 80% band), its points
    # too narrow in the tails; college more so, where foul trouble and routs cut minutes.
    "nba": {"points": 6.0, "rebounds": 20.0, "assists": 20.0, "three_made": 20.0},
    "cbb": {"points": 4.0, "rebounds": 8.0, "assists": 8.0},
    "nhl": {"shots": 10.0, "assists": 20.0, "saves": 14.0},
    # Football yards are nearly as wide as they are long: a receiver's game swings by
    # about his average, so the bands at k of 2 held 70% instead of 80%.
    "nfl": {
        "pass_yards": 8.0, "rush_yards": 1.5, "rec_yards": 1.0, "receptions": 10.0, "pass_td": 30.0,
    },
    "cfb": {
        "pass_yards": 3.0, "rush_yards": 1.5, "rec_yards": 1.0, "receptions": 6.0, "pass_td": 30.0,
    },
}  # fmt: skip
# A projected blowout costs the men who play most some of their minutes: in the
# sample, basketball's top-half players by playing time played 3-7% less when the
# projected margin passed nine, and the bench more. The expected playing time of a
# player at or above BLOWOUT_SHARE of the game comes down BLOWOUT_RATE for every
# point of the game's expected margin beyond BLOWOUT_FROM, up to BLOWOUT_MAX; the
# margin is the blend's, since the book's line is the better blowout forecast.
BLOWOUT_FROM = 3.0
BLOWOUT_RATE = 0.004
BLOWOUT_MAX = 0.08
BLOWOUT_SHARE = 0.6
# Which game-level factor scales a stat: the team's projected scoring against its
# average (opponent and pace together), or the game's pace alone.
TEAM_SCALED = {
    "points",
    "assists",
    "three_made",
    "shots",
    "goals",
    "pass_yards",
    "rush_yards",
    "rec_yards",
    "receptions",
    "pass_td",
}
PACE_SCALED = {"rebounds"}
FACTOR_CLAMP = (0.7, 1.4)
RECENT_GAMES = 5  # playing time is projected from this many recent games
RECENT_WEIGHTS = (0.35, 0.25, 0.2, 0.1, 0.1)
EXPOSURE_GRID = 15  # the mixture over playing time uses this many quantiles
GOALIE = "G"
# A man whose chance of missing the game (from the injury report) is at least this
# gets no line: out, doubtful and injured reserve. Questionable and day-to-day keep theirs.
ABSENT_FROM = 0.75


@dataclass
class PlayerSeason:
    player_id: str
    name: str
    team_id: str
    team_abbr: str
    position: str | None
    games: list[dict] = field(default_factory=list)  # oldest first, played games only
    last_team_game: str | None = None  # the team's latest game id
    played_last: bool = False

    def total(self, stat: str) -> float:
        return float(sum(g["stats"].get(stat) or 0 for g in self.games))

    def exposure_total(self, key: str | None) -> float:
        if key is None:
            return float(len(self.games))
        return float(sum(g["stats"].get(key) or 0 for g in self.games))


def seasons(rows: list[dict], sport: str) -> dict[str, PlayerSeason]:
    """Every player's season from the diary's player lines, with who played his team's last game."""
    out: dict[str, PlayerSeason] = {}
    last_game: dict[str, tuple[str, str]] = {}  # team -> (date, game_id)
    for row in rows:
        key = (row["date"], row["game_id"])
        if row["team_id"] not in last_game or key > last_game[row["team_id"]]:
            last_game[row["team_id"]] = key
    for row in rows:
        season = out.get(row["player_id"])
        if season is None:
            season = PlayerSeason(
                row["player_id"], row["name"], row["team_id"], row["team_abbr"], row["position"]
            )
            out[row["player_id"]] = season
        # A traded player keeps his lines; his team is wherever he played last.
        season.team_id, season.team_abbr = row["team_id"], row["team_abbr"]
        if row["position"]:
            season.position = row["position"]
        if row["played"]:
            season.games.append(row)
    for season in out.values():
        _, game_id = last_game.get(season.team_id, (None, None))
        season.last_team_game = game_id
        season.played_last = any(g["game_id"] == game_id for g in season.games)
    return out


def _role_players(seasons_by_id: dict[str, PlayerSeason], sport: str) -> dict[str, set[str]]:
    """Per stat, the players in the role the lines are for: each team's leaders by the
    stat, as many as the sport projects a side. Football's box scores carry no playing
    time or position, so without this a starting quarterback would be shrunk toward
    the rate of every man who threw a pass, backups and trick plays included, and
    the sample showed the lines running 10 to 60 yards light."""
    out: dict[str, set[str]] = {}
    by_team: dict[str, list[PlayerSeason]] = {}
    for season in seasons_by_id.values():
        by_team.setdefault(season.team_id, []).append(season)
    for stat, count in SPORT_STATS[sport].items():
        chosen = out.setdefault(stat, set())
        for squad in by_team.values():
            ranked = sorted((s for s in squad if s.total(stat) > 0), key=lambda s: -s.total(stat))
            chosen.update(s.player_id for s in ranked[:count])
    return out


def league_rates(seasons_by_id: dict[str, PlayerSeason], sport: str) -> dict[str, dict[str, float]]:
    """Per-position (and overall, under "") rates per exposure unit for each projected stat.

    Per game (football), the rate is the role's: each team's leaders in the stat.
    """
    exposure = EXPOSURE[sport]
    roles = _role_players(seasons_by_id, sport) if exposure is None else {}
    totals: dict[str, dict[str, list[float]]] = {}
    for season in seasons_by_id.values():
        for position in ("", season.position or ""):
            bucket = totals.setdefault(position, {})
            for stat in SPORT_STATS[sport]:
                if stat == "saves" and (season.position or "") != GOALIE:
                    continue
                if stat != "saves" and (season.position or "") == GOALIE and sport == "nhl":
                    continue
                made = season.total(stat)
                if exposure is None and (made <= 0 or season.player_id not in roles[stat]):
                    continue  # per game: the league rate is the role's
                sums = bucket.setdefault(stat, [0.0, 0.0])
                sums[0] += made
                sums[1] += season.exposure_total(exposure)
    out: dict[str, dict[str, float]] = {}
    for position, stats in totals.items():
        out[position] = {
            stat: (made / played) if played > 0 else 0.0 for stat, (made, played) in stats.items()
        }
    return out


def dispersion_for(sport: str, stat: str) -> float:
    return DISPERSION_BY_SPORT.get(sport, {}).get(stat, DISPERSION[stat])


def blowout_factor(sport: str, projection: dict | None, exposure_mu: float) -> float:
    """How much of his usual playing time a heavy-minutes player keeps in a projected rout."""
    if sport not in EXPOSURE_CAP or projection is None:
        return 1.0
    if exposure_mu < BLOWOUT_SHARE * EXPOSURE_CAP[sport]:
        return 1.0
    part = projection.get("blend") or projection.get("raw") or {}
    margin = part.get("margin")
    if margin is None:
        return 1.0
    return 1.0 - min(BLOWOUT_RATE * max(abs(float(margin)) - BLOWOUT_FROM, 0.0), BLOWOUT_MAX)


def _weighted_recent(values: list[float]) -> float:
    recent = list(reversed(values[-RECENT_GAMES:]))
    weights = RECENT_WEIGHTS[: len(recent)]
    return sum(v * w for v, w in zip(recent, weights, strict=False)) / sum(weights)


def _sd(values: list[float], floor: float) -> float:
    if len(values) < 2:
        return floor
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return max(math.sqrt(var), floor)


def exposure_projection(season: PlayerSeason, sport: str) -> tuple[float, float]:
    """(expected playing time, its spread) from the player's recent games; (1, 0) per game."""
    key = EXPOSURE[sport]
    if key is None:
        return 1.0, 0.0
    values = [float(g["stats"].get(key) or 0) for g in season.games]
    if not values:
        return 0.0, EXPOSURE_FLOOR_SD[sport]
    mu = _weighted_recent(values)
    sd = _sd(values[-10:], EXPOSURE_FLOOR_SD[sport])
    return min(mu, EXPOSURE_CAP[sport]), sd


def rate_for(
    season: PlayerSeason, stat: str, sport: str, rates: dict[str, dict[str, float]]
) -> tuple[float, float]:
    """(the player's shrunk rate per exposure unit, the league rate it was shrunk toward)."""
    league = rates.get(season.position or "", {}).get(stat)
    if league is None or league == 0.0:
        league = rates.get("", {}).get(stat, 0.0)
    prior = PRIOR_EXPOSURE[sport]
    made = season.total(stat)
    played = season.exposure_total(EXPOSURE[sport])
    return (made + prior * league) / (played + prior), league


def game_factors(projection: dict | None, slate: dict, records_avg: dict) -> dict[str, dict]:
    """Per side: how the team's projected scoring and the game's pace compare with the usual.

    ``records_avg`` carries each team's average points this season and the
    league's average per side; without a projection both factors are 1.
    """
    out = {}
    raw = (projection or {}).get("raw") or {}
    league = (projection or {}).get("league") or {}
    league_points = league.get("points") or records_avg.get("league")
    for side in ("home", "away"):
        team_id = slate[side]["team_id"]
        team_avg = records_avg.get(team_id)
        projected = raw.get(side)
        team = 1.0
        if projected is not None and team_avg:
            team = min(max(projected / team_avg, FACTOR_CLAMP[0]), FACTOR_CLAMP[1])
        pace = 1.0
        if raw.get("total") is not None and league_points:
            pace = min(max(raw["total"] / (2.0 * league_points), FACTOR_CLAMP[0]), FACTOR_CLAMP[1])
        out[team_id] = {"team": team, "pace": pace}
    return out


# -- the distribution ------------------------------------------------------------------------


def _nb_log_pmf(x: int, mean: float, k: float) -> float:
    if mean <= 0:
        return 0.0 if x == 0 else -math.inf
    p = k / (k + mean)
    return (
        math.lgamma(x + k)
        - math.lgamma(k)
        - math.lgamma(x + 1)
        + k * math.log(p)
        + x * math.log(1.0 - p)
    )


def _exposure_grid(mu: float, sd: float, cap: float) -> list[tuple[float, float]]:
    """(playing time, weight) at the quantiles of a normal, clipped to what a game allows."""
    if sd <= 0:
        return [(max(min(mu, cap), 0.0), 1.0)]
    from statistics import NormalDist

    dist = NormalDist(mu, sd)
    weight = 1.0 / EXPOSURE_GRID
    return [
        (max(min(dist.inv_cdf((i + 0.5) / EXPOSURE_GRID), cap), 0.0), weight)
        for i in range(EXPOSURE_GRID)
    ]


def distribution(params: dict) -> dict[int, float]:
    """The stat's probability for each value, mixed over the player's playing time."""
    rate, k, factor = params["rate"], params["k"], params["factor"]
    grid = _exposure_grid(params["exposure_mu"], params["exposure_sd"], params["exposure_cap"])
    means = [(rate * e * factor, w) for e, w in grid]
    top = max(m for m, _ in means)
    limit = int(top + 6 * math.sqrt(top + top * top / k) + 5)
    pmf: dict[int, float] = {}
    for mean, w in means:
        if mean <= 0:
            pmf[0] = pmf.get(0, 0.0) + w
            continue
        for x in range(limit + 1):
            prob = math.exp(_nb_log_pmf(x, mean, k)) * w
            if prob < PLAYER_MAX_PMF and x > mean:
                break
            pmf[x] = pmf.get(x, 0.0) + prob
    total = sum(pmf.values())
    return {x: p / total for x, p in sorted(pmf.items())}


def summarise_pmf(pmf: dict[int, float]) -> dict:
    """Mean, median, the 80% band and the spread of a distribution."""
    mean = sum(x * p for x, p in pmf.items())
    var = sum((x - mean) ** 2 * p for x, p in pmf.items())
    cumulative = 0.0
    quantiles = {}
    for x, p in pmf.items():
        cumulative += p
        for name, q in (("p10", 0.1), ("median", 0.5), ("p90", 0.9)):
            if name not in quantiles and cumulative >= q:
                quantiles[name] = x
    return {"mean": mean, "sd": math.sqrt(var), **quantiles}


def chance_over(pmf: dict[int, float], line: float) -> dict[str, float]:
    """The share above, on and below a line (a .5 line never pushes)."""
    above = sum(p for x, p in pmf.items() if x > line)
    push = sum(p for x, p in pmf.items() if x == line)
    return {"above": above, "push": push, "below": max(1.0 - above - push, 0.0)}


def pit(pmf: dict[int, float], actual: int) -> float:
    """Where the actual fell in the distribution, half of its own bar counted: 0 low, 1 high."""
    below = sum(p for x, p in pmf.items() if x < actual)
    return below + 0.5 * pmf.get(actual, 0.0)


# -- the simulation ------------------------------------------------------------------------


def _poisson(lam: float, rng: random.Random) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit = math.exp(-lam)
    count, product = 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def simulate_player(params: dict, draws: int = SIM_DRAWS, seed: int | None = None) -> dict:
    """Draw the player's games: playing time, then the stat as a Poisson with an uncertain rate.

    The same model as :func:`distribution`, played out rather than summed, for
    the pages' pictures. Returns the histogram and the summary the draws give.
    """
    rng = random.Random(seed)
    rate, k, factor = params["rate"], params["k"], params["factor"]
    mu, sd, cap = params["exposure_mu"], params["exposure_sd"], params["exposure_cap"]
    hist: dict[int, int] = {}
    for _ in range(draws):
        exposure = max(min(rng.gauss(mu, sd), cap), 0.0) if sd > 0 else max(min(mu, cap), 0.0)
        mean = rate * exposure * factor
        if mean <= 0:
            value = 0
        else:
            lam = rng.gammavariate(k, mean / k)
            value = _poisson(lam, rng)
        hist[value] = hist.get(value, 0) + 1
    pmf = {x: n / draws for x, n in sorted(hist.items())}
    return {"draws": draws, "seed": seed, "hist": {str(x): n for x, n in sorted(hist.items())},
            **summarise_pmf(pmf)}  # fmt: skip


# -- projecting a game ---------------------------------------------------------------------


def project_game(
    sport: str,
    slate: dict,
    kickoff: datetime,
    projection: dict | None,
    player_rows: list[dict],
    records_avg: dict,
    now: datetime,
    availability: dict[str, float] | None = None,
    starting: set[str] | None = None,
) -> list[dict]:
    """Every projected line for a game: one row per (player, stat), the leaders by mean.

    ``player_rows`` are the diary's player lines for the sport's season,
    ``records_avg`` each team's average points and the league's per side, and
    ``availability`` each listed man's chance of missing the game from the injury
    report (scanner.injuries): a man likely out gets no line, and in the sports with
    playing time his minutes go to the men who stay, in proportion to their own; a man
    in doubt keeps his line, marked with the chance. ``starting`` names the men reported
    to start (hockey's goalie): they get a line whether or not they played last time.
    """
    stats = SPORT_STATS.get(sport)
    if not stats:
        return []
    availability = availability or {}
    starting = starting or set()
    by_id = seasons(player_rows, sport)
    rates = league_rates(by_id, sport)
    factors = game_factors(projection, slate, records_avg)
    exposure_key = EXPOSURE[sport]
    cap = EXPOSURE_CAP.get(sport, 1.0)
    out: list[dict] = []
    for side in ("home", "away"):
        team = slate[side]
        team_id = team["team_id"]
        opponent = slate["away" if side == "home" else "home"]
        regulars = [
            s
            for s in by_id.values()
            if s.team_id == team_id
            and (s.played_last or s.player_id in starting)
            and len(s.games) >= PLAYER_MIN_GAMES
        ]
        squad = [s for s in regulars if availability.get(s.player_id, 0.0) < ABSENT_FROM]
        boost = 1.0
        if exposure_key is not None:
            absent = sum(exposure_projection(s, sport)[0] for s in regulars if s not in squad)
            staying = sum(exposure_projection(s, sport)[0] for s in squad)
            if absent > 0 and staying > 0:
                boost = 1.0 + absent / staying
        candidates: dict[str, list[dict]] = {stat: [] for stat in stats}
        for season in squad:
            is_goalie = (season.position or "") == GOALIE and sport == "nhl"
            mu, sd = exposure_projection(season, sport)
            if mu <= 0:
                continue
            rout = blowout_factor(sport, projection, mu)
            mu *= rout
            if boost > 1.0 and not is_goalie:
                mu = min(mu * boost, cap)
            for stat in stats:
                if (stat == "saves") != is_goalie:
                    continue
                if exposure_key is None and season.total(stat) <= 0:
                    continue  # football: only the men who have recorded the stat
                rate, league = rate_for(season, stat, sport, rates)
                factor = factors[team_id]["team"] if stat in TEAM_SCALED else 1.0
                if stat in PACE_SCALED:
                    factor = factors[team_id]["pace"]
                if is_goalie:
                    # A goalie faces the other side's shots: scale by their scoring instead.
                    factor = factors[opponent["team_id"]]["team"]
                params = {
                    "rate": rate,
                    "league_rate": league,
                    "exposure_mu": mu,
                    "exposure_sd": sd,
                    "exposure_cap": cap,
                    "factor": factor,
                    "k": dispersion_for(sport, stat),
                    "games": len(season.games),
                    "prior": PRIOR_EXPOSURE[sport],
                    "blowout": rout,
                    "boost": round(boost, 3),
                    "availability": availability.get(season.player_id, 0.0),
                }
                pmf = distribution(params)
                summary = summarise_pmf(pmf)
                candidates[stat].append(
                    {
                        "sport": sport,
                        "game_id": slate["game_id"],
                        "player_id": season.player_id,
                        "stat": stat,
                        "team_id": team_id,
                        "team_abbr": team["abbreviation"],
                        "opponent_abbr": opponent["abbreviation"],
                        "home": side == "home",
                        "name": season.name,
                        "position": season.position,
                        "kickoff": kickoff,
                        "model": MODEL_VERSION,
                        "params": params,
                        "pmf": {str(x): p for x, p in pmf.items()},
                        **summary,
                    }
                )
        for stat, limit in stats.items():
            ranked = sorted(candidates[stat], key=lambda r: -r["mean"])[:limit]
            out.extend(ranked)
    return out


def team_averages(records: list, sport: str) -> dict:
    """Each team's average points this season and the league's per side, for the factors."""
    points: dict[str, list[int]] = {}
    for record in records:
        points.setdefault(record.home.team_id, []).append(record.home_score)
        points.setdefault(record.away.team_id, []).append(record.away_score)
    out = {team: sum(v) / len(v) for team, v in points.items() if v}
    everything = [p for v in points.values() for p in v]
    out["league"] = sum(everything) / len(everything) if everything else None
    return out


# -- grading ---------------------------------------------------------------------------------


def grade_line(row: dict, actual: int | None) -> dict:
    """How a projected line did: None actual means the player did not play."""
    if actual is None:
        return {"outcome": "did_not_play"}
    pmf = {int(x): p for x, p in row["pmf"].items()}
    return {
        "outcome": "graded",
        "actual": actual,
        "error": actual - row["mean"],
        "abs_error": abs(actual - row["mean"]),
        "over_median": actual > row["median"],
        "on_median": actual == row["median"],
        "in_band": row["p10"] <= actual <= row["p90"],
        "pit": pit(pmf, actual),
    }


def summarise_grades(rows: list[dict]) -> dict:
    """The scorecard's numbers over graded lines: the miss, the bias and the calibration by stat."""
    graded = [r for r in rows if r.get("outcome") == "graded"]

    def tally(subset: list[dict]) -> dict:
        n = len(subset)
        if not n:
            return {"graded": 0}
        over = sum(1 for r in subset if r["over_median"])
        on = sum(1 for r in subset if r.get("on_median"))
        # A discrete stat lands on its median often (a goal line of 0 or 1 nearly
        # always), so the honest "above the median" counts those ties as half.
        deciles = [0] * 10
        for r in subset:
            deciles[min(int(r["pit"] * 10), 9)] += 1
        return {
            "graded": n,
            "mae": sum(r["abs_error"] for r in subset) / n,
            "bias": sum(r["error"] for r in subset) / n,
            "over_median": over / n,
            "on_median": on / n,
            "centred": (over + 0.5 * on) / n,
            "in_band": sum(1 for r in subset if r["in_band"]) / n,
            "pit_deciles": [d / n for d in deciles],
        }

    return {
        "projected": len(rows),
        "did_not_play": sum(1 for r in rows if r.get("outcome") == "did_not_play"),
        **tally(graded),
        "by_sport": {
            s: {
                "graded": sum(1 for r in graded if r["sport"] == s),
                "by_stat": {
                    stat: tally([r for r in graded if r["sport"] == s and r["stat"] == stat])
                    for stat in SPORT_STATS.get(s, {})
                    if any(r["sport"] == s and r["stat"] == stat for r in graded)
                },
            }
            for s in sorted({r["sport"] for r in graded})
        },
    }
