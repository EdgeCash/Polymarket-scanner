from __future__ import annotations

from dataclasses import replace

import pytest

from scanner.matching import match_games
from scanner.models import GameStatus, League, PeriodMarket, Side
from scanner.periods import (
    PeriodSides,
    decide,
    from_json,
    is_period_pick,
    last_period,
    period_sides,
    to_json,
)
from scanner.polymarket import parse_event
from scanner.scores import parse_scoreboard
from tests.conftest import at, load_fixture

SIDES = PeriodSides(home_abbr="JAX", away_abbr="PHI")


def state(status=GameStatus.LIVE, period=1, period_over=False, home=(), away=(), **changes):
    raw = load_fixture("espn_states.json")
    base = {s.feed_id: s for s in parse_scoreboard(raw, League.NFL, at(0))}["401873001"]
    fields = dict(
        status=status,
        period=period,
        period_over=period_over,
        home_score=sum(home),
        away_score=sum(away),
        home_linescores=tuple(home),
        away_linescores=tuple(away),
        clock_seconds=0.0 if period_over else 300.0,
    )
    fields.update(changes)
    return replace(base, **fields)


def market(period, kind, line, **changes) -> PeriodMarket:
    base = PeriodMarket(
        f"m-{period}-{kind}-{line}", period, kind, line, 0.0695, True, False, True, True
    )
    return replace(base, **changes)


# -- totals -----------------------------------------------------------------------


def test_quarter_total_over_is_decided_as_soon_as_the_points_pass_the_line():
    d = decide(market("1q", "total", 2.5), state(home=(3,), away=(0,)), SIDES)
    assert d is not None and d.side_label == "long" and d.pick == "1Q OVER 2.5"
    assert d.by_period_end is False and d.points == 3
    assert d.detail == "3 points in the 1st quarter so far"
    assert decide(market("1q", "total", 3.5), state(home=(3,), away=(0,)), SIDES) is None


def test_quarter_total_under_needs_the_quarter_to_be_over():
    during = state(home=(3,), away=(0,))
    assert decide(market("1q", "total", 3.5), during, SIDES) is None
    ended = state(period=1, period_over=True, home=(3,), away=(0,))
    d = decide(market("1q", "total", 3.5), ended, SIDES)
    assert d is not None and d.side_label == "short" and d.pick == "1Q UNDER 3.5"
    assert (
        d.by_period_end is True and d.points == 3 and d.detail == "1st quarter ended with 3 points"
    )
    next_quarter = state(period=2, home=(3, 0), away=(0, 0))
    assert decide(market("1q", "total", 3.5), next_quarter, SIDES).pick == "1Q UNDER 3.5"
    over_at_end = decide(market("1q", "total", 2.5), ended, SIDES)
    assert (
        over_at_end.by_period_end is True
        and over_at_end.detail == "1st quarter ended with 3 points"
    )


def test_under_side_follows_which_side_is_the_over():
    ended = state(period=1, period_over=True, home=(3,), away=(0,))
    flipped = decide(market("1q", "total", 3.5, over_is_long=False), ended, SIDES)
    assert flipped.side_label == "long" and flipped.pick == "1Q UNDER 3.5"
    over = decide(market("1q", "total", 2.5, over_is_long=False), ended, SIDES)
    assert over.side_label == "short" and over.pick == "1Q OVER 2.5"


def test_half_total_spans_two_quarters_and_a_push_decides_nothing():
    halftime = state(GameStatus.HALFTIME, period=2, home=(3, 7), away=(14, 3))
    d = decide(market("1h", "total", 24.5), halftime, SIDES)
    assert d.side_label == "long" and d.pick == "1H OVER 24.5" and d.points == 27
    assert d.detail == "1st half ended with 27 points"
    assert decide(market("1h", "total", 27.0), halftime, SIDES) is None  # a push
    assert decide(market("1h", "total", 27.5), halftime, SIDES).pick == "1H UNDER 27.5"
    in_q1 = state(period=1, home=(3,), away=(14,))
    assert decide(market("1h", "total", 27.5), in_q1, SIDES) is None  # half still running
    assert decide(market("1h", "total", 16.5), in_q1, SIDES).pick == "1H OVER 16.5"


def test_fourth_quarter_and_second_half_totals_decide_only_the_over_before_the_final():
    late = state(period=4, home=(3, 7, 7, 10), away=(14, 3, 0, 7))
    assert decide(market("4q", "total", 16.5), late, SIDES).pick == "4Q OVER 16.5"
    assert decide(market("4q", "total", 17.5), late, SIDES) is None
    assert decide(market("2h", "total", 23.5), late, SIDES).pick == "2H OVER 23.5"
    assert decide(market("2h", "total", 24.5), late, SIDES) is None
    final = state(GameStatus.FINAL, period=4, home=(3, 7, 7, 10), away=(14, 3, 0, 7))
    assert decide(market("2h", "total", 24.5), final, SIDES).pick == "2H UNDER 24.5"


def test_team_total_uses_that_teams_points_only():
    sides = replace(SIDES, team_side=Side.HOME)
    halftime = state(GameStatus.HALFTIME, period=2, home=(3, 7), away=(14, 3))
    d = decide(market("1h", "team_total", 10.5), halftime, sides)
    assert d.side_label == "short" and d.pick == "1H JAX UNDER 10.5" and d.pick_side == "home"
    assert d.detail == "1st half ended with JAX 10 points"
    over = decide(market("1h", "team_total", 9.5), halftime, sides)
    assert over.side_label == "long" and over.pick == "1H JAX OVER 9.5"
    assert decide(market("1h", "team_total", 9.5), halftime, SIDES) is None  # team unknown


def test_nothing_is_decided_without_per_period_scores():
    halftime = state(GameStatus.HALFTIME, period=2, home=(), away=(), home_score=10, away_score=17)
    assert decide(market("1h", "total", 24.5), halftime, SIDES) is None
    assert decide(market("1h", "total", 27.5), halftime, SIDES) is None
    spread = market("1q", "spread", 2.5, team_id=1, other_team_id=2, long_line=2.5)
    sides = replace(SIDES, team_side=Side.AWAY, other_side=Side.HOME)
    assert decide(spread, halftime, sides) is None


# -- spreads ----------------------------------------------------------------------


def test_spread_is_decided_only_when_the_span_is_over():
    sides = replace(SIDES, team_side=Side.AWAY, other_side=Side.HOME)  # long = PHI, short = JAX
    dog = market("1q", "spread", 2.5, team_id=1, other_team_id=2, long_line=2.5)
    assert decide(dog, state(period=1, home=(3,), away=(14,)), sides) is None  # Q1 still running
    ended = state(period=1, period_over=True, home=(3,), away=(14,))
    d = decide(dog, ended, sides)
    assert d.side_label == "long" and d.pick == "1Q PHI +2.5" and d.pick_side == "away"
    assert d.by_period_end is True and d.points is None
    assert d.detail == "1st quarter ended PHI 14, JAX 3"
    favourite = market("1q", "spread", -14.5, team_id=1, other_team_id=2, long_line=-14.5)
    d = decide(favourite, ended, sides)
    assert d.side_label == "short" and d.pick == "1Q JAX +14.5" and d.pick_side == "home"
    push = market("1q", "spread", -11, team_id=1, other_team_id=2, long_line=-11.0)
    assert decide(push, ended, sides) is None
    assert decide(dog, ended, SIDES) is None  # teams unknown


def test_half_spread_at_halftime():
    sides = replace(SIDES, team_side=Side.HOME, other_side=Side.AWAY)  # long = JAX
    halftime = state(GameStatus.HALFTIME, period=2, home=(3, 7), away=(14, 3))
    d = decide(
        market("1h", "spread", 7.5, team_id=1, other_team_id=2, long_line=7.5), halftime, sides
    )
    assert d.side_label == "long" and d.pick == "1H JAX +7.5"  # 10 + 7.5 > 17
    d = decide(
        market("1h", "spread", 6.5, team_id=1, other_team_id=2, long_line=6.5), halftime, sides
    )
    assert d.side_label == "short" and d.pick == "1H PHI -6.5"


# -- helpers ----------------------------------------------------------------------


def test_json_round_trip_decides_the_same_way():
    sides = replace(SIDES, team_side=Side.AWAY, other_side=Side.HOME)
    dog = market("1q", "spread", 2.5, team_id=1, other_team_id=2, long_line=2.5)
    data = to_json(dog, sides)
    parsed = from_json(data)
    assert parsed is not None
    back_market, back_sides = parsed
    ended = state(period=1, period_over=True, home=(3,), away=(14,))
    assert decide(back_market, ended, back_sides) == decide(dog, ended, sides)
    assert from_json(None) is None and from_json({"period": "1q"}) is None
    assert from_json({**data, "team_side": "nowhere"}) is None


def test_period_sides_map_polymarket_team_ids_onto_espn_sides():
    game = parse_event(load_fixture("pm_nfl_tb_dal.json")["events"][0], League.NFL)
    espn = state(GameStatus.PRE, period=0)
    espn = replace(
        espn,
        home=replace(espn.home, name="Dallas Cowboys", abbreviation="DAL", location="Dallas"),
        away=replace(
            espn.away, name="Tampa Bay Buccaneers", abbreviation="TB", location="Tampa Bay"
        ),
        kickoff=game.start_time,
    )
    result = match_games([game], [espn])
    assert len(result.matches) == 1
    match = result.matches[0]
    spread = next(m for m in game.period_markets if m.market_slug.endswith("1q-pos-1pt5"))
    sides = period_sides(match, spread)
    assert (sides.home_abbr, sides.away_abbr) == ("DAL", "TB")
    assert sides.team_side is Side.AWAY and sides.other_side is Side.HOME  # TB is the long side
    total = next(m for m in game.period_markets if m.market_slug.endswith("1h-15pt5"))
    assert period_sides(match, total).team_side is None
    assert last_period(spread) == 1 and last_period(total) == 2


def test_period_pick_prefixes():
    assert is_period_pick("1H OVER 24.5") and is_period_pick("3Q JAX +2.5")
    assert not is_period_pick("OVER 47.5") and not is_period_pick("PHI")


@pytest.mark.parametrize(
    "period,last", [("1q", 1), ("2q", 2), ("3q", 3), ("4q", 4), ("1h", 2), ("2h", 4)]
)
def test_last_period_of_each_span(period, last):
    assert last_period(market(period, "total", 1.5)) == last
