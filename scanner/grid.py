"""Every final score of one game and its exact chance: the one source of the model's odds.

The projection gives a margin and a total and how widely each spreads. Each
whole-number regulation score (home, away) gets the chance that a normal margin
and a normal total land on it: the margin and the total are home − away and
home + away, so they share a parity, and a margin can be no bigger than the
total. Sports whose results bunch on certain numbers (football's 3 and 7,
basketball's tie after regulation) carry a table, built from real games, of how
much more or less often each regulation margin and each team score happens than
the bell curves say; the grid is multiplied by it. A tie after regulation is
then played out by the sport's overtime. Finally the two bell curves' centres
are moved, the least they need to be, so the mean final margin and total are
exactly what the projection said: the projection is fit to final scores, so
overtime and the tables change the shape, never the expectation.

Nothing is drawn at random, so there is no simulation noise and a page shows
the same picture every time. The card, the simulation pages, the projection's
win chance and the stakes all read from here, so they cannot disagree.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SPAN = 6.0  # how many sigmas either side of the projection the grid reaches
SOLVE_STEPS = 12  # Newton steps for the centres; two or three are usually enough
SOLVE_TOLERANCE = 1e-3  # in points (goals): far inside any price
TABLES_PATH = Path(__file__).with_name("sim_tables.json")

# How a tie is broken when the sport's table says nothing: the winner of
# overtime adds one of these (a field goal or a touchdown in football, a goal in
# hockey), each entry as likely as the next.
OVERTIME_MARGINS = {
    "nfl": (3, 3, 6, 7),
    "cfb": (3, 3, 6, 7, 8),
    "nhl": (1,),
}
# Basketball's overtime has both teams score for five more minutes: a base both
# sides add (college a little lower) and a winning margin of at least one.
BASKETBALL_OVERTIME_POINTS = {"nba": 11.0, "cbb": 9.0}
BASKETBALL_OVERTIME_SD = 3.0


def _load_tables() -> dict:
    try:
        return json.loads(TABLES_PATH.read_text())
    except (OSError, ValueError):
        return {}


TABLES = _load_tables()


@dataclass(frozen=True)
class SportShape:
    """What a sport's real results add to the two bell curves."""

    margin: dict[int, float] = field(default_factory=dict)  # by |regulation margin|
    score: dict[int, float] = field(default_factory=dict)  # by one team's regulation score
    # Overtime outcomes as (winner adds, loser adds, chance); empty for the defaults above.
    overtime: tuple[tuple[int, int, float], ...] = ()
    # How much of the regulation favourite's edge carries into overtime (1 = all of it).
    overtime_edge: float = 1.0
    # The bell curves' own widths as a share of the sport's: a table that piles chance
    # onto a few numbers also narrows the spread of results, and this puts it back, so
    # the final margin and total spread as widely as ``TUNING`` says.
    margin_scale: float = 1.0
    total_scale: float = 1.0
    # Hockey: the chance a side leading by one (two) late scores into the empty net,
    # turning the lead into two (three); the bell curves are the goals before that.
    empty_net: dict[int, float] = field(default_factory=dict)

    @property
    def joint(self) -> bool:
        """Whether the grid must lay out every score (a score table or empty nets)."""
        return bool(self.score or self.empty_net)


@lru_cache(maxsize=16)
def shape_for(sport: str) -> SportShape:
    table = TABLES.get(sport) or {}
    return SportShape(
        margin={int(k): float(v) for k, v in (table.get("margin") or {}).items()},
        score={int(k): float(v) for k, v in (table.get("score") or {}).items()},
        overtime=tuple((int(w), int(lo), float(p)) for w, lo, p in (table.get("overtime") or [])),
        overtime_edge=float(table.get("overtime_edge", 1.0)),
        margin_scale=float(table.get("margin_scale", 1.0)),
        total_scale=float(table.get("total_scale", 1.0)),
        empty_net={int(k): float(v) for k, v in (table.get("empty_net") or {}).items()},
    )


@dataclass(frozen=True)
class Grid:
    """A game's outcomes: the chance of each final margin and total, summing to one."""

    margin: dict[int, float]
    total: dict[int, float]
    overtime: float  # the chance the game is level after regulation
    home_win: float
    centres: tuple[float, float] = (0.0, 0.0)  # the bell curves' solved centres

    def margins(self) -> dict[int, float]:
        return self.margin

    def totals(self) -> dict[int, float]:
        return self.total


def _phi(x: float, mean: float, sd: float) -> float:
    z = (x - mean) / sd
    return math.exp(-0.5 * z * z)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _rounded_normal(mean: float, sd: float, k: int) -> float:
    """The chance a normal rounds to k, with everything below zero rounding to zero."""
    upper = _norm_cdf((k + 0.5 - mean) / sd)
    lower = 0.0 if k == 0 else _norm_cdf((k - 0.5 - mean) / sd)
    return upper - lower


@lru_cache(maxsize=16)
def overtime_outcomes(sport: str) -> tuple[tuple[int, int, float], ...]:
    """(winner adds, loser adds, chance) for an overtime, whoever wins it."""
    table = shape_for(sport).overtime
    if table:
        total = sum(p for _, _, p in table)
        return tuple((w, lo, p / total) for w, lo, p in table)
    if sport in BASKETBALL_OVERTIME_POINTS:
        base_mean = BASKETBALL_OVERTIME_POINTS[sport]
        sd = BASKETBALL_OVERTIME_SD
        out = []
        for base in range(int(base_mean + 6 * sd) + 1):
            p_base = _rounded_normal(base_mean, sd, base)
            for extra in range(int(6 * sd) + 1):
                # 1 + |round(N(0, sd))|: zero once, every other size from either side.
                p_round = _norm_cdf((extra + 0.5) / sd) - _norm_cdf((extra - 0.5) / sd)
                p = p_base * (p_round if extra == 0 else 2.0 * p_round)
                if p > 1e-9:
                    out.append((base + 1 + extra, base, p))
        total = sum(p for _, _, p in out)
        return tuple((w, lo, p / total) for w, lo, p in out)
    margins = OVERTIME_MARGINS.get(sport, (1,))
    counts: dict[int, int] = {}
    for m in margins:
        counts[m] = counts.get(m, 0) + 1
    return tuple((m, 0, n / len(margins)) for m, n in sorted(counts.items()))


@lru_cache(maxsize=16)
def _overtime_summary(sport: str) -> tuple[tuple, tuple, float]:
    """The overtime by what it does to the margin and to the total, kept apart for speed:
    ((winning margin, chance) for decided overtimes, (points added, chance), chance of a tie).
    """
    by_margin: dict[int, float] = {}
    by_total: dict[int, float] = {}
    tied = 0.0
    for winner, loser, q in overtime_outcomes(sport):
        if winner == loser:
            tied += q
        else:
            by_margin[winner - loser] = by_margin.get(winner - loser, 0.0) + q
        by_total[winner + loser] = by_total.get(winner + loser, 0.0) + q
    return tuple(sorted(by_margin.items())), tuple(sorted(by_total.items())), tied


def _overtime_home(shape: SportShape, regulation_margin: dict[int, float]) -> float:
    """Home's chance of winning an overtime: its regulation edge, scaled by the sport's table."""
    ahead = sum(p for m, p in regulation_margin.items() if m > 0)
    behind = sum(p for m, p in regulation_margin.items() if m < 0)
    p_reg = ahead / (ahead + behind) if ahead + behind > 0 else 0.5
    return 0.5 + shape.overtime_edge * (p_reg - 0.5)


def _bounds(mu: float, tau: float, sigma: float, sigma_total: float) -> tuple[int, int, int, int]:
    return (
        math.floor(mu - SPAN * sigma),
        math.ceil(mu + SPAN * sigma),
        max(0, math.floor(tau - SPAN * sigma_total)),
        max(0, math.ceil(tau + SPAN * sigma_total)),
    )


def _separable(sport: str, mu: float, tau: float, sigma: float, sigma_total: float):
    """Final margins and totals without building every score, for a sport with no score table.

    A regulation cell's weight is g(m)·φ(t), g being the margin's bell curve times
    its table, on the lattice where m and t share a parity and |m| ≤ t. So each
    margin's chance is g(m) times a running sum of φ over the totals it can sit
    in, and each total's is φ(t) times a running sum of g over the margins.
    """
    shape = shape_for(sport)
    m_lo, m_hi, t_lo, t_hi = _bounds(mu, tau, sigma, sigma_total)
    phi_t = {t: _phi(t, tau, sigma_total) for t in range(t_lo, t_hi + 1)}
    g = {m: _phi(m, mu, sigma) * shape.margin.get(abs(m), 1.0) for m in range(m_lo, m_hi + 1)}
    tail: dict[int, float] = {}  # φ(t) + φ(t + 2) + ...
    for t in range(t_hi, -1, -1):
        tail[t] = phi_t.get(t, 0.0) + tail.get(t + 2, 0.0)
    margin = {m: w * tail.get(abs(m), 0.0) for m, w in g.items()}
    z = sum(margin.values())
    if z <= 0:
        return None
    margin = {m: p / z for m, p in margin.items()}
    total: dict[int, float] = {}
    inner = {0: g.get(0, 0.0), 1: g.get(1, 0.0) + g.get(-1, 0.0)}
    for t in range(2, t_hi + 1):
        inner[t] = inner[t - 2] + g.get(t, 0.0) + g.get(-t, 0.0)
    for t in range(t_lo, t_hi + 1):
        total[t] = phi_t[t] * inner[t] / z
    g0 = g.get(0, 0.0)
    level_totals = {t: g0 * phi_t[t] / z for t in range(t_lo, t_hi + 1) if t % 2 == 0}
    return _settle(sport, shape, margin, total, level_totals)


def _settle(sport, shape, margin_reg, total_reg, level_totals):
    """Play out the level games: (final margins, final totals, level chance)."""
    by_margin, by_total, tied = _overtime_summary(sport)
    p_home = _overtime_home(shape, margin_reg)
    level = margin_reg.get(0, 0.0)
    margin = dict(margin_reg)
    margin[0] = level * tied
    for d, q in by_margin:
        margin[d] = margin.get(d, 0.0) + level * q * p_home
        margin[-d] = margin.get(-d, 0.0) + level * q * (1.0 - p_home)
    total = dict(total_reg)
    for t, p in level_totals.items():
        total[t] -= p
        for add, q in by_total:
            total[t + add] = total.get(t + add, 0.0) + p * q
    margin = {k: v for k, v in sorted(margin.items()) if v > 1e-15}
    total = {k: v for k, v in sorted(total.items()) if v > 1e-15}
    return margin, total, level


def _regulation_cells(sport: str, mu: float, tau: float, sigma: float, sigma_total: float):
    """Every regulation score (home, away) and its chance, with the sport's tables."""
    shape = shape_for(sport)
    m_lo, m_hi, t_lo, t_hi = _bounds(mu, tau, sigma, sigma_total)
    phi_m = {m: _phi(m, mu, sigma) * shape.margin.get(abs(m), 1.0) for m in range(m_lo, m_hi + 1)}
    phi_t = {t: _phi(t, tau, sigma_total) for t in range(t_lo, t_hi + 1)}
    score = shape.score
    regulation: dict[tuple[int, int], float] = {}
    if shape.empty_net:
        # The bell curves give the goals before the net empties (the margin table is
        # applied after, to the regulation margin itself).
        phi_m = {m: _phi(m, mu, sigma) for m in range(m_lo, m_hi + 1)}
    for t in range(t_lo, t_hi + 1):
        pt = phi_t[t]
        for m in range(max(m_lo, -t), min(m_hi, t) + 1):
            if (m + t) % 2:
                continue
            h, a = (t + m) // 2, (t - m) // 2
            regulation[(h, a)] = phi_m[m] * pt
    if shape.empty_net:
        regulation = _empty_net(regulation, shape)
    if score or shape.empty_net:
        regulation = {
            (h, a): p
            * (shape.margin.get(abs(h - a), 1.0) if shape.empty_net else 1.0)
            * score.get(h, 1.0)
            * score.get(a, 1.0)
            for (h, a), p in regulation.items()
        }
    z = sum(regulation.values())
    if z <= 0:
        return {}
    return {k: v / z for k, v in regulation.items()}


def _empty_net(cells: dict[tuple[int, int], float], shape: SportShape) -> dict:
    """Move each one- and two-goal lead on by an empty-net goal at the sport's rates."""
    out: dict[tuple[int, int], float] = {}
    for (h, a), p in cells.items():
        rate = shape.empty_net.get(abs(h - a), 0.0)
        if rate <= 0:
            out[(h, a)] = out.get((h, a), 0.0) + p
            continue
        moved = (h + 1, a) if h > a else (h, a + 1)
        out[(h, a)] = out.get((h, a), 0.0) + p * (1.0 - rate)
        out[moved] = out.get(moved, 0.0) + p * rate
    return out


def _joint(sport: str, mu: float, tau: float, sigma: float, sigma_total: float):
    """Every final score (home, away) and its chance, plus the level chance."""
    shape = shape_for(sport)
    regulation = _regulation_cells(sport, mu, tau, sigma, sigma_total)
    if not regulation:
        return None
    margin_reg: dict[int, float] = {}
    for (h, a), p in regulation.items():
        margin_reg[h - a] = margin_reg.get(h - a, 0.0) + p
    p_home = _overtime_home(shape, margin_reg)
    out: dict[tuple[int, int], float] = {}
    level = 0.0
    for (h, a), p in regulation.items():
        if h != a:
            out[(h, a)] = out.get((h, a), 0.0) + p
            continue
        level += p
        for winner, loser, q in overtime_outcomes(sport):
            if winner == loser:
                splits = (((h + winner, a + loser), 1.0),)
            else:
                splits = (((h + winner, a + loser), p_home), ((h + loser, a + winner), 1 - p_home))
            for key, share in splits:
                out[key] = out.get(key, 0.0) + p * q * share
    return out, level


def _marginals(cells: dict) -> tuple[dict[int, float], dict[int, float]]:
    margin: dict[int, float] = {}
    total: dict[int, float] = {}
    for (h, a), p in cells.items():
        margin[h - a] = margin.get(h - a, 0.0) + p
        total[h + a] = total.get(h + a, 0.0) + p
    return dict(sorted(margin.items())), dict(sorted(total.items()))


def _moments(pmf: dict[int, float]) -> tuple[float, float]:
    mean = sum(v * p for v, p in pmf.items())
    return mean, sum((v - mean) ** 2 * p for v, p in pmf.items())


def _solve(sport, margin_mean, total_mean, sigma, sigma_total, build):
    """The bell-curve centres whose final means are the projection's, and what they give.

    Moving a normal's centre is the same as tilting it, so each step is Newton's:
    the mean moves by the final variance over the bell curve's for each point the
    centre moves.
    """
    mu, tau = margin_mean, total_mean
    best = None
    for _ in range(SOLVE_STEPS):
        result = build(sport, mu, tau, sigma, sigma_total)
        if result is None:
            break  # walked off the lattice (a margin no total can hold): keep the last
        best = (result, mu, tau)
        margin, total = result[0], result[1]
        (m_mean, m_var), (t_mean, t_var) = _moments(margin), _moments(total)
        m_err, t_err = margin_mean - m_mean, total_mean - t_mean
        if abs(m_err) < SOLVE_TOLERANCE and abs(t_err) < SOLVE_TOLERANCE:
            break
        # Newton's step, at most one width at a time.
        mu += max(-sigma, min(sigma, m_err * sigma**2 / max(m_var, 1e-9)))
        tau += max(-sigma_total, min(sigma_total, t_err * sigma_total**2 / max(t_var, 1e-9)))
    return best


def _joint_marginals(sport, mu, tau, sigma, sigma_total):
    joint = _joint(sport, mu, tau, sigma, sigma_total)
    if joint is None:
        return None
    cells, level = joint
    margin, total = _marginals(cells)
    return margin, total, level


@lru_cache(maxsize=4096)
def _grid(
    sport: str, margin_mean: float, total_mean: float, sigma: float, sigma_total: float
) -> Grid | None:
    build = _joint_marginals if shape_for(sport).joint else _separable
    solved = _solve(sport, margin_mean, total_mean, sigma, sigma_total, build)
    if solved is None:
        return None
    (margin, total, level), mu, tau = solved
    return Grid(
        margin=margin,
        total=total,
        overtime=level,
        home_win=sum(p for m, p in margin.items() if m > 0),
        centres=(mu, tau),
    )


@lru_cache(maxsize=8)
def _cells(
    sport: str, margin_mean: float, total_mean: float, sigma: float, sigma_total: float
) -> dict[tuple[int, int], float]:
    g = _grid(sport, margin_mean, total_mean, sigma, sigma_total)
    if g is None:
        return {}
    joint = _joint(sport, *g.centres, sigma, sigma_total)
    return joint[0] if joint else {}


def _key(sport, margin_mean, total_mean, sigma, sigma_total) -> tuple:
    shape = shape_for(sport)
    return (
        sport,
        round(float(margin_mean), 4),
        round(float(total_mean), 4),
        round(float(sigma) * shape.margin_scale, 4),
        round(float(sigma_total) * shape.total_scale, 4),
    )


def _usable(sigma, sigma_total, total_mean) -> bool:
    return sigma > 0 and sigma_total > 0 and total_mean > 0


def grid(
    sport: str, margin_mean: float, total_mean: float, sigma: float, sigma_total: float
) -> Grid | None:
    """A game's final margins and totals and their chances; None with nothing to lay out."""
    if not _usable(sigma, sigma_total, total_mean):
        return None
    return _grid(*_key(sport, margin_mean, total_mean, sigma, sigma_total))


def cells(
    sport: str, margin_mean: float, total_mean: float, sigma: float, sigma_total: float
) -> dict[tuple[int, int], float]:
    """Every final score (home, away) and its chance, for the score pictures."""
    if not _usable(sigma, sigma_total, total_mean):
        return {}
    return _cells(*_key(sport, margin_mean, total_mean, sigma, sigma_total))


def regulation(
    sport: str, margin_mean: float, total_mean: float, sigma: float, sigma_total: float
) -> dict[tuple[int, int], float]:
    """Every score at the end of regulation and its chance, before overtime (for grading
    the shape against history and for building the tables)."""
    if not _usable(sigma, sigma_total, total_mean):
        return {}
    key = _key(sport, margin_mean, total_mean, sigma, sigma_total)
    g = _grid(*key)
    if g is None:
        return {}
    return _regulation_cells(sport, *g.centres, key[3], key[4])


def win_chance(
    sport: str, margin_mean: float, total_mean: float | None, sigma: float, sigma_total: float
) -> float | None:
    """Home's chance of winning, from the grid; None when the grid cannot be laid out."""
    if total_mean is None:
        return None
    g = grid(sport, margin_mean, total_mean, sigma, sigma_total)
    return None if g is None else g.home_win


def margin_for_win(
    sport: str, p_home: float, total_mean: float, sigma: float, sigma_total: float
) -> float | None:
    """The expected margin at which the grid gives home ``p_home`` to win (bisection: the
    grid's win chance rises with the margin). None when the grid cannot be laid out."""
    lo, hi = -3.0 * sigma, 3.0 * sigma  # wider than any price the books quote
    for _ in range(40):
        mid = (lo + hi) / 2.0
        g = grid(sport, mid, total_mean, sigma, sigma_total)
        if g is None:
            return None
        if g.home_win < p_home:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    return round((lo + hi) / 2.0, 4)


def split(pmf: dict[int, float], line: float) -> dict[str, float]:
    """The chance above, on and below a line."""
    above = sum(p for v, p in pmf.items() if v > line)
    push = sum(p for v, p in pmf.items() if v == line)
    total = sum(pmf.values())
    return {"above": above, "push": push, "below": max(total - above - push, 0.0)}


def cover(g: Grid, home_line: float) -> dict[str, float]:
    """Home covers a handicap when margin + line > 0; ``above`` is the home side."""
    return split(g.margin, -home_line)


def over(g: Grid, line: float) -> dict[str, float]:
    return split(g.total, line)


def without_push(chances: dict[str, float]) -> tuple[float, float] | None:
    """(above, below) given no push, as a void line settles: the stake comes back."""
    decided = chances["above"] + chances["below"]
    if decided <= 0:
        return None
    return chances["above"] / decided, chances["below"] / decided
