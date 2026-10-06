from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from scanner.books import SPORTS, BookFeedError, parse_pregame_scoreboard
from scanner.config import load_settings
from scanner.diary import OUTCOME_LOSS, OUTCOME_NOT_GRADED, OUTCOME_PUSH, OUTCOME_WIN, Diary
from scanner.matching import match_games
from scanner.models import League
from scanner.polymarket import PolymarketError, parse_event
from scanner.pregame import PregameScanner, closing_for, find_gaps, pregame_outcome
from scanner.web import RuntimeStatus
from tests.conftest import at, load_fixture

START = datetime.fromisoformat("2026-10-06T22:00:00+00:00")  # LAD at ATL, game 3
T_SCAN = START - timedelta(hours=8)


class FakeReader:
    def __init__(self, games_by_slug, leagues=None):
        self.games_by_slug = games_by_slug
        self.leagues = leagues or [{"slug": "mlb", "name": "MLB", "abbreviation": "MLB"}]
        self.calls: list[str] = []
        self.fail = False

    def list_leagues(self):
        if self.fail:
            raise PolymarketError("down")
        return self.leagues

    def list_games(self, league, slug):
        self.calls.append(f"games:{slug}")
        if self.fail:
            raise PolymarketError("down")
        return self.games_by_slug.get(slug, [])


class FakeFeed:
    def __init__(self):
        self.events = {}  # (sport key, YYYYMMDD) -> events, or an exception
        self.calls: list[tuple[str, str]] = []

    def fetch(self, sport, date):
        self.calls.append((sport.key, date))
        value = self.events.get((sport.key, date), [])
        if isinstance(value, Exception):
            raise value
        return value


def mlb_game():
    return parse_event(load_fixture("pm_pregame_mlb.json")["events"][0], League.MLB)


def lad_atl_event(**book_changes):
    events = parse_pregame_scoreboard(load_fixture("espn_pregame_mlb.json"), League.MLB, T_SCAN)
    event = next(e for e in events if e.away.abbreviation == "LAD" and e.kickoff == START)
    if book_changes:
        event = replace(event, book=replace(event.book, **book_changes))
    return event


# A book that disagrees with Polymarket on three markets: ATL to win (-200/+170),
# the Over 6.5 (-150/+125) and ATL -1.5 (+120/-140).
WIDE = dict(
    home_ml=-200,
    away_ml=170,
    total=6.5,
    over_odds=-150,
    under_odds=125,
    home_spread=-1.5,
    home_spread_odds=120,
    away_spread_odds=-140,
)


def build(events=None, **overrides):
    settings = load_settings(**{"PREGAME_SPORTS": "mlb", "DATABASE_PATH": ":memory:", **overrides})
    reader = FakeReader({"mlb": [mlb_game()]})
    feed = FakeFeed()
    feed.events[("mlb", "20261006")] = events if events is not None else [lad_atl_event(**WIDE)]
    diary = Diary(":memory:")
    status = RuntimeStatus()
    scanner = PregameScanner(settings, diary, status, reader=reader, feed=feed, now=lambda: T_SCAN)
    return scanner, reader, feed, diary, status


# -- pure pieces --------------------------------------------------------------------


def test_matching_pairs_the_polymarket_game_with_espn_across_sports():
    game = mlb_game()
    result = match_games([game], [lad_atl_event()], nicknames=True)
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.home_team.abbreviation == "ATL" and match.away_team.abbreviation == "LAD"
    assert match.home_team.quote == pytest.approx(0.5) and match.away_team.quote == pytest.approx(
        0.505
    )


def test_find_gaps_names_every_side_below_the_books_number():
    game = mlb_game()
    match = match_games([game], [lad_atl_event(**WIDE)], nicknames=True).matches[0]
    gaps = find_gaps(match, SPORTS["mlb"], 0.03, T_SCAN)
    by_pick = {g.pick: g for g in gaps}
    assert set(by_pick) == {"ATL", "OVER 6.5", "ATL -1.5"}
    ml = by_pick["ATL"]
    assert ml.market == "moneyline" and ml.pick_side == "home" and ml.side_label == "short"
    assert ml.buy_price == pytest.approx(0.5) and ml.book_odds == -200
    assert ml.book_fair == pytest.approx(0.6429, abs=0.0005)
    assert ml.edge == pytest.approx(0.6429 - 0.5 - 0.0695 * 0.25, abs=0.0006)
    assert (
        ml.home == "ATL" and ml.away == "LAD" and ml.start == START and ml.provider == "DraftKings"
    )
    over = by_pick["OVER 6.5"]
    assert over.market == "total" and over.line == 6.5 and over.pick_side == "over"
    assert over.buy_price == pytest.approx(0.475) and over.book_odds == -150
    spread = by_pick["ATL -1.5"]
    assert spread.market == "spread" and spread.line == -1.5 and spread.pick_side == "home"
    assert spread.side_label == "short" and spread.buy_price == pytest.approx(0.3)
    assert spread.book_odds == 120


def test_find_gaps_respects_the_threshold_lines_and_draws():
    game = mlb_game()
    match = match_games([game], [lad_atl_event()], nicknames=True).matches[0]
    assert find_gaps(match, SPORTS["mlb"], 0.03, T_SCAN) == []  # the real line: no gap
    # A total at a line Polymarket does not list is not compared.
    moved = match_games([game], [lad_atl_event(**{**WIDE, "total": 6.0})], nicknames=True).matches[
        0
    ]
    assert {g.pick for g in find_gaps(moved, SPORTS["mlb"], 0.03, T_SCAN)} == {"ATL", "ATL -1.5"}
    # A sport where a draw is possible never compares moneylines.
    match = match_games([game], [lad_atl_event(**WIDE)], nicknames=True).matches[0]
    picks = {
        g.pick
        for g in find_gaps(match, replace(SPORTS["mlb"], two_way_moneyline=False), 0.03, T_SCAN)
    }
    assert picks == {"OVER 6.5", "ATL -1.5"}
    # A higher threshold keeps only the widest gaps.
    assert {g.pick for g in find_gaps(match, SPORTS["mlb"], 0.10, T_SCAN)} == {"ATL", "ATL -1.5"}
    # No book line, no gaps.
    assert (
        find_gaps(replace(match, espn=replace(match.espn, book=None)), SPORTS["mlb"], 0.03, T_SCAN)
        == []
    )


def test_outcomes_for_every_market():
    ml = {"market": "moneyline", "pick_side": "home", "line": None}
    assert pregame_outcome(ml, 5, 3) == (OUTCOME_WIN, 1.0)
    assert pregame_outcome(ml, 3, 5) == (OUTCOME_LOSS, 0.0)
    assert pregame_outcome(ml, 3, 3) == (OUTCOME_PUSH, 0.5)
    over = {"market": "total", "pick_side": "over", "line": 6.5}
    assert pregame_outcome(over, 4, 3) == (OUTCOME_WIN, 1.0)
    assert pregame_outcome({**over, "pick_side": "under"}, 4, 3) == (OUTCOME_LOSS, 0.0)
    assert pregame_outcome({**over, "line": 7.0}, 4, 3) == (OUTCOME_PUSH, 0.5)
    assert pregame_outcome({**over, "line": None}, 4, 3) == (OUTCOME_NOT_GRADED, None)
    fav = {"market": "spread", "pick_side": "home", "line": -1.5}
    assert pregame_outcome(fav, 5, 3) == (OUTCOME_WIN, 1.0)
    assert pregame_outcome(fav, 4, 3) == (OUTCOME_LOSS, 0.0)
    dog = {"market": "spread", "pick_side": "away", "line": 1.5}
    assert pregame_outcome(dog, 4, 3) == (OUTCOME_WIN, 1.0)
    assert pregame_outcome({**dog, "line": 1.0}, 4, 3) == (OUTCOME_PUSH, 0.5)
    assert pregame_outcome({"market": "parlay", "pick_side": "x", "line": None}, 1, 0) == (
        OUTCOME_NOT_GRADED,
        None,
    )


def test_closing_line_only_counts_at_the_same_number():
    book = replace(lad_atl_event(**WIDE).book, home_ml=-150, away_ml=130)
    fair_home, odds = closing_for({"market": "moneyline", "pick_side": "home", "line": None}, book)
    assert fair_home == pytest.approx(0.5798, abs=0.0005) and odds == -150
    assert closing_for({"market": "total", "pick_side": "over", "line": 6.5}, book)[1] == -150
    assert closing_for({"market": "total", "pick_side": "over", "line": 7.5}, book) == (None, None)
    assert closing_for({"market": "spread", "pick_side": "home", "line": -1.5}, book)[1] == 120
    assert closing_for({"market": "spread", "pick_side": "away", "line": 1.5}, book)[1] == -140
    assert closing_for({"market": "spread", "pick_side": "away", "line": 2.5}, book) == (None, None)
    assert closing_for({"market": "moneyline", "pick_side": "home", "line": None}, None) == (
        None,
        None,
    )
    soccer = replace(book, draw_ml=300)
    assert closing_for({"market": "moneyline", "pick_side": "home", "line": None}, soccer) == (
        None,
        None,
    )


# -- the scan -----------------------------------------------------------------------


def test_scan_records_gaps_lines_and_status_and_never_duplicates():
    scanner, reader, feed, diary, status = build()
    summary = scanner.scan_once(T_SCAN)
    assert summary.sports == ["mlb"] and summary.matched == 1 and summary.with_lines == 1
    assert summary.gaps_new == 3 and summary.gaps_updated == 0 and summary.errors == []
    assert feed.calls == [("mlb", "20261005"), ("mlb", "20261006"), ("mlb", "20261007")]
    assert reader.calls == ["games:mlb"]
    rows = diary.pregame_gaps()
    assert {r["pick"] for r in rows} == {"ATL", "OVER 6.5", "ATL -1.5"}
    ml = next(r for r in rows if r["pick"] == "ATL")
    assert ml["buy_price"] == pytest.approx(0.5) and ml["seen_count"] == 1 and ml["outcome"] is None
    assert ml["start_time"] == START.isoformat() and ml["home"] == "ATL" and ml["away"] == "LAD"
    assert diary.last_pregame_line("mlb", ml["feed_id"])["book"]["home_ml"] == -200
    assert status.pregame["matched"] == 1 and status.pregame["open_gaps"] == 3
    assert status.pregame["last_scan"] == T_SCAN.isoformat() and status.pregame["error"] is None

    # The same reading again: nothing new, the gaps are only refreshed.
    later = T_SCAN + timedelta(minutes=15)
    summary = scanner.scan_once(later)
    assert summary.gaps_new == 0 and summary.gaps_updated == 3
    ml = next(r for r in diary.pregame_gaps() if r["pick"] == "ATL")
    assert ml["seen_count"] == 2 and ml["last_seen_at"] == later.isoformat()
    assert len([r for r in _lines(diary)]) == 1  # unchanged lines are not stored twice

    # The book moves: the line is stored again; the gap keeps its first price.
    feed.events[("mlb", "20261006")] = [lad_atl_event(**{**WIDE, "home_ml": -150, "away_ml": 130})]
    summary = scanner.scan_once(later + timedelta(minutes=15))
    assert len(_lines(diary)) == 2
    ml = next(r for r in diary.pregame_gaps() if r["pick"] == "ATL")
    assert ml["buy_price"] == pytest.approx(0.5) and ml["latest_edge"] < ml["edge"]
    assert ml["max_edge"] == pytest.approx(ml["edge"])


def _lines(diary):
    with diary._lock:
        return [dict(r) for r in diary._conn.execute("SELECT * FROM pregame_lines").fetchall()]


def test_gaps_get_the_closing_line_once_the_game_starts_and_a_grade_at_the_final():
    scanner, reader, feed, diary, status = build()
    scanner.scan_once(T_SCAN)
    feed.events[("mlb", "20261006")] = [lad_atl_event(**{**WIDE, "home_ml": -150, "away_ml": 130})]
    scanner.scan_once(T_SCAN + timedelta(hours=1))
    # First pitch: the game is no longer upcoming, and every gap gets the last line before it.
    live = replace(lad_atl_event(**{**WIDE, "home_ml": -150, "away_ml": 130}), status="live")
    feed.events[("mlb", "20261006")] = [live]
    summary = scanner.scan_once(START + timedelta(minutes=1))
    assert summary.matched == 0 and summary.closed == 3 and summary.graded == 0
    rows = {r["pick"]: r for r in diary.pregame_gaps()}
    ml = rows["ATL"]
    assert ml["closing_fair"] == pytest.approx(0.5798, abs=0.0005) and ml["closing_odds"] == -150
    assert ml["clv"] == pytest.approx(0.5798 - 0.5 - 0.0695 * 0.25, abs=0.0006)
    assert 0 < ml["clv"] < ml["edge"]  # the book came our way, but less than the opening gap
    assert rows["OVER 6.5"]["closing_odds"] == -150 and rows["ATL -1.5"]["closing_odds"] == 120
    assert diary.open_pregame_gap_count() == 3

    # Final: ATL 5, LAD 3. ATL won, the Over hit, ATL covered -1.5.
    final = replace(live, status="final", home_score=5, away_score=3)
    feed.events[("mlb", "20261006")] = [final]
    summary = scanner.scan_once(START + timedelta(hours=4))
    assert summary.graded == 3 and summary.closed == 0
    rows = {r["pick"]: r for r in diary.pregame_gaps()}
    assert all(r["outcome"] == OUTCOME_WIN for r in rows.values())
    assert rows["ATL"]["result_per_contract"] == pytest.approx(1 - 0.5 - 0.0695 * 0.25)
    assert rows["ATL"]["final_home"] == 5 and rows["ATL"]["final_away"] == 3
    assert diary.open_pregame_gap_count() == 0
    card = diary.pregame_summary()
    assert card["gaps"] == 3 and card["graded"] == 3 and card["wins"] == 3
    assert card["by_sport"] == {"mlb": 3} and card["by_market"] == {
        "moneyline": 1,
        "total": 1,
        "spread": 1,
    }
    assert card["closed"] == 3 and card["avg_clv"] > 0 and card["positive_clv_share"] == 1.0
    assert card["actual_win_rate"] == 1.0 and card["profit_per_100"] > 0
    assert len(card["recent"]) == 3 and card["recent"][0]["outcome"] == OUTCOME_WIN
    assert diary.scorecard()["pregame"]["gaps"] == 3


def test_a_lost_pick_and_a_postponed_game():
    scanner, reader, feed, diary, _ = build()
    scanner.scan_once(T_SCAN)
    final = replace(
        lad_atl_event(**WIDE), status="final", home_score=2, away_score=3
    )  # LAD won 3-2
    feed.events[("mlb", "20261006")] = [final]
    scanner.scan_once(START + timedelta(hours=4))
    rows = {r["pick"]: r for r in diary.pregame_gaps()}
    assert rows["ATL"]["outcome"] == OUTCOME_LOSS and rows["ATL -1.5"]["outcome"] == OUTCOME_LOSS
    assert rows["OVER 6.5"]["outcome"] == OUTCOME_LOSS  # 5 runs
    assert rows["ATL"]["result_per_contract"] == pytest.approx(-0.5 - 0.0695 * 0.25)

    scanner, reader, feed, diary, _ = build()
    scanner.scan_once(T_SCAN)
    feed.events[("mlb", "20261006")] = [replace(lad_atl_event(**WIDE), status="postponed")]
    scanner.scan_once(START + timedelta(hours=4))
    assert {r["outcome"] for r in diary.pregame_gaps()} == {OUTCOME_NOT_GRADED}
    assert diary.pregame_summary()["not_graded"] == 3


def test_old_games_are_fetched_by_their_own_day_to_be_graded():
    scanner, reader, feed, diary, _ = build()
    scanner.scan_once(T_SCAN)
    final = replace(lad_atl_event(**WIDE), status="final", home_score=5, away_score=3)
    feed.events[("mlb", "20261006")] = [final]
    scanner.scan_once(START + timedelta(days=5))  # the three-day window no longer has the game
    assert ("mlb", "20261006") in feed.calls[-1:] or ("mlb", "20261006") in feed.calls
    assert {r["outcome"] for r in diary.pregame_gaps()} == {OUTCOME_WIN}


def test_horizon_unmatched_and_feed_errors():
    far = replace(lad_atl_event(**WIDE), kickoff=START + timedelta(days=3))
    scanner, reader, feed, diary, status = build(events=[far])
    summary = scanner.scan_once(T_SCAN)
    assert summary.matched == 0 and summary.unmatched == 1 and diary.pregame_gaps() == []

    scanner, reader, feed, diary, status = build()
    feed.events[("mlb", "20261006")] = BookFeedError("mlb 20261006: timeout")
    summary = scanner.scan_once(T_SCAN)
    assert summary.sports == [] and summary.errors == ["mlb 20261006: timeout"]
    assert status.pregame["error"] == "mlb 20261006: timeout"

    scanner, reader, feed, diary, status = build()
    reader.fail = True
    summary = scanner.scan_once(T_SCAN)
    assert summary.errors and "down" in summary.errors[0] and diary.pregame_gaps() == []


def test_sports_without_a_polymarket_league_are_skipped_and_dates_follow_eastern_time():
    scanner, reader, feed, diary, _ = build(PREGAME_SPORTS="mlb,nhl")
    assert [s.key for s in scanner.sports] == ["mlb", "nhl"]
    assert scanner.discover() == {"mlb": "mlb"}
    summary = scanner.scan_once(T_SCAN)
    assert summary.sports == ["mlb"] and all(call[0] == "mlb" for call in feed.calls)
    late_evening = datetime.fromisoformat("2026-10-07T03:30:00+00:00")  # 11:30 PM Eastern, Oct 6
    assert scanner.dates(late_evening) == ["20261005", "20261006", "20261007"]
    assert scanner.dates(at(0)) == ["20261010", "20261011", "20261012"]
