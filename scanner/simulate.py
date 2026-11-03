"""A Monte Carlo picture of one game, drawn from the projection model's own numbers.

The projection says what the final margin and total should be and how widely
each spreads (``TUNING``'s sigmas). This module plays the game out that many
times: each draw is a margin and a total from those normal distributions, turned
into a home and an away score; a tie goes to overtime. What comes out is the
whole shape of the model's opinion rather than one number: how often each side
wins, how often each margin and total lands, the likeliest final scores and the
chance of any line clearing, for the card page's pictures.

It is a picture of the model, not a better model. A game drawn 100,000 times
inherits every error in the ratings underneath, and the record on the scorecard
is what says whether those ratings are any good. Nothing here is sent or placed.
"""

from __future__ import annotations

import html
import math
import random
from collections import Counter

from scanner.config import SIM_DRAWS
from scanner.projection import tuning_for

# How a tie is broken: the winner of overtime adds these to its score, each as
# likely as the next (a field goal or a touchdown in football, a goal in hockey).
# Basketball's overtime has both teams score for five more minutes, so its
# winning margin is drawn beside a base both sides add (college a little lower).
OVERTIME_MARGINS = {
    "nfl": (3, 3, 6, 7),
    "cfb": (3, 3, 6, 7, 8),
    "nhl": (1,),
}
# What each side scores in a five-minute basketball overtime, on average.
BASKETBALL_OVERTIME_POINTS = {"nba": 11.0, "cbb": 9.0}
BASKETBALL = tuple(BASKETBALL_OVERTIME_POINTS)


def seed_for(sport: str, game_id: str, stamp: str) -> int:
    """A seed that pins a game's picture, so a page shows the same 100,000 games every time."""
    return hash_text(f"{sport}:{game_id}:{stamp}")


def hash_text(text: str) -> int:
    """A small, stable hash (Python's own is salted per process)."""
    value = 2166136261
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def _overtime(sport: str, home_wins: bool, rng: random.Random) -> tuple[int, int]:
    """(home points added, away points added) by an overtime the given side wins."""
    if sport in BASKETBALL:
        base = max(0, round(rng.gauss(BASKETBALL_OVERTIME_POINTS[sport], 3.0)))
        margin = 1 + abs(round(rng.gauss(0.0, 3.0)))
        winner, loser = base + margin, base
    else:
        winner, loser = rng.choice(OVERTIME_MARGINS.get(sport, (1,))), 0
    return (winner, loser) if home_wins else (loser, winner)


def simulate(
    projection: dict,
    sport: str,
    draws: int = SIM_DRAWS,
    seed: int | None = None,
    source: str = "raw",
    with_cells: bool = False,
) -> dict | None:
    """Play a projected game out ``draws`` times. None when the projection has no numbers.

    ``source`` is which of the projection's views to draw from: the model
    (``raw``, the default and what the card grades) or the ``blend``. With
    ``with_cells`` the joint count of every (home, away) score comes back too,
    for the heatmap; it is left out of what the diary stores.
    """
    part = projection.get(source) or {}
    margin_mean, total_mean = part.get("margin"), part.get("total")
    if margin_mean is None or total_mean is None or draws <= 0:
        return None
    league = projection.get("league") or {}
    tuning = tuning_for(sport)
    sigma = float(league.get("sigma") or tuning.sigma)
    sigma_total = float(league.get("sigma_total") or tuning.sigma_total)
    p_home = part.get("home_win")
    if p_home is None:
        p_home = 0.5 * (1.0 + math.erf(margin_mean / (sigma * math.sqrt(2.0))))
    rng = random.Random(seed)

    margins: Counter[int] = Counter()
    totals: Counter[int] = Counter()
    cells: Counter[tuple[int, int]] = Counter()
    home_wins = overtimes = 0
    margin_sum = margin_sq = total_sum = total_sq = 0.0
    for _ in range(draws):
        margin = rng.gauss(margin_mean, sigma)
        total = rng.gauss(total_mean, sigma_total)
        home = max(0, round((total + margin) / 2.0))
        away = max(0, round((total - margin) / 2.0))
        if home == away:
            overtimes += 1
            won = rng.random() < p_home
            add_home, add_away = _overtime(sport, won, rng)
            home, away = home + add_home, away + add_away
        final_margin, final_total = home - away, home + away
        if final_margin > 0:
            home_wins += 1
        margins[final_margin] += 1
        totals[final_total] += 1
        cells[(home, away)] += 1
        margin_sum += final_margin
        margin_sq += final_margin * final_margin
        total_sum += final_total
        total_sq += final_total * final_total

    def moments(total_of: float, square_of: float) -> tuple[float, float]:
        mean = total_of / draws
        variance = max(square_of / draws - mean * mean, 0.0)
        return mean, math.sqrt(variance)

    margin_mu, margin_sd = moments(margin_sum, margin_sq)
    total_mu, total_sd = moments(total_sum, total_sq)
    out = {
        "draws": draws,
        "seed": seed,
        "source": source,
        "home_win": home_wins / draws,
        "overtime": overtimes / draws,
        "margin": {
            "mean": margin_mu,
            "sd": margin_sd,
            "hist": {str(k): v for k, v in sorted(margins.items())},
        },
        "total": {
            "mean": total_mu,
            "sd": total_sd,
            "hist": {str(k): v for k, v in sorted(totals.items())},
        },
        "scores": [[h, a, n] for (h, a), n in cells.most_common(10)],
    }
    if with_cells:
        out["cells"] = {f"{h},{a}": n for (h, a), n in cells.items()}
    return out


def chances(hist: dict, line: float) -> dict[str, float]:
    """From a histogram of integer outcomes, the share above, on and below a line."""
    draws = sum(hist.values())
    if draws <= 0:
        return {"above": 0.0, "push": 0.0, "below": 0.0}
    above = push = 0
    for key, count in hist.items():
        value = int(key)
        if value > line:
            above += count
        elif value == line:
            push += count
    return {
        "above": above / draws,
        "push": push / draws,
        "below": (draws - above - push) / draws,
    }


def pick_chance(sim: dict, market: str, side: str, line: float | None) -> float | None:
    """How often the simulated games came in for a side: the pick's own probability."""
    if market == "moneyline":
        return sim["home_win"] if side == "home" else 1.0 - sim["home_win"]
    if line is None:
        return None
    if market == "spread":
        # The line is the home handicap: home covers when margin + line > 0.
        split = chances(sim["margin"]["hist"], -line)
        return split["above"] if side == "home" else split["below"]
    if market == "total":
        split = chances(sim["total"]["hist"], line)
        return split["above"] if side == "over" else split["below"]
    return None


def ladder(hist: dict, centre: float, step: float, rungs: int = 3) -> list[dict]:
    """The chance of clearing each line on either side of a centre line, for the table."""
    out = []
    for n in range(-rungs, rungs + 1):
        line = round(centre + n * step, 2)
        split = chances(hist, line)
        out.append({"line": line, **split})
    return out


# -- pictures ----------------------------------------------------------------------------------

WIDTH = 640
HEIGHT = 230
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 10, 10, 32, 30


def _trim(hist: dict, keep: float = 0.995) -> list[tuple[int, int]]:
    """The histogram's bars without the far tails, so the picture has a readable scale."""
    pairs = sorted((int(k), v) for k, v in hist.items())
    draws = sum(v for _, v in pairs)
    if draws == 0:
        return []
    cut = draws * (1.0 - keep) / 2.0
    low = 0
    seen = 0
    for i, (_, count) in enumerate(pairs):
        seen += count
        if seen > cut:
            low = i
            break
    high = len(pairs) - 1
    seen = 0
    for i in range(len(pairs) - 1, -1, -1):
        seen += pairs[i][1]
        if seen > cut:
            high = i
            break
    return pairs[low : high + 1]


def _tick(value: int, axis_label: str) -> str:
    """A margin is signed (home ahead is positive); a total is not."""
    if axis_label.startswith("Margin") and value != 0:
        return f"{value:+d}"
    return str(value)


def svg_histogram(
    hist: dict,
    *,
    line: float | None,
    mean: float | None,
    side_above: bool | None,
    line_label: str,
    mean_label: str,
    axis_label: str,
    highlight: str = "#1a7f37",
    muted: str = "#8884",
    width: int = WIDTH,
    height: int = HEIGHT,
    text: str = "currentColor",
) -> str:
    """A bar for each integer outcome, the pick's side shaded, the book's line and the model's
    mean marked. Inline SVG with no scripts; colours work on a light or dark page."""
    bars = _trim(hist)
    if not bars:
        return ""
    draws = sum(hist.values())
    values = [v for v, _ in bars]
    lo, hi = values[0], values[-1]
    # Room for the marked lines even when they sit outside the bars' range.
    for mark in (line, mean):
        if mark is not None:
            lo, hi = min(lo, math.floor(mark)), max(hi, math.ceil(mark))
    span = hi - lo + 1
    plot_w = width - PAD_LEFT - PAD_RIGHT
    plot_h = height - PAD_TOP - PAD_BOTTOM
    bar_w = plot_w / span
    peak = max(v for _, v in bars)

    def x_of(value: float) -> float:
        return PAD_LEFT + (value - lo + 0.5) * bar_w

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' "
        f"width='100%' role='img' aria-label='{html.escape(axis_label)}' "
        "style='font-family: -apple-system, system-ui, sans-serif; font-size: 15px'>"
    ]
    counts = dict(bars)
    for value in range(lo, hi + 1):
        count = counts.get(value, 0)
        if count == 0:
            continue
        bar_h = plot_h * count / peak
        x = PAD_LEFT + (value - lo) * bar_w
        y = PAD_TOP + plot_h - bar_h
        on_side = (
            None
            if side_above is None or line is None
            else (value > line if side_above else value < line)
        )
        fill = highlight if on_side else muted
        title = _tick(value, axis_label)
        parts.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{max(bar_w - 1, 1):.1f}' "
            f"height='{bar_h:.1f}' fill='{fill}'><title>{html.escape(title)}: "
            f"{count / draws * 100:.1f}%</title></rect>"
        )
    baseline = PAD_TOP + plot_h
    parts.append(
        f"<line x1='{PAD_LEFT}' y1='{baseline}' x2='{width - PAD_RIGHT}' y2='{baseline}' "
        "stroke='#888' stroke-width='1'/>"
    )
    # Axis ticks: at most a dozen, on round numbers.
    step = max(1, math.ceil(span / 12))
    first = lo + (-lo) % step
    for value in range(first, hi + 1, step):
        label = _tick(value, axis_label)
        parts.append(
            f"<text x='{x_of(value):.1f}' y='{height - 12}' text-anchor='middle' "
            f"fill='{text}' opacity='0.8'>{label}</text>"
        )
    parts.append(
        f"<text x='{width - PAD_RIGHT}' y='{height - 1}' text-anchor='end' fill='{text}' "
        f"opacity='0.7' font-size='13'>{html.escape(axis_label)}</text>"
    )
    marks = []
    if line is not None:
        marks.append((line, line_label, "#b42318", "4 3"))
    if mean is not None:
        marks.append((mean, mean_label, "#1f6f8b", None))
    for i, (value, label, colour, dash) in enumerate(marks):
        x = x_of(value)
        dash_attr = f" stroke-dasharray='{dash}'" if dash else ""
        parts.append(
            f"<line x1='{x:.1f}' y1='{PAD_TOP - 4}' x2='{x:.1f}' y2='{baseline}' "
            f"stroke='{colour}' stroke-width='2'{dash_attr}/>"
        )
        anchor = "start" if x < width / 2 else "end"
        offset = 5 if anchor == "start" else -5
        # Two rows of labels, so the book's line and the model's mean never overprint.
        parts.append(
            f"<text x='{x + offset:.1f}' y='{12 + i * 14}' text-anchor='{anchor}' "
            f"fill='{colour}' font-weight='600'>{html.escape(label)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_heatmap(
    cells: dict,
    home: str,
    away: str,
    *,
    colour: str = "#1f6f8b",
    size: int = 520,
) -> str:
    """Every simulated final score as a cell, darker the more often it came up.

    The away score runs along the bottom, the home score up the side, so a cell
    above the diagonal is a home win. Scores off the busy middle are cut so the
    grid stays readable.
    """
    counts = {}
    for key, count in cells.items():
        h, a = key.split(",")
        counts[(int(h), int(a))] = count
    if not counts:
        return ""
    draws = sum(counts.values())
    home_hist = Counter()
    away_hist = Counter()
    for (h, a), n in counts.items():
        home_hist[h] += n
        away_hist[a] += n
    home_keep = [v for v, _ in _trim({str(k): v for k, v in home_hist.items()}, 0.98)]
    away_keep = [v for v, _ in _trim({str(k): v for k, v in away_hist.items()}, 0.98)]
    if not home_keep or not away_keep:
        return ""
    h_lo, h_hi = home_keep[0], home_keep[-1]
    a_lo, a_hi = away_keep[0], away_keep[-1]
    rows, cols = h_hi - h_lo + 1, a_hi - a_lo + 1
    pad_left, pad_bottom, pad = 34, 30, 8
    cell = max(2.0, min((size - pad_left - pad) / cols, (size - pad_bottom - pad) / rows))
    width = pad_left + pad + cols * cell
    height = pad_bottom + pad + rows * cell
    peak = max(counts.values())
    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width:.0f} {height:.0f}' "
        "width='100%' role='img' aria-label='Simulated final scores' "
        "style='font-family: -apple-system, system-ui, sans-serif; font-size: 13px; "
        f"max-width: {size}px'>"
    ]
    for (h, a), n in counts.items():
        if not (h_lo <= h <= h_hi and a_lo <= a <= a_hi):
            continue
        x = pad_left + (a - a_lo) * cell
        y = pad + (h_hi - h) * cell
        opacity = 0.08 + 0.92 * (n / peak) ** 0.5
        parts.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{cell:.1f}' height='{cell:.1f}' "
            f"fill='{colour}' fill-opacity='{opacity:.2f}'><title>{html.escape(home)} {h}, "
            f"{html.escape(away)} {a}: {n / draws * 100:.2f}%</title></rect>"
        )
    # The tie line: everything above it is a home win.
    lo_v, hi_v = max(h_lo, a_lo), min(h_hi, a_hi)
    if lo_v <= hi_v:
        x1 = pad_left + (lo_v - a_lo) * cell
        y1 = pad + (h_hi - lo_v + 1) * cell
        x2 = pad_left + (hi_v - a_lo + 1) * cell
        y2 = pad + (h_hi - hi_v) * cell
        parts.append(
            f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='#b42318' "
            "stroke-width='1.5' stroke-dasharray='4 3' opacity='0.8'/>"
        )
    tick = max(1, math.ceil(max(rows, cols) / 10))
    for a in range(a_lo + (-a_lo) % tick, a_hi + 1, tick):
        x = pad_left + (a - a_lo + 0.5) * cell
        parts.append(
            f"<text x='{x:.1f}' y='{height - 16:.0f}' text-anchor='middle' fill='currentColor' "
            f"opacity='0.8'>{a}</text>"
        )
    for h in range(h_lo + (-h_lo) % tick, h_hi + 1, tick):
        y = pad + (h_hi - h + 0.5) * cell + 4
        parts.append(
            f"<text x='{pad_left - 6}' y='{y:.1f}' text-anchor='end' fill='currentColor' "
            f"opacity='0.8'>{h}</text>"
        )
    parts.append(
        f"<text x='{pad_left + cols * cell / 2:.1f}' y='{height - 3:.0f}' text-anchor='middle' "
        f"fill='currentColor' opacity='0.7'>{html.escape(away)} score</text>"
    )
    parts.append(
        f"<text x='10' y='{pad + rows * cell / 2:.1f}' text-anchor='middle' fill='currentColor' "
        f"opacity='0.7' transform='rotate(-90 10 {pad + rows * cell / 2:.1f})'>"
        f"{html.escape(home)} score</text>"
    )
    parts.append("</svg>")
    return "".join(parts)
