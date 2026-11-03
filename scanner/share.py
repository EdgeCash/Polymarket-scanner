"""The card's share images: a day's card and a single pick, as 1200 by 675 PNGs.

Each image is drawn as SVG (so it is the same text and the same histogram the
page shows) and rasterised with cairosvg. Dark background, team colours as
accents, no logos: nothing is fetched from anywhere while an image is drawn.
Fonts come from the host; the Docker image carries DejaVu Sans.
"""

from __future__ import annotations

import html
from datetime import datetime

from scanner.config import Settings
from scanner.simulate import svg_histogram

WIDTH, HEIGHT = 1200, 675
BG = "#0f172a"
INK = "#f8fafc"
DIM = "#94a3b8"
ACCENT = "#1f6f8b"
WIN, LOSS, PUSH = "#1a7f37", "#b42318", "#6b7280"
FONT = "DejaVu Sans, Helvetica Neue, Helvetica, Arial, sans-serif"
MARGIN = 56


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _colour(team: dict | None) -> str:
    value = (team or {}).get("color")
    if isinstance(value, str) and len(value) == 6:
        return f"#{value}"
    return ACCENT


def _text(x: float, y: float, text: str, size: int, **attrs) -> str:
    extra = "".join(f" {k.replace('_', '-')}='{_esc(v)}'" for k, v in attrs.items())
    return (
        f"<text x='{x:.0f}' y='{y:.0f}' font-family='{FONT}' font-size='{size}'{extra}>"
        f"{_esc(text)}</text>"
    )


def _chip(x: float, y: float, text: str, fill: str, size: int = 18) -> tuple[str, float]:
    """A rounded label; returns the markup and its width so the next item can follow it."""
    width = len(text) * size * 0.62 + 20
    markup = (
        f"<rect x='{x:.0f}' y='{y - size - 4:.0f}' width='{width:.0f}' height='{size + 12}' "
        f"rx='6' fill='{fill}'/>" + _text(x + 10, y + 2, text, size, fill="#fff", font_weight="700")
    )
    return markup, width


def _head() -> str:
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{WIDTH}' height='{HEIGHT}' "
        f"viewBox='0 0 {WIDTH} {HEIGHT}'><rect width='{WIDTH}' height='{HEIGHT}' fill='{BG}'/>"
    )


def _odds(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _when(iso: str, tz: str) -> str:
    from zoneinfo import ZoneInfo

    from scanner.web import _zone_label

    moment = datetime.fromisoformat(iso).astimezone(ZoneInfo(tz))
    return f"{moment.strftime('%a %-I:%M %p')} {_zone_label(tz)}"


def _result(pick: dict) -> tuple[str, str] | None:
    outcome = pick.get("outcome")
    if outcome == "win":
        return f"WIN {pick['units']:+.2f}u", WIN
    if outcome == "loss":
        return f"LOSS {pick['units']:+.2f}u", LOSS
    if outcome == "push":
        return "PUSH", PUSH
    return None


def _record_text(summary: dict) -> str | None:
    if not summary.get("graded"):
        return None
    text = f"Record {summary['wins']}-{summary['losses']}-{summary['pushes']}, "
    text += f"{summary['units']:+.2f} units over {summary['days']} days"
    return text


def card_svg(card: dict, summary: dict, settings: Settings) -> str:
    """A day's card: every pick in rank order, the record so far, the rule in one line."""
    parts = [_head()]
    y = MARGIN + 36
    parts.append(
        _text(MARGIN, y, "DAILY CARD", 18, fill=DIM, letter_spacing="3", font_weight="700")
    )
    parts.append(_text(WIDTH - MARGIN, y, _when(card["locked_at"], settings.CARD_TZ), 18,
                       fill=DIM, text_anchor="end"))  # fmt: skip
    y += 44
    parts.append(_text(MARGIN, y, card["card_date"], 40, fill=INK, font_weight="700"))
    record = _record_text(summary)
    if record:
        parts.append(_text(WIDTH - MARGIN, y, record, 22, fill=INK, text_anchor="end"))
    rows = card["rows"][: settings.CARD_PICKS]
    top = y + 34
    room = HEIGHT - top - 64
    step = min(92, room / max(len(rows), 1))
    for i, pick in enumerate(rows):
        y = top + i * step
        detail = pick.get("detail") or {}
        home_colour = _colour(detail.get("home_team"))
        away_colour = _colour(detail.get("away_team"))
        parts.append(
            f"<rect x='{MARGIN}' y='{y:.0f}' width='6' height='{step - 14:.0f}' rx='3' "
            f"fill='{away_colour}'/><rect x='{MARGIN + 8}' y='{y:.0f}' width='6' "
            f"height='{step - 14:.0f}' rx='3' fill='{home_colour}'/>"
        )
        x = MARGIN + 30
        chip, width = _chip(x, y + 26, f"#{pick['rank']}", "#334155", 20)
        parts.append(chip)
        x += width + 8
        chip, width = _chip(x, y + 26, pick["sport"].upper(), ACCENT, 16)
        parts.append(chip)
        x += width + 14
        matchup = f"{pick['away']} at {pick['home']} · {_when(pick['kickoff'], settings.CARD_TZ)}"
        parts.append(_text(x, y + 28, matchup, 20, fill=DIM))
        call = f"{pick['side_label']}  ({_odds(pick['odds'])})"
        parts.append(_text(MARGIN + 30, y + step - 26, call, 30, fill=INK, font_weight="700"))
        chance = detail.get("chance")
        right = WIDTH - MARGIN
        result = _result(pick)
        if result:
            chip, width = _chip(right - len(result[0]) * 20 * 0.62 - 20, y + step - 30,
                                result[0], result[1], 20)  # fmt: skip
            parts.append(chip)
            right -= width + 16
        if chance is not None:
            parts.append(_text(right, y + step - 26, f"sim {chance * 100:.0f}%", 24,
                               fill=INK, text_anchor="end"))  # fmt: skip
    from scanner.web import _zone_label

    lock = f"{settings.card_lock_time.strftime('%-I:%M %p')} {_zone_label(settings.CARD_TZ)}"
    foot = (
        f"Chosen by rule at {lock}, graded at the final, misses shown. "
        "Research, not advice; nothing placed."
    )
    parts.append(_text(MARGIN, HEIGHT - 28, foot, 16, fill=DIM))
    parts.append("</svg>")
    return "".join(parts)


def pick_svg(pick: dict, settings: Settings) -> str:
    """One pick: the call and its numbers on the left, its histogram on the right."""
    from scanner.web import _implied, _value_text

    detail = pick.get("detail") or {}
    sim = detail.get("sim") or {}
    parts = [_head()]
    y = MARGIN + 36
    chip, width = _chip(MARGIN, y, f"#{pick['rank']}", "#334155", 20)
    parts.append(chip)
    chip, _ = _chip(MARGIN + width + 8, y, pick["sport"].upper(), ACCENT, 16)
    parts.append(chip)
    parts.append(_text(WIDTH - MARGIN, y, f"Daily card {pick['card_date']}", 18, fill=DIM,
                       text_anchor="end"))  # fmt: skip
    y += 52
    parts.append(_text(MARGIN, y, f"{pick['away']} at {pick['home']}", 36, fill=INK,
                       font_weight="700"))  # fmt: skip
    y += 30
    parts.append(_text(MARGIN, y, _when(pick["kickoff"], settings.CARD_TZ), 20, fill=DIM))
    y += 62
    parts.append(_text(MARGIN, y, pick["side_label"], 52, fill=INK, font_weight="800"))
    parts.append(_text(MARGIN + len(pick["side_label"]) * 52 * 0.76 + 16, y,
                       f"({_odds(pick['odds'])})", 24, fill=DIM))  # fmt: skip
    result = _result(pick)
    if result:
        y += 44
        chip, _ = _chip(MARGIN, y, result[0], result[1], 22)
        parts.append(chip)
        final = f"Final {pick['away']} {pick['final_away']}, {pick['home']} {pick['final_home']}"
        parts.append(_text(MARGIN + len(result[0]) * 22 * 0.62 + 36, y + 2, final, 20, fill=DIM))
    unit = "goals" if pick["sport"] == "nhl" else "points"
    lines = [
        f"Model {_value_text(pick, pick['model_value'])}, book "
        f"{_value_text(pick, pick['book_value'])}",
        f"{abs(pick['gap']):.1f} {unit} apart, {pick['z']:.2f} of a game's spread",
    ]
    chance = detail.get("chance")
    if chance is not None and sim.get("draws"):
        lines.append(f"Hits in {chance * 100:.0f}% of {sim['draws']:,} simulated games")
        lines.append(f"The price says {_implied(pick['odds']) * 100:.0f}%")
    y = HEIGHT - 64 - 30 * len(lines)
    for line in lines:
        parts.append(_text(MARGIN, y, line, 20, fill=INK))
        y += 30
    if sim:
        if pick["market"] == "total":
            hist = svg_histogram(
                sim["total"]["hist"], line=pick["book_value"], mean=pick["model_value"],
                side_above=pick["side"] == "over", line_label=f"book {pick['book_value']:g}",
                mean_label=f"model {pick['model_value']:.1f}", axis_label=f"Total {unit}",
                muted="#334155", text=INK,
            )  # fmt: skip
        else:
            hist = svg_histogram(
                sim["margin"]["hist"], line=pick["book_value"], mean=pick["model_value"],
                side_above=pick["side"] == "home",
                line_label=f"book {pick['home']} {-pick['book_value']:+.1f}",
                mean_label=f"model {pick['home']} {-pick['model_value']:+.1f}",
                axis_label=f"Margin ({pick['home']}, {unit})", muted="#334155", text=INK,
            )  # fmt: skip
        if hist:
            inner = hist.replace("<svg ", "<svg x='520' y='130' width='624' height='224' ", 1)
            inner = inner.replace("width='100%' ", "")
            parts.append(inner)
    parts.append(_text(MARGIN, HEIGHT - 28,
                       "Chosen by rule, graded at the final. Research, not advice; nothing placed.",
                       16, fill=DIM))  # fmt: skip
    parts.append("</svg>")
    return "".join(parts)


def player_svg(sport: str, upcoming: dict, lines: list[dict], settings: Settings) -> str:
    """One player's lines for a game: every stat's median and band, the first stat's histogram."""
    from scanner.players import simulate_player
    from scanner.simulate import seed_for
    from scanner.web import STAT_LABELS, _stat_order

    lines = sorted(lines, key=lambda r: _stat_order(sport, r["stat"]))
    first = lines[0]
    parts = [_head()]
    y = MARGIN + 36
    chip, width = _chip(MARGIN, y, sport.upper(), ACCENT, 16)
    parts.append(chip)
    if first.get("position"):
        chip, _ = _chip(MARGIN + width + 8, y, first["position"], "#334155", 16)
        parts.append(chip)
    parts.append(_text(WIDTH - MARGIN, y, "Player lines", 18, fill=DIM, text_anchor="end"))
    y += 52
    parts.append(_text(MARGIN, y, first["name"], 36, fill=INK, font_weight="700"))
    y += 30
    when = _when(first["kickoff"], settings.CARD_TZ)
    where = f"{first['team_abbr']} v {first['opponent_abbr']} · {when}"
    parts.append(_text(MARGIN, y, where, 20, fill=DIM))
    y += 54
    for row in lines[:5]:
        label = STAT_LABELS.get(row["stat"], row["stat"])
        parts.append(_text(MARGIN, y, label, 20, fill=DIM))
        parts.append(_text(MARGIN + 230, y, str(row["median"]), 30, fill=INK, font_weight="800"))
        parts.append(_text(MARGIN + 300, y, f"{row['p10']}–{row['p90']}", 20, fill=DIM))
        result = _result_line(row)
        if result:
            parts.append(_text(MARGIN + 400, y, result, 18, fill=INK))
        y += 46
    stamp = f"{first['game_id']}:{first['player_id']}:{first['stat']}"
    sim = simulate_player(
        first["params"], seed=seed_for(sport, stamp, str(first.get("updated_at") or ""))
    )
    mark = (
        (float(first["actual"]), f"actual {first['actual']}")
        if first.get("outcome") == "graded"
        else None
    )
    hist = svg_histogram(
        sim["hist"],
        line=None,
        mean=float(first["median"]),
        side_above=None,
        line_label="",
        mean_label=f"median {first['median']}",
        axis_label=STAT_LABELS.get(first["stat"], first["stat"]),
        muted="#334155",
        text=INK,
        band=(first["p10"], first["p90"]),
        mark=mark,
    )
    if hist:
        inner = hist.replace("<svg ", "<svg x='560' y='150' width='584' height='210' ", 1)
        parts.append(inner.replace("width='100%' ", ""))
    parts.append(_text(MARGIN, HEIGHT - 28,
                       "Median and 80% band from the box scores alone. Graded at the final. "
                       "Research, not advice; nothing placed.", 16, fill=DIM))  # fmt: skip
    parts.append("</svg>")
    return "".join(parts)


def _result_line(row: dict) -> str | None:
    if row.get("outcome") == "graded":
        return f"actual {row['actual']}"
    if row.get("outcome") == "did_not_play":
        return "did not play"
    return None


def to_png(svg: str, scale: float = 2.0) -> bytes:
    """Rasterise one of the images above; ``scale`` 2 gives 2400 by 1350 for a sharp post."""
    import cairosvg

    return cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        output_width=int(WIDTH * scale),
        output_height=int(HEIGHT * scale),
    )
