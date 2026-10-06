from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from scanner.config import CLINCHED_FAIR_PRICE, load_settings
from scanner.matching import match_games
from scanner.models import (
    AlertType,
    Book,
    BookLevel,
    FairPrice,
    GameStatus,
    League,
    Quote,
    Side,
)
from scanner.polymarket import parse_book, parse_event, parse_quote
from scanner.rules import (
    AlertHistory,
    Decision,
    availability_at_edge,
    evaluate_clinched,
    evaluate_winner,
    pick_clinched,
    polymarket_score_differs,
)
from scanner.scores import parse_event as espn_parse
from scanner.scores import parse_scoreboard
from scanner.tracker import GameTracker
from scanner.winprob import WinProbModel, fair_price
from tests.conftest import at, load_fixture

SLUG = "aec-nfl-phi-jax-2026-10-11"


def phi_jax_match(state):
    game = parse_event(load_fixture("pm_nfl_events.json")["events"][1], League.NFL)
    result = match_games([game], [state])
    assert len(result.matches) == 1
    return result.matches[0]


def live_state(seconds=0.0, **changes):
    raw = load_fixture("espn_states.json")
    states = {s.feed_id: s for s in parse_scoreboard(raw, League.NFL, at(seconds))}
    return replace(states["401873001"], **changes)  # PHI 24 at JAX 14, Q4 3:52, PHI ball


class Scenario:
    """Everything evaluate_winner needs, set up so that every rule passes."""

    def __init__(self, **settings_overrides):
        self.settings = load_settings(**settings_overrides)
        self.state = live_state(20)
        self.match = phi_jax_match(self.state)
        self.tracker = GameTracker()
        self.tracker.update(live_state(0))
        self.tracker.update(self.state)
        self.history = AlertHistory("America/Chicago")
        self.now = at(22)
        self.fair = FairPrice(0.98, 0.985, 0.98, "min with espn")
        self.quote = parse_quote(load_fixture("pm_bbo_late.json"), SLUG, True, 0.0695, True, at(20))
        self.book = parse_book(load_fixture("pm_book_late.json"), SLUG, at(20))

    def run(self, side=Side.AWAY, **overrides) -> Decision:
        args = dict(
            match=self.match,
            state=self.state,
            side=side,
            fair=self.fair,
            quote=self.quote,
            book=self.book,
            settings=self.settings,
            tracker=self.tracker,
            history=self.history,
            now=self.now,
        )
        args.update(overrides)
        return evaluate_winner(**args)


# -- the happy path -----------------------------------------------------------


def test_every_rule_passing_produces_a_winner_alert():
    s = Scenario()
    d = s.run()
    assert d.fired and d.near_miss is None
    a = d.alert
    assert a.alert_type is AlertType.WINNER
    assert a.pick == "PHI" and a.home == "JAX" and a.away == "PHI"
    assert a.market_slug == SLUG and a.side_label == "long"
    assert a.buy_price == pytest.approx(0.94)
    assert a.fair_price == 0.98 and a.model_price == 0.985 and a.espn_price == 0.98
    assert a.fee == pytest.approx(0.0695 * 0.94 * 0.06)
    assert a.edge == pytest.approx(0.98 - 0.94 - 0.0695 * 0.94 * 0.06)
    assert a.dollars_available == pytest.approx(93 + 93.5 + 235)
    assert a.average_price == pytest.approx((93 + 93.5 + 235) / 450)
    assert a.situation["seconds_left"] == 232.0 and a.situation["possession"] == "away"
    assert a.polymarket_score == "24-14" and a.polymarket_score_differs is False
    assert a.enabled is False  # ALERTS_ENABLED defaults to false; the alert is still built


def test_alert_carries_the_switch_state():
    s = Scenario(ALERTS_ENABLED=True)
    assert s.run().alert.enabled is True


# -- rule 1 -------------------------------------------------------------------


def test_rule_1_passes_at_eight_minutes_and_fails_above():
    s = Scenario()
    s.state = replace(s.state, seconds_left=480.0, clock_seconds=480.0)
    s.tracker.update(s.state)
    s.tracker.update(replace(s.state, fetched_at=at(21)))
    s.state = replace(s.state, fetched_at=at(21))
    assert s.run().fired
    s.state = replace(s.state, seconds_left=481.0, clock_seconds=481.0)
    d = s.run()
    assert not d.fired and d.near_miss is None and d.reason == "rule 1: too much time left"


def test_rule_1_fails_outside_the_fourth_quarter_without_a_near_miss():
    s = Scenario()
    d = s.run(state=replace(s.state, period=3, seconds_left=1000.0))
    assert not d.fired and d.near_miss is None and "rule 1" in d.reason
    d = s.run(state=replace(s.state, period=5, seconds_left=0.0))
    assert d.reason == "overtime"


# -- rule 2 -------------------------------------------------------------------


def test_rule_2_passes_at_the_minimum_and_fails_below():
    s = Scenario()
    assert s.run(fair=FairPrice(0.93, 0.93, 0.93, "x")).reason == "rule 3: edge too small"
    d = s.run(fair=FairPrice(0.9299, 0.93, 0.93, "x"))
    assert not d.fired and d.near_miss is None and d.reason == "rule 2: fair price too low"


def test_a_model_espn_disagreement_above_the_bar_is_a_near_miss():
    s = Scenario()
    d = s.run(fair=FairPrice(None, 0.97, 0.90, "disagree: model 0.970 vs espn 0.900"))
    assert not d.fired and d.near_miss is not None
    assert d.near_miss.reason.startswith("no fair price: disagree")
    d = s.run(fair=FairPrice(None, 0.80, 0.90, "disagree"))
    assert d.near_miss is None


# -- always-on checks ---------------------------------------------------------


def test_game_must_be_live_not_delayed_or_in_overtime():
    s = Scenario()
    for status in (GameStatus.DELAYED, GameStatus.HALFTIME, GameStatus.FINAL, GameStatus.UNKNOWN):
        d = s.run(state=replace(s.state, status=status))
        assert not d.fired and d.near_miss is None


def test_stale_score_blocks():
    s = Scenario()
    s.quote = replace(s.quote, fetched_at=at(34))
    s.book = replace(s.book, fetched_at=at(34))
    assert s.run(now=at(35)).fired  # score read 15s ago: still fresh
    d = s.run(now=at(35.5))
    assert d.near_miss.reason == "stale score"


def test_stale_price_blocks():
    s = Scenario()
    s.quote = replace(s.quote, fetched_at=at(17.0))
    assert s.run().fired  # price read 5s ago: still fresh
    s.quote = replace(s.quote, fetched_at=at(16.9))
    assert s.run().near_miss.reason == "stale price"


def test_situation_must_match_on_two_polls():
    s = Scenario()
    s.tracker = GameTracker()
    s.tracker.update(s.state)
    assert s.run().near_miss.reason == "not confirmed on two polls"
    s.tracker = GameTracker()
    s.tracker.update(live_state(0, down=3))
    s.tracker.update(s.state)
    assert s.run().near_miss.reason == "not confirmed on two polls"


def test_no_price_blocks():
    s = Scenario()
    s.quote = replace(s.quote, buy_price=None)
    assert s.run().near_miss.reason == "no price"


def test_market_must_be_open_and_side_tradable():
    s = Scenario()
    assert (
        s.run(quote=replace(s.quote, state="MARKET_STATE_HALTED")).near_miss.reason
        == "market not open"
    )
    assert s.run(quote=replace(s.quote, tradable=False)).near_miss.reason == "market not open"
    closed = replace(s.match, polymarket=replace(s.match.polymarket, moneyline_closed=True))
    assert s.run(match=closed).near_miss.reason == "market not open"


# -- rule 3 -------------------------------------------------------------------


def test_rule_3_edge_boundary():
    s = Scenario()
    fee = 0.0695 * 0.94 * 0.06
    assert s.run(fair=FairPrice(0.94 + fee + 0.03, 0.99, 0.99, "x")).fired
    d = s.run(fair=FairPrice(0.94 + fee + 0.0299, 0.99, 0.99, "x"))
    assert d.near_miss.reason == "rule 3: edge too small"
    assert d.near_miss.edge == pytest.approx(0.0299)


# -- rule 4 -------------------------------------------------------------------


def test_rule_4_deep_book_counts_dollars_while_the_edge_clears():
    levels = parse_book(load_fixture("pm_book_late.json"), SLUG, at(0)).offers
    avail = availability_at_edge(levels, 0.98, 0.0695, 0.03)
    assert avail.contracts == 450 and avail.best_price == pytest.approx(0.93)
    assert avail.dollars == pytest.approx(421.5)
    # a lower fair price stops the walk earlier
    avail = availability_at_edge(levels, 0.97, 0.0695, 0.03)
    assert avail.contracts == 200


def test_rule_4_thin_book_fails():
    s = Scenario()
    s.book = parse_book(load_fixture("pm_book_thin.json"), SLUG, at(20))
    d = s.run()
    assert d.near_miss.reason == "rule 4: not enough for sale"
    assert availability_at_edge(s.book.offers, 0.98, 0.0695, 0.03).dollars == pytest.approx(0.94)


def test_rule_4_exactly_fifty_dollars_passes():
    s = Scenario()
    s.book = Book(SLUG, (), (BookLevel(0.94, 50 / 0.94),), "MARKET_STATE_OPEN", at(20))
    assert s.run().fired


def test_rule_4_needs_a_fresh_book():
    s = Scenario()
    assert s.run(book=None).near_miss.reason == "rule 4: no book"
    assert s.run(book=replace(s.book, fetched_at=at(10))).near_miss.reason == "rule 4: stale book"


# -- rule 5 -------------------------------------------------------------------


def test_rule_5_score_cooldown():
    s = Scenario()
    assert s.run(now=at(20)).fired  # score first seen at t=0, 20s ago
    s.tracker = GameTracker()
    s.tracker.update(live_state(5))  # score first seen 17s before "now"
    s.tracker.update(s.state)
    d = s.run()
    assert d.near_miss.reason == "rule 5: score changed recently"


# -- rule 6 -------------------------------------------------------------------


def test_rule_6_repeat_gap_unless_the_edge_grew_two_cents():
    s = Scenario()
    first = s.run()
    assert first.fired
    s.history.record(first.alert)
    later = at(22 + 60)
    same = s.run(
        now=later,
        state=replace(s.state, fetched_at=at(80)),
        quote=replace(s.quote, fetched_at=at(80)),
        book=replace(s.book, fetched_at=at(80)),
    )
    assert same.near_miss.reason == "rule 6: repeat too soon"
    s.tracker.update(replace(s.state, fetched_at=at(80)))
    better = s.run(
        now=later,
        state=replace(s.state, fetched_at=at(80)),
        quote=replace(s.quote, fetched_at=at(80)),
        book=replace(s.book, fetched_at=at(80)),
        fair=FairPrice(0.98 + 0.02, 0.99, 0.99, "x"),
    )
    assert better.fired
    after_gap = s.run(
        now=at(22 + 301),
        state=replace(s.state, fetched_at=at(320)),
        quote=replace(s.quote, fetched_at=at(320)),
        book=replace(s.book, fetched_at=at(320)),
    )
    assert after_gap.fired
    # a different team in the same game is not a repeat
    assert s.history.last("nfl", "401873001", "JAX") is None


# -- rule 7 -------------------------------------------------------------------


def test_rule_7_daily_cap_and_the_once_a_day_paused_message():
    s = Scenario(MAX_ALERTS_PER_DAY=2)
    first = s.run()
    assert first.fired
    s.history.record(replace(first.alert, pick="OVER 41.5"))
    assert s.run().fired  # one alert so far today
    s.history.record(replace(first.alert, pick="OVER 47.5"))
    assert s.history.count_today(s.now) == 2
    third = s.run(now=at(23))
    assert third.near_miss.reason == "rule 7: daily cap"
    assert s.history.paused_message_due(s.now, 2) is True
    assert s.history.paused_message_due(s.now, 2) is False
    # a new local day resets the count
    tomorrow = s.now + timedelta(days=1)
    assert s.history.count_today(tomorrow) == 0


def test_daily_cap_counts_local_days_in_the_configured_zone():
    history = AlertHistory("America/Chicago")
    # 04:30 UTC on the 12th is 23:30 on the 11th in Chicago
    late_evening = datetime.fromisoformat("2026-10-12T04:30:00+00:00")
    s = Scenario()
    alert = s.run().alert
    alert.created_at = late_evening
    history.record(alert)
    assert history.count_today(late_evening) == 1
    assert history.count_today(datetime.fromisoformat("2026-10-12T05:30:00+00:00")) == 0


# -- polymarket score ---------------------------------------------------------


def test_polymarket_score_difference_is_recorded():
    state = live_state()
    assert polymarket_score_differs("24-14", state) is False
    assert polymarket_score_differs("14-24", state) is False
    assert polymarket_score_differs("17-14", state) is True
    assert polymarket_score_differs(None, state) is False
    assert polymarket_score_differs("garbage", state) is False


# -- clinched overs -----------------------------------------------------------


class ClinchScenario:
    def __init__(self, away=31, home=21, **settings_overrides):
        self.settings = load_settings(**settings_overrides)
        self.state = live_state(70, away_score=away, home_score=home)
        self.match = phi_jax_match(self.state)
        self.total = next(t for t in self.match.polymarket.totals if t.line == 47.5)
        self.tracker = GameTracker()
        self.tracker.update(live_state(0, away_score=away, home_score=home))
        self.tracker.update(self.state)
        self.history = AlertHistory("America/Chicago")
        self.now = at(72)
        self.quote = Quote(
            self.total.market_slug,
            "long",
            0.96,
            0.95,
            0.96,
            "MARKET_STATE_OPEN",
            True,
            0.0695,
            at(70),
        )
        self.book = Book(
            self.total.market_slug,
            (BookLevel(0.95, 100.0),),
            (BookLevel(0.96, 150.0), BookLevel(0.965, 100.0), BookLevel(0.99, 5000.0)),
            "MARKET_STATE_OPEN",
            at(70),
        )

    def run(self, **overrides) -> Decision:
        args = dict(
            match=self.match,
            state=self.state,
            total=self.total,
            quote=self.quote,
            book=self.book,
            settings=self.settings,
            tracker=self.tracker,
            history=self.history,
            now=self.now,
        )
        args.update(overrides)
        return evaluate_clinched(**args)


def test_clinched_over_alert_when_the_score_is_over_the_line():
    c = ClinchScenario()  # 31-21 = 52 > 47.5
    d = c.run()
    assert d.fired
    a = d.alert
    assert a.alert_type is AlertType.CLINCHED_OVER
    assert a.pick == "OVER 47.5" and a.line == 47.5 and a.combined_score == 52
    assert a.fair_price == CLINCHED_FAIR_PRICE
    assert a.buy_price == 0.96
    assert a.edge == pytest.approx(0.995 - 0.96 - 0.0695 * 0.96 * 0.04)
    assert a.dollars_available == pytest.approx(0.96 * 150 + 0.965 * 100)
    assert a.situation["score_age_seconds"] == 72


def test_clinched_over_one_point_under_the_line_does_nothing():
    c = ClinchScenario(away=24, home=23)  # 47 is not over 47.5
    d = c.run()
    assert not d.fired and d.near_miss is None and d.reason == "not clinched"


def test_clinched_over_exactly_half_point_over_counts():
    c = ClinchScenario(away=24, home=24)  # 48 > 47.5
    assert c.run().fired


def test_clinched_over_waits_for_the_score_to_stand_sixty_seconds():
    c = ClinchScenario()
    c.state = live_state(55, away_score=31, home_score=21)
    c.tracker = GameTracker()
    c.tracker.update(live_state(0, away_score=31, home_score=21))
    c.tracker.update(c.state)
    c.quote = replace(c.quote, fetched_at=at(58))
    c.book = replace(c.book, fetched_at=at(58))
    d = c.run(now=at(59))
    assert d.near_miss.reason == "clinch cooldown: score changed recently"
    assert c.run(now=at(60)).fired


def test_clinched_over_reversed_inside_the_cooldown_does_not_alert():
    c = ClinchScenario()
    # Touchdown at t=0 made it 31-21 (clinched). Review takes it back at t=30.
    reversed_state = live_state(30, away_score=24, home_score=21)
    c.tracker.update(reversed_state)
    d = c.run(state=reversed_state, now=at(32))
    assert not d.fired and d.reason == "not clinched"
    # The touchdown stands again at t=40: the clock restarts from the latest change.
    again = live_state(40, away_score=31, home_score=21)
    c.tracker.update(again)
    c.tracker.update(live_state(45, away_score=31, home_score=21))
    d = c.run(
        state=live_state(45, away_score=31, home_score=21),
        now=at(70),
        quote=replace(c.quote, fetched_at=at(68)),
        book=replace(c.book, fetched_at=at(68)),
    )
    assert d.near_miss.reason == "clinch cooldown: score changed recently"


def test_clinched_over_two_lines_keeps_the_one_with_most_dollars():
    c = ClinchScenario()
    lines = {t.line: t for t in c.match.polymarket.totals}
    assert set(lines) == {41.5, 47.5}
    low = lines[41.5]
    low_quote = replace(c.quote, market_slug=low.market_slug)
    low_book = Book(
        low.market_slug,
        (),
        (BookLevel(0.96, 30.0), BookLevel(0.97, 30.0)),
        "MARKET_STATE_OPEN",
        at(70),
    )
    d_low = c.run(total=low, quote=low_quote, book=low_book)
    d_high = c.run()
    assert d_low.fired and d_high.fired
    best = pick_clinched([d_low, d_high])
    assert best is d_high
    assert pick_clinched([Decision(None, None, "x")]) is None


def test_clinched_over_edge_and_size_rules():
    c = ClinchScenario()
    pricey = replace(c.quote, buy_price=0.974)  # edge 1.9c after a 0.18c fee
    assert c.run(quote=pricey).near_miss.reason == "edge too small"
    assert c.run(quote=replace(c.quote, buy_price=0.973)).fired  # edge 2.0c
    thin = Book(c.total.market_slug, (), (BookLevel(0.96, 10.0),), "MARKET_STATE_OPEN", at(70))
    assert c.run(book=thin).near_miss.reason == "rule 4: not enough for sale"
    assert c.run(book=None).near_miss.reason == "rule 4: no book"


def test_clinched_over_always_on_checks():
    c = ClinchScenario()
    assert (
        c.run(quote=replace(c.quote, state="MARKET_STATE_HALTED")).near_miss.reason
        == "market not open"
    )
    assert c.run(total=replace(c.total, closed=True)).near_miss.reason == "market not open"
    assert c.run(total=replace(c.total, over_tradable=False)).near_miss.reason == "market not open"
    assert c.run(quote=replace(c.quote, fetched_at=at(60))).near_miss.reason == "stale price"
    assert c.run(now=at(90)).near_miss.reason == "stale score"
    assert c.run(state=replace(c.state, status=GameStatus.DELAYED)).reason == "game is delayed"
    assert c.run(state=replace(c.state, status=GameStatus.FINAL)).reason == "game is final"
    overtime = replace(c.state, period=5, seconds_left=0.0)
    ot_tracker = GameTracker()
    ot_tracker.update(replace(overtime, fetched_at=at(0)))
    ot_tracker.update(overtime)
    assert c.run(state=overtime, tracker=ot_tracker).fired  # overtime points count
    fresh_tracker = GameTracker()
    fresh_tracker.update(c.state)
    assert c.run(tracker=fresh_tracker).reason == "clinch cooldown: score changed recently"


def test_clinched_over_repeat_gap_and_daily_cap():
    c = ClinchScenario(MAX_ALERTS_PER_DAY=1)
    first = c.run()
    assert first.fired
    c.history.record(first.alert)
    assert c.run(now=at(73)).near_miss.reason == "rule 6: repeat too soon"
    c.history = AlertHistory("America/Chicago")
    c.history.record(first.alert)
    other = replace(first.alert, pick="OVER 41.5")
    c.history = AlertHistory("America/Chicago")
    c.history.record(other)
    assert c.run(now=at(73)).near_miss.reason == "rule 7: daily cap"


def test_no_path_can_alert_on_an_under():
    c = ClinchScenario()
    under_quote = replace(c.quote, side_label="short", buy_price=0.04)
    d = c.run(quote=under_quote)
    assert not d.fired and d.reason == "quote is not the Over side of this total"
    flipped_total = replace(c.total, over_is_long=False)  # the Over would be the short side
    d = c.run(total=flipped_total)  # but the quote is still for the long side
    assert not d.fired and d.reason == "quote is not the Over side of this total"
    wrong_market = replace(c.quote, market_slug="some-other-market")
    assert not c.run(quote=wrong_market).fired
    for decision in (
        c.run(),
        c.run(total=flipped_total, quote=replace(c.quote, side_label="short")),
    ):
        if decision.alert is not None:
            assert decision.alert.pick.startswith("OVER")


def test_clinched_switch_off_leaves_winner_alerts_working():
    c = ClinchScenario(CLINCHED_OVERS_ENABLED=False)
    d = c.run()
    assert not d.fired and d.reason == "clinched overs disabled"
    s = Scenario(CLINCHED_OVERS_ENABLED=False)
    assert s.run().fired


# -- replay -------------------------------------------------------------------


def test_replay_of_a_recorded_late_game_sequence_produces_the_expected_alerts():
    fixture = load_fixture("replay_phi_jax.json")
    t0 = datetime.fromisoformat(fixture["t0"])
    settings = load_settings()
    model = WinProbModel.load()
    game = parse_event(
        load_fixture("pm_nfl_events.json")["events"][fixture["polymarket_event_index"]], League.NFL
    )
    tracker = GameTracker()
    history = AlertHistory(settings.TZ)
    fired: list[int] = []
    reasons: dict[int, str] = {}
    for poll in fixture["polls"]:
        now = t0 + timedelta(seconds=poll["t"])
        state = espn_parse(poll["espn_event"], League.NFL, now)
        tracker.update(state)
        match = match_games([game], [state]).matches[0]
        team = match.market_team_for(Side.AWAY)
        quote = parse_quote(
            poll["bbo"], game.moneyline_slug, team.is_long, game.moneyline_theta, True, now
        )
        book = parse_book(poll["book"], game.moneyline_slug, now)
        fair = fair_price(state, Side.AWAY, settings, model)
        decision = evaluate_winner(
            match=match,
            state=state,
            side=Side.AWAY,
            fair=fair,
            quote=quote,
            book=book,
            settings=settings,
            tracker=tracker,
            history=history,
            now=now,
        )
        reasons[poll["t"]] = decision.reason
        if decision.fired:
            history.record(decision.alert)
            fired.append(poll["t"])
    assert fired == fixture["expected_alert_polls"] == [20, 30, 340]
    assert reasons[0] == "not confirmed on two polls"
    assert reasons[5].startswith("rule 5")
    assert reasons[25].startswith("rule 6")
    # After the Jacksonville touchdown the model and ESPN disagree and the model is
    # below the bar, so there is no fair price and no near miss.
    assert reasons[35].startswith("no fair price") and reasons[40].startswith("no fair price")
    assert reasons[80].startswith("rule 6")
    assert reasons[85].startswith("rule 4")
    assert reasons[345].startswith("rule 6")


# -- quarter and half markets -------------------------------------------------------


class PeriodScenario:
    """A halftime state where the first-half Over 24.5 is decided and every rule passes."""

    def __init__(self, **settings_overrides):
        from scanner.models import PeriodMarket
        from scanner.periods import decide, period_sides

        self.settings = load_settings(**settings_overrides)
        base = live_state(0)
        self.state0 = replace(
            base,
            status=GameStatus.HALFTIME,
            period=2,
            clock_seconds=0.0,
            seconds_left=1800.0,
            home_score=10,
            away_score=17,
            home_linescores=(3, 7),
            away_linescores=(14, 3),
        )
        self.state = replace(self.state0, fetched_at=at(68))
        self.match = phi_jax_match(self.state)
        self.tracker = GameTracker()
        self.tracker.update(self.state0)
        self.tracker.update(self.state)
        self.history = AlertHistory("America/Chicago")
        self.now = at(70)
        self.market = PeriodMarket(
            "tsc-nfl-phi-jax-2026-10-11-1h-24pt5",
            "1h",
            "total",
            24.5,
            0.0695,
            True,
            False,
            True,
            True,
        )
        self.sides = period_sides(self.match, self.market)
        self.decision = decide(self.market, self.state, self.sides)
        self.quote = parse_quote(
            load_fixture("pm_bbo_late.json"), self.market.market_slug, True, 0.0695, True, at(68)
        )
        self.book = parse_book(load_fixture("pm_book_late.json"), self.market.market_slug, at(68))

    def run(self, **overrides) -> Decision:
        from scanner.rules import evaluate_period

        args = dict(
            match=self.match,
            state=self.state,
            market=self.market,
            decision=self.decision,
            sides=self.sides,
            quote=self.quote,
            book=self.book,
            settings=self.settings,
            tracker=self.tracker,
            history=self.history,
            now=self.now,
        )
        args.update(overrides)
        return evaluate_period(**args)


def test_decided_half_total_produces_a_period_alert_that_is_recorded_not_sent():
    s = PeriodScenario()
    assert s.decision is not None and s.decision.pick == "1H OVER 24.5"
    d = s.run()
    assert d.fired and d.near_miss is None
    a = d.alert
    assert a.alert_type is AlertType.PERIOD and a.pick == "1H OVER 24.5" and a.side_label == "long"
    assert a.market_slug == s.market.market_slug and a.home == "JAX" and a.away == "PHI"
    assert a.fair_price == CLINCHED_FAIR_PRICE and a.buy_price == pytest.approx(0.94)
    assert a.edge == pytest.approx(0.995 - 0.94 - 0.0695 * 0.94 * 0.06)
    assert a.line == 24.5 and a.combined_score == 27 and a.pick_side is None
    # At a 99.5c fair price the 96c level still clears the 2c clinched edge; 99c does not.
    assert a.dollars_available == pytest.approx(93 + 93.5 + 235 + 1920)
    assert a.situation["period_by_end"] is True and a.situation["decided_age_seconds"] == 70
    assert a.situation["period_detail"] == "1st half ended with 27 points"
    assert a.situation["period_market"]["period"] == "1h"
    assert a.enabled is False
    assert PeriodScenario(ALERTS_ENABLED=True).run().alert.enabled is False
    both = PeriodScenario(ALERTS_ENABLED=True, PERIOD_ALERTS_ENABLED=True)
    assert both.run().alert.enabled is True


def test_period_alert_waits_for_the_span_to_have_been_over_for_the_cooldown():
    s = PeriodScenario()
    d = s.run(now=at(30), quote=replace(s.quote, fetched_at=at(29)))
    assert not d.fired and d.near_miss.reason == "clinch cooldown: period just ended"
    s = PeriodScenario(CLINCH_COOLDOWN_SECONDS=100)
    assert s.run().near_miss.reason == "clinch cooldown: period just ended"


def test_over_passed_mid_half_waits_for_the_score_cooldown():
    from scanner.periods import decide

    s = PeriodScenario()
    running = replace(s.state0, status=GameStatus.LIVE, period=2, clock_seconds=200.0)
    s.tracker = GameTracker()
    s.tracker.update(running)
    later = replace(running, fetched_at=at(68))
    s.tracker.update(later)
    s.state = later
    s.decision = decide(s.market, later, s.sides)
    assert s.decision.by_period_end is False
    d = s.run(now=at(30), quote=replace(s.quote, fetched_at=at(29)))
    assert d.near_miss.reason == "clinch cooldown: score changed recently"
    assert s.run().fired


def test_period_rules_refuse_the_wrong_side_closed_markets_and_the_switch():
    s = PeriodScenario()
    wrong = parse_quote(
        load_fixture("pm_bbo_late.json"), s.market.market_slug, False, 0.0695, True, at(68)
    )
    d = s.run(quote=wrong)
    assert not d.fired and d.near_miss is None and "not the decided side" in d.reason
    d = s.run(market=replace(s.market, closed=True))
    assert d.near_miss.reason == "market not open"
    d = s.run(market=replace(s.market, long_tradable=False))
    assert d.near_miss.reason == "market not open"
    d = PeriodScenario(PERIOD_MARKETS_ENABLED=False).run()
    assert not d.fired and d.near_miss is None and d.reason == "period markets disabled"
    d = s.run(state=replace(s.state, status=GameStatus.FINAL))
    assert not d.fired and d.near_miss is None and d.reason == "game is final"


def test_period_rules_edge_book_repeat_and_cap():
    s = PeriodScenario()
    dear = parse_quote(
        {
            "marketData": {
                "bestBid": {"value": "0.98"},
                "bestAsk": {"value": "0.985"},
                "state": "MARKET_STATE_OPEN",
            }
        },
        s.market.market_slug,
        True,
        0.0695,
        True,
        at(68),
    )
    assert s.run(quote=dear).near_miss.reason == "edge too small"
    assert s.run(book=None).near_miss.reason == "rule 4: no book"
    assert s.run(book=replace(s.book, fetched_at=at(50))).near_miss.reason == "rule 4: stale book"
    thin = parse_book(
        {
            "marketData": {
                "bids": [],
                "offers": [
                    {"px": {"value": "0.9400"}, "qty": "1.0000"},
                    {"px": {"value": "0.9500"}, "qty": "20.0000"},
                ],
                "state": "MARKET_STATE_OPEN",
            }
        },
        s.market.market_slug,
        at(68),
    )
    assert s.run(book=thin).near_miss.reason == "rule 4: not enough for sale"
    first = s.run()
    s.history.record(first.alert)
    again = s.run(
        now=at(130),
        state=replace(s.state, fetched_at=at(128)),
        quote=replace(s.quote, fetched_at=at(128)),
        book=replace(s.book, fetched_at=at(128)),
    )
    assert again.near_miss.reason == "rule 6: repeat too soon"
    # The cap only applies once period alerts can reach the phone.
    assert PeriodScenario(MAX_ALERTS_PER_DAY=0).run().fired
    capped = PeriodScenario(MAX_ALERTS_PER_DAY=0, PERIOD_ALERTS_ENABLED=True)
    assert capped.run().near_miss.reason == "rule 7: daily cap"
