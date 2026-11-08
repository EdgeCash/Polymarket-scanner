from __future__ import annotations

import pytest

from scanner import grid as grid_module
from scanner.grid import cells, cover, grid, over, split, win_chance, without_push
from scanner.projection import win_probability

CASES = [
    ("nfl", 3.0, 44.0, 13.5, 13.5),
    ("cfb", -7.5, 58.0, 16.5, 16.5),
    ("nba", -4.0, 228.0, 13.5, 19.0),
    ("cbb", 5.0, 145.0, 11.0, 17.0),
    ("nhl", 0.3, 6.1, 2.5, 2.4),
]


@pytest.fixture
def tables():
    """Swap in a sport table for one test, and leave the real ones behind it."""

    def use(value: dict) -> None:
        grid_module.reload(value)

    yield use
    grid_module.reload()


def _mean(pmf):
    return sum(v * p for v, p in pmf.items())


@pytest.mark.parametrize("case", CASES)
def test_the_grid_sums_to_one_and_keeps_the_projections_means(case):
    sport, margin, total, sigma, sigma_total = case
    g = grid(sport, margin, total, sigma, sigma_total)
    assert sum(g.margin.values()) == pytest.approx(1.0)
    assert sum(g.total.values()) == pytest.approx(1.0)
    # Fit to final scores, so overtime and the tables change the shape, not the mean.
    assert _mean(g.margin) == pytest.approx(margin, abs=1e-3)
    assert _mean(g.total) == pytest.approx(total, abs=1e-3)
    assert g.home_win == pytest.approx(sum(p for m, p in g.margin.items() if m > 0))
    # Close to the plain bell curve's win chance: the grid only reshapes it.
    assert g.home_win == pytest.approx(win_probability(margin, sigma), abs=0.03)
    assert win_chance(sport, margin, total, sigma, sigma_total) == g.home_win


@pytest.mark.parametrize("case", CASES)
def test_every_score_adds_up_to_the_same_margins_and_totals(case):
    sport, margin, total, sigma, sigma_total = case
    g = grid(sport, margin, total, sigma, sigma_total)
    by_margin: dict[int, float] = {}
    by_total: dict[int, float] = {}
    for (h, a), p in cells(sport, margin, total, sigma, sigma_total).items():
        assert h >= 0 and a >= 0
        by_margin[h - a] = by_margin.get(h - a, 0.0) + p
        by_total[h + a] = by_total.get(h + a, 0.0) + p
    for k in set(by_margin) | set(g.margin):
        assert by_margin.get(k, 0.0) == pytest.approx(g.margin.get(k, 0.0), abs=1e-12)
    for k in set(by_total) | set(g.total):
        assert by_total.get(k, 0.0) == pytest.approx(g.total.get(k, 0.0), abs=1e-12)


def test_a_margin_table_reshapes_the_grid_and_both_paths_agree(tables):
    tables({})
    plain = grid("nba", -4.0, 228.0, 13.5, 19.0)
    tables({"nba": {"margin": {"0": 2.0, "1": 0.7}}})
    shaped = grid("nba", -4.0, 228.0, 13.5, 19.0)
    assert shaped.overtime == pytest.approx(2 * plain.overtime, rel=0.05)
    assert shaped.margin[1] < plain.margin[1]
    assert _mean(shaped.margin) == pytest.approx(-4.0, abs=1e-3)
    from_cells: dict[int, float] = {}
    for (h, a), p in cells("nba", -4.0, 228.0, 13.5, 19.0).items():
        from_cells[h - a] = from_cells.get(h - a, 0.0) + p
    for k, p in shaped.margin.items():
        assert from_cells.get(k, 0.0) == pytest.approx(p, abs=1e-12)


def test_a_score_table_moves_chance_onto_its_scores_without_moving_the_means(tables):
    tables({})
    plain = grid("nfl", 3.0, 44.0, 13.5, 13.5)
    tables({"nfl": {"margin": {"3": 2.0, "7": 1.5}, "score": {"17": 1.4, "20": 1.5}}})
    shaped = grid("nfl", 3.0, 44.0, 13.5, 13.5)
    assert shaped.margin[3] > 1.5 * plain.margin[3]
    assert shaped.total[37] > plain.total[37]  # 20-17
    assert _mean(shaped.margin) == pytest.approx(3.0, abs=1e-3)
    assert _mean(shaped.total) == pytest.approx(44.0, abs=1e-3)


def test_an_overtime_table_can_end_level_and_can_ignore_the_favourite(tables):
    table = [[3, 0, 0.6], [6, 0, 0.3], [0, 0, 0.1]]
    tables({"nfl": {"overtime": table, "overtime_edge": 0.0}})
    coin = grid("nfl", 7.0, 44.0, 13.5, 13.5)
    assert coin.margin[0] == pytest.approx(0.1 * coin.overtime)  # a tie survives overtime
    tables({"nfl": {"overtime": table, "overtime_edge": 1.0}})
    full = grid("nfl", 7.0, 44.0, 13.5, 13.5)
    # With the whole edge the favourite takes more of the level games than with none.
    edge = lambda g: g.margin[3] - g.margin[-3]  # noqa: E731
    assert edge(full) > edge(coin)
    assert full.home_win > coin.home_win


def test_nothing_to_lay_out_gives_none():
    assert grid("nfl", 3.0, 44.0, 0.0, 13.5) is None
    assert grid("nfl", 3.0, 0.0, 13.5, 13.5) is None
    assert cells("nfl", 3.0, 44.0, 13.5, -1.0) == {}
    assert win_chance("nfl", 3.0, None, 13.5, 13.5) is None


def test_lines_split_into_above_on_and_below():
    pmf = {-3: 0.1, 0: 0.2, 3: 0.3, 7: 0.4}
    assert split(pmf, 3) == {"above": 0.4, "push": 0.3, "below": pytest.approx(0.3)}
    assert without_push(split(pmf, 3)) == (pytest.approx(0.4 / 0.7), pytest.approx(0.3 / 0.7))
    assert without_push({"above": 0.0, "push": 1.0, "below": 0.0}) is None
    g = grid("nfl", 3.0, 44.0, 13.5, 13.5)
    home = cover(g, -2.5)
    assert home["push"] == 0.0 and home["above"] == pytest.approx(
        sum(p for m, p in g.margin.items() if m > 2.5)
    )
    assert over(g, 44.0)["push"] == pytest.approx(g.total[44])


def test_spread_memberships_share_a_margin_between_the_centres_either_side():
    centres = [0.0, 3.0, 6.5, 10.0]
    assert grid_module.spread_memberships(centres, 0.0) == [1.0, 0.0, 0.0, 0.0]
    assert grid_module.spread_memberships(centres, -3.0) == [0.0, 1.0, 0.0, 0.0]
    half = grid_module.spread_memberships(centres, 4.75)
    assert half == pytest.approx([0.0, 0.5, 0.5, 0.0])
    assert grid_module.spread_memberships(centres, 21.0) == [0.0, 0.0, 0.0, 1.0]


def test_the_margin_table_can_move_with_the_expected_margin(tables):
    base = {"margin": {"3": 2.0}}
    tables({"nfl": base})
    plain_short = grid("nfl", 3.0, 44.0, 13.5, 13.5)
    plain_long = grid("nfl", 14.0, 44.0, 13.5, 13.5)
    tables(
        {
            "nfl": {
                **base,
                "margin_by_spread": [
                    {"centre": 0.0, "adjust": {"3": 1.0}},
                    {"centre": 3.0, "adjust": {"3": 1.3}},
                    {"centre": 14.0, "adjust": {"3": 1.0}},
                ],
            }
        }
    )
    short = grid("nfl", 3.0, 44.0, 13.5, 13.5)
    long = grid("nfl", 14.0, 44.0, 13.5, 13.5)
    # A three-point favourite lands on 3 more; a fourteen-point one is left alone.
    assert short.margin[3] > 1.15 * plain_short.margin[3]
    assert long.margin[3] == pytest.approx(plain_long.margin[3], rel=1e-3)
    assert _mean(short.margin) == pytest.approx(3.0, abs=1e-3)
    # Between two centres the table is interpolated, so a price never jumps.
    near = [grid("nfl", x, 44.0, 13.5, 13.5).margin[3] for x in (2.95, 3.0, 3.05)]
    assert max(near) - min(near) < 0.002
    assert grid_module.margin_weights("nfl", -3.0) == grid_module.margin_weights("nfl", 3.0)
    assert grid_module.margin_weights("nfl", 8.5)[3] == pytest.approx(2.0 * 1.3 ** (1 - 5.5 / 11))
