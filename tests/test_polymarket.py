from __future__ import annotations

import pytest

from scanner.models import League
from scanner.polymarket import (
    PolymarketError,
    PolymarketReader,
    RateLimited,
    RateLimiter,
    buy_levels,
    parse_book,
    parse_event,
    parse_quote,
    walk_book,
)
from tests.conftest import at, load_fixture


class FakeTransport:
    """Serves fixtures by path and records every request."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path, query=None):
        self.calls.append((path, query))
        if path == "/v2/leagues":
            page = (query or {}).get("offset", 0) // 50
            return load_fixture(f"pm_leagues_page{page}.json")
        result = self.routes.get(path)
        if result is None:
            raise PolymarketError(f"no fixture for {path}")
        if isinstance(result, Exception):
            raise result
        return result


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make_reader(routes=None, clock=None, max_rps=5.0):
    clock = clock or FakeClock()
    transport = FakeTransport(routes or {})
    reader = PolymarketReader(
        transport=transport, max_rps=max_rps, clock=clock, sleep=clock.sleep, now=lambda: at(0)
    )
    return reader, transport, clock


# -- event parsing ------------------------------------------------------------


def nfl_events():
    return load_fixture("pm_nfl_events.json")["events"]


def test_parses_a_normal_game_with_moneyline_and_game_totals():
    game = parse_event(nfl_events()[0], League.NFL)
    assert game is not None
    assert game.event_slug == "nfl-atl-no-2026-10-05"
    assert game.moneyline_slug == "aec-nfl-atl-no-2026-10-05"
    assert game.moneyline_theta == pytest.approx(0.0695)
    assert game.moneyline_active and not game.moneyline_closed
    assert {t.abbreviation for t in game.teams} == {"ATL", "NO"}
    long_team = game.team_on(True)
    short_team = game.team_on(False)
    assert long_team.abbreviation == "ATL"
    assert short_team.abbreviation == "NO"
    assert game.start_time.isoformat() == "2026-10-06T00:15:00+00:00"
    # only full-game totals, in line order; team totals and half totals are ignored
    assert [t.line for t in game.totals] == [44.5, 47.5, 50.5]
    assert all(t.over_is_long for t in game.totals)
    assert all(t.market_slug.endswith(("44pt5", "47pt5", "50pt5")) for t in game.totals)


def test_long_and_short_pricing_rule_holds_in_the_event_feed():
    """Long-side quote == best ask, short-side quote == 1 - best bid, on every moneyline."""
    checked = 0
    for raw in nfl_events() + load_fixture("pm_cfb_events.json")["events"]:
        for market in raw["markets"]:
            if market["sportsMarketType"] != "football_team_full_game_winner":
                continue
            if not market.get("bestBidQuote") or not market.get("bestAskQuote"):
                continue
            bid = float(market["bestBidQuote"]["value"])
            ask = float(market["bestAskQuote"]["value"])
            for side in market["marketSides"]:
                if side.get("quote") is None:
                    continue
                quote = float(side["quote"]["value"])
                expected = ask if side["long"] else 1 - bid
                assert quote == pytest.approx(expected, abs=1e-6)
                checked += 1
    assert checked >= 6


def test_live_event_records_polymarket_scoreboard_but_nothing_else_uses_it():
    game = parse_event(nfl_events()[1], League.NFL)
    assert game.live is True
    assert game.display_score == "24-14"
    assert game.display_period == "Q4"
    assert game.display_elapsed == "56:08"


def test_game_with_no_moneyline_market_has_no_moneyline_slug():
    game = parse_event(nfl_events()[2], League.NFL)
    assert game is not None
    assert game.moneyline_slug is None
    assert game.moneyline_closed is True
    assert len(game.totals) == 1  # totals are still read


def test_closed_moneyline_is_flagged_closed():
    game = parse_event(nfl_events()[3], League.NFL)
    assert game.moneyline_slug is not None
    assert game.moneyline_closed is True
    assert game.moneyline_active is False
    assert game.ended is True


def test_missing_fee_coefficient_falls_back_to_the_published_taker_rate():
    game = parse_event(nfl_events()[5], League.NFL)
    assert game.moneyline_theta == pytest.approx(0.0695)


def test_total_without_over_under_labels_is_skipped():
    game = parse_event(nfl_events()[5], League.NFL)
    assert [t.line for t in game.totals] == [47.5]


def test_event_without_two_teams_is_skipped():
    assert parse_event(nfl_events()[6], League.NFL) is None


def test_cfb_events_parse_with_several_total_lines_each():
    for raw in load_fixture("pm_cfb_events.json")["events"]:
        game = parse_event(raw, League.CFB)
        assert game is not None
        assert game.league is League.CFB
        assert game.moneyline_slug
        assert len(game.totals) >= 2
        for total in game.totals:
            assert total.over_is_long is True
            assert total.line % 1 == 0.5


# -- quotes -------------------------------------------------------------------


def test_quote_long_side_is_best_ask_and_short_side_is_one_minus_best_bid():
    raw = load_fixture("pm_bbo_open.json")
    long_q = parse_quote(raw, "aec-nfl-atl-no-2026-10-05", True, 0.0695, True, at(0))
    short_q = parse_quote(raw, "aec-nfl-atl-no-2026-10-05", False, 0.0695, True, at(0))
    assert long_q.buy_price == pytest.approx(0.4650)
    assert short_q.buy_price == pytest.approx(0.5375)
    assert long_q.best_bid == pytest.approx(0.4625)
    assert long_q.best_ask == pytest.approx(0.4650)
    assert long_q.is_open and short_q.is_open
    assert long_q.fetched_at == at(0)


def test_quote_with_none_prices_means_no_price_no_alert():
    raw = load_fixture("pm_bbo_none.json")
    for is_long in (True, False):
        q = parse_quote(raw, "x", is_long, 0.0695, True, at(0))
        assert q.buy_price is None
        assert q.best_bid is None and q.best_ask is None


def test_quote_on_halted_market_is_not_open():
    raw = load_fixture("pm_bbo_halted.json")
    q = parse_quote(raw, "x", True, 0.0695, True, at(0))
    assert q.buy_price is not None
    assert q.is_open is False


def test_quote_disagreeing_with_the_feeds_own_quote_is_refused():
    raw = load_fixture("pm_bbo_contradictory.json")
    short_q = parse_quote(raw, "x", False, 0.0695, True, at(0))
    assert short_q.buy_price is None
    long_q = parse_quote(raw, "x", True, 0.0695, True, at(0))
    assert long_q.buy_price == pytest.approx(0.4650)


def test_quote_on_untradable_side_is_not_open():
    raw = load_fixture("pm_bbo_open.json")
    q = parse_quote(raw, "x", True, 0.0695, False, at(0))
    assert q.is_open is False


def test_quote_from_garbage_payload_is_empty():
    q = parse_quote({"marketData": None}, "x", True, 0.0695, True, at(0))
    assert q.buy_price is None and q.state is None


# -- books --------------------------------------------------------------------


def test_book_levels_are_sorted_and_short_side_is_mirrored():
    book = parse_book(load_fixture("pm_book_deep.json"), "aec-nfl-atl-no-2026-10-05", at(0))
    assert book.state == "MARKET_STATE_OPEN"
    assert [lvl.price for lvl in book.offers][:3] == pytest.approx([0.4650, 0.4675, 0.4700])
    assert [lvl.price for lvl in book.bids][:2] == pytest.approx([0.4625, 0.4600])
    short_levels = buy_levels(book, False)
    assert [lvl.price for lvl in short_levels][:2] == pytest.approx([0.5375, 0.5400])
    assert short_levels[0].quantity == book.bids[0].quantity
    assert buy_levels(book, True) == book.offers


def test_walk_book_adds_dollars_at_or_below_the_ceiling():
    book = parse_book(load_fixture("pm_book_late.json"), "x", at(0))
    levels = buy_levels(book, True)
    avail = walk_book(levels, 0.94)
    assert avail.contracts == pytest.approx(450)
    assert avail.dollars == pytest.approx(100 * 0.93 + 100 * 0.935 + 250 * 0.94)
    assert avail.best_price == pytest.approx(0.93)
    assert avail.average_price == pytest.approx(avail.dollars / 450)
    nothing = walk_book(levels, 0.92)
    assert nothing.dollars == 0 and nothing.average_price is None and nothing.best_price is None


def test_thin_book_shows_one_cheap_contract_with_nothing_behind_it():
    book = parse_book(load_fixture("pm_book_thin.json"), "x", at(0))
    avail = walk_book(buy_levels(book, True), 0.94)
    assert avail.contracts == 1
    assert avail.dollars == pytest.approx(0.94)


def test_closed_book_is_empty_and_closed():
    book = parse_book(load_fixture("pm_book_closed.json"), "x", at(0))
    assert book.state == "MARKET_STATE_CLOSED"
    assert book.bids == () and book.offers == ()


# -- reader -------------------------------------------------------------------


def test_discover_leagues_pages_until_college_football_is_found():
    reader, transport, _ = make_reader()
    found = reader.discover_leagues()
    assert found == {League.NFL: "nfl", League.CFB: "cfb"}
    offsets = [q["offset"] for path, q in transport.calls if path == "/v2/leagues"]
    assert offsets == [0, 50, 100]


def test_discover_leagues_fails_loudly_without_college_football(monkeypatch):
    reader, transport, _ = make_reader()
    page1 = load_fixture("pm_leagues_page1.json")
    page1["leagues"] = [lg for lg in page1["leagues"] if lg["slug"] != "cfb"]
    original = transport.get

    def get(path, query=None):
        if path == "/v2/leagues" and (query or {}).get("offset") == 50:
            transport.calls.append((path, query))
            return page1
        return original(path, query)

    transport.get = get
    with pytest.raises(PolymarketError):
        reader.discover_leagues()


def test_list_games_parses_every_usable_event():
    reader, transport, _ = make_reader(
        {"/v2/leagues/nfl/events": load_fixture("pm_nfl_events.json")}
    )
    games = reader.list_games(League.NFL, "nfl")
    assert len(games) == 6  # seven in the fixture, one has no teams
    assert transport.calls[0] == ("/v2/leagues/nfl/events", {"limit": 100, "offset": 0})


def test_quotes_for_game_prices_both_teams_from_one_read():
    reader, transport, _ = make_reader(
        {
            "/v2/leagues/nfl/events": load_fixture("pm_nfl_events.json"),
            "/v1/markets/aec-nfl-atl-no-2026-10-05/bbo": load_fixture("pm_bbo_open.json"),
        }
    )
    game = reader.list_games(League.NFL, "nfl")[0]
    quotes = reader.quotes_for_game(game)
    assert quotes["ATL"].buy_price == pytest.approx(0.4650)
    assert quotes["NO"].buy_price == pytest.approx(0.5375)
    assert sum(1 for p, _ in transport.calls if p.endswith("/bbo")) == 1


def test_over_quote_uses_the_over_side():
    reader, _, _ = make_reader(
        {
            "/v2/leagues/nfl/events": load_fixture("pm_nfl_events.json"),
            "/v1/markets/tsc-nfl-atl-no-2026-10-05-total-47pt5/bbo": {
                "marketData": {
                    "bestBid": {"value": "0.9500", "currency": "USD"},
                    "bestAsk": {"value": "0.9600", "currency": "USD"},
                    "state": "MARKET_STATE_OPEN",
                }
            },
        }
    )
    game = reader.list_games(League.NFL, "nfl")[0]
    total = next(t for t in game.totals if t.line == 47.5)
    q = reader.over_quote(total)
    assert q.buy_price == pytest.approx(0.96)
    assert q.side_label == "long"


def test_reader_stays_under_five_requests_per_second():
    clock = FakeClock()
    reader, transport, _ = make_reader(
        {"/v1/markets/x/bbo": load_fixture("pm_bbo_open.json")}, clock=clock
    )
    stamps = []
    for _ in range(25):
        reader.quote("x", True, 0.0695)
        stamps.append(clock.t)
    # In any one-second window there are at most 5 requests.
    for i, start in enumerate(stamps):
        in_window = sum(1 for s in stamps[i:] if s < start + 1.0)
        assert in_window <= 5
    assert reader.request_count == 25
    # 25 requests at 5/s need at least 4.8 seconds of spacing.
    assert stamps[-1] - stamps[0] >= 4.8


def test_rate_limiter_spaces_calls_evenly():
    clock = FakeClock()
    limiter = RateLimiter(max_rps=5.0, clock=clock, sleep=clock.sleep)
    limiter.wait()
    limiter.wait()
    limiter.wait()
    assert clock.sleeps == pytest.approx([0.2, 0.2])


def test_429_triggers_backoff_and_refuses_calls_until_it_passes():
    clock = FakeClock()
    reader, transport, _ = make_reader({"/v1/markets/x/bbo": RateLimited("429")}, clock=clock)
    with pytest.raises(RateLimited):
        reader.quote("x", True, 0.0695)
    first_calls = len(transport.calls)
    with pytest.raises(RateLimited):
        reader.quote("x", True, 0.0695)
    assert len(transport.calls) == first_calls  # no request made while backing off
    clock.t += 59
    with pytest.raises(RateLimited):
        reader.quote("x", True, 0.0695)
    assert len(transport.calls) == first_calls  # still paused: a minute, not a second
    clock.t += 2
    with pytest.raises(RateLimited):
        reader.quote("x", True, 0.0695)
    assert len(transport.calls) == first_calls + 1  # retried after the minute passed
    assert reader.backoff_remaining() == pytest.approx(120)  # and the pause doubled


def test_cloudflare_ban_page_becomes_a_short_message():
    from scanner.polymarket import short_error

    page = (
        "<!doctype html><html><title>Access denied | Cloudflare</title>"
        "Error 1015 You are being rate limited</html>"
    )
    assert short_error(page) == "Cloudflare rate-limit page (error 1015: temporarily banned)"
    assert short_error("x" * 500).endswith("...") and len(short_error("x" * 500)) == 160
    assert short_error("plain  text\n here") == "plain text here"


# -- quarter and half markets ----------------------------------------------------


def tb_dal():
    return parse_event(load_fixture("pm_nfl_tb_dal.json")["events"][0], League.NFL)


def test_period_markets_are_parsed_from_a_real_event():
    game = tb_dal()
    by_slug = {m.market_slug: m for m in game.period_markets}
    assert len(game.period_markets) == 30
    kinds = {(m.period, m.kind) for m in game.period_markets}
    assert kinds == {
        ("1q", "total"),
        ("2q", "total"),
        ("3q", "total"),
        ("4q", "total"),
        ("1h", "total"),
        ("2h", "total"),
        ("1h", "team_total"),
        ("2h", "team_total"),
        ("1q", "spread"),
        ("2q", "spread"),
        ("3q", "spread"),
        ("4q", "spread"),
        ("1h", "spread"),
        ("2h", "spread"),
    }
    total = by_slug["tsc-nfl-tb-dal-2026-10-08-1h-15pt5"]
    assert (total.period, total.kind, total.line, total.over_is_long) == ("1h", "total", 15.5, True)
    assert total.team_id is None and total.active and not total.closed and total.theta == 0.0695
    team_total = by_slug["tsc-nfl-tb-dal-2026-10-08-tt1h-tb-4pt5"]
    assert (team_total.kind, team_total.line, team_total.team_id) == ("team_total", 4.5, 77)
    dog = by_slug["asc-nfl-tb-dal-2026-10-08-1q-pos-1pt5"]
    assert (dog.kind, dog.team_id, dog.other_team_id, dog.long_line) == ("spread", 77, 56, 1.5)
    favourite = by_slug["asc-nfl-tb-dal-2026-10-08-1h-neg-14pt5"]
    assert (favourite.team_id, favourite.other_team_id, favourite.long_line) == (77, 56, -14.5)
    # Full-game markets are not period markets.
    assert not any("total-" in m.market_slug for m in game.period_markets)
    assert game.moneyline_slug == "aec-nfl-tb-dal-2026-10-08" and len(game.totals) == 2


def test_unclear_period_markets_are_skipped():
    import copy

    raw = copy.deepcopy(load_fixture("pm_nfl_tb_dal.json")["events"][0])
    for market in raw["markets"]:
        slug = market["slug"]
        if slug == "asc-nfl-tb-dal-2026-10-08-1q-pos-1pt5":
            market["marketSides"][1]["description"] = "-2.50"  # sides disagree
        elif slug == "asc-nfl-tb-dal-2026-10-08-1q-pos-2pt5":
            market["marketSides"][1]["teamId"] = 77  # both sides the same team
        elif slug == "tsc-nfl-tb-dal-2026-10-08-1h-15pt5":
            market["marketSides"][0]["description"] = "Yes"  # not Over/Under
        elif slug == "tsc-nfl-tb-dal-2026-10-08-tt1h-tb-4pt5":
            market["marketSides"][1]["teamId"] = 56  # Over and Under name different teams
        elif slug == "asc-nfl-tb-dal-2026-10-08-1h-neg-14pt5":
            market["line"] = 13.5  # market line disagrees with its sides
    game = parse_event(raw, League.NFL)
    slugs = {m.market_slug for m in game.period_markets}
    assert len(game.period_markets) == 25
    assert (
        not {
            "asc-nfl-tb-dal-2026-10-08-1q-pos-1pt5",
            "asc-nfl-tb-dal-2026-10-08-1q-pos-2pt5",
            "tsc-nfl-tb-dal-2026-10-08-1h-15pt5",
            "tsc-nfl-tb-dal-2026-10-08-tt1h-tb-4pt5",
            "asc-nfl-tb-dal-2026-10-08-1h-neg-14pt5",
        }
        & slugs
    )


def test_side_quotes_price_both_sides_from_one_read():
    market = next(m for m in tb_dal().period_markets if m.market_slug.endswith("1h-15pt5"))
    reader, transport, _ = make_reader(
        {f"/v1/markets/{market.market_slug}/bbo": load_fixture("pm_bbo_open.json")}
    )
    quotes = reader.side_quotes(market)
    assert set(quotes) == {"long", "short"}
    assert quotes["long"].side_label == "long" and quotes["short"].side_label == "short"
    assert quotes["long"].market_slug == market.market_slug
    assert quotes["long"].buy_price == pytest.approx(quotes["long"].best_ask)
    assert quotes["short"].buy_price == pytest.approx(1 - quotes["short"].best_bid)
    assert len(transport.calls) == 1


# -- other sports: quotes, spreads, generic totals ----------------------------------


def test_nba_event_carries_quotes_totals_and_spreads():
    game = parse_event(load_fixture("pm_pregame_nba.json")["events"][0], League.NBA)
    assert game.event_slug == "nba-bkn-cha-2026-10-06" and game.moneyline_slug
    quotes = {t.abbreviation: t.quote for t in game.teams}
    assert quotes == {"BKN": pytest.approx(0.35), "CHA": pytest.approx(0.66)}
    total = next(t for t in game.totals if t.line == 209.5)
    assert total.over_quote == pytest.approx(0.58) and total.under_quote == pytest.approx(0.45)
    assert len(game.spreads) == 3
    dog = next(s for s in game.spreads if s.long_line == 7.5)
    assert (dog.long_team_id, dog.short_team_id) == (22, 16)
    assert dog.long_quote == pytest.approx(0.53) and dog.short_quote == pytest.approx(0.48)
    assert dog.market_slug == "asc-nba-bkn-cha-2026-10-06-pos-7pt5" and dog.active


def test_hockey_and_soccer_events_parse_with_the_generic_market_rules():
    nhl = parse_event(load_fixture("pm_pregame_nhl.json")["events"][0], League.NHL)
    assert nhl.moneyline_slug == "aec-nhl-nas-tor-2026-10-06"
    assert [t.line for t in nhl.totals] == [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5]
    assert {s.long_line for s in nhl.spreads} == {-3.5, -2.5, -1.5, 1.5, 2.5, 3.5}
    assert nhl.period_markets == ()  # hockey periods are not football quarters
    epl = parse_event(load_fixture("pm_pregame_epl.json")["events"][0], League.EPL)
    assert epl.moneyline_slug is None  # a three-way market, not a moneyline
    assert all(t.quote is None for t in epl.teams)
    assert [t.line for t in epl.totals] == [0.5, 1.5, 2.5, 3.5]
    assert {s.long_line for s in epl.spreads} == {-2.5, -1.5, 1.5, 2.5}


def test_list_leagues_returns_every_page():
    reader, transport, _ = make_reader()
    leagues = reader.list_leagues()
    assert len(leagues) == 100 and {lg["slug"] for lg in leagues} >= {
        "nfl",
        "cfb",
        "mlb",
        "nba",
        "nhl",
        "epl",
    }
    assert len([c for c in transport.calls if c[0] == "/v2/leagues"]) == 3
