from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from scanner.books import BookLine
from scanner.card import (
    MARKET_MONEYLINE,
    MARKET_SPREAD,
    MARKET_TOTAL,
    Candidate,
    build_if_due,
    candidates_for,
    card_date_for,
    choose,
    closing_for,
    is_late,
    lock_moment,
    payout,
    settle_pick,
    summarise,
    trust_for,
    window_for,
)
from scanner.config import load_settings
from scanner.diary import Diary
from scanner.gamelog import parse_game_summary, parse_week_scoreboard
from scanner.projection import fit, project
from scanner.report import card_report
from tests.conftest import load_fixture
from tests.test_web import replace_slate

SETTINGS = load_settings(GAMELOG_SPORTS="nfl,cfb", CARD_PICKS=5)
BOOK = BookLine("DraftKings", -150, 130, None, 41.5, -105, -115, -2.5, -110, -110)
NFL = {
    "model": "v1",
    "thin": False,
    "raw": {"margin": 6.0, "total": 48.0, "home_win": 0.67},
    "blend": {"margin": 4.0, "total": 45.0, "home_win": 0.6},
    "book": {"margin": 2.5, "total": 41.5, "home_win": 0.58},
    "league": {"sigma": 13.5, "sigma_total": 10.5},
}
HOME = {"team_id": "5", "abbreviation": "CLE", "name": "Browns"}
AWAY = {"team_id": "23", "abbreviation": "PIT", "name": "Steelers"}


def test_the_card_locks_at_ten_eastern_and_covers_the_next_day():
    assert lock_moment(date(2026, 10, 11), SETTINGS) == datetime(2026, 10, 11, 14, 0, tzinfo=UTC)
    # 9:59 ET is still yesterday's card; 10:00 ET is today's.
    assert card_date_for(datetime(2026, 10, 11, 13, 59, tzinfo=UTC), SETTINGS) == date(2026, 10, 10)
    assert card_date_for(datetime(2026, 10, 11, 14, 0, tzinfo=UTC), SETTINGS) == date(2026, 10, 11)
    now = datetime(2026, 10, 11, 14, 5, tzinfo=UTC)
    start, end = window_for(date(2026, 10, 11), now, SETTINGS)
    assert start == now and end == datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
    late = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
    assert window_for(date(2026, 10, 11), late, SETTINGS)[0] == late
    assert not is_late(now, date(2026, 10, 11), SETTINGS) and is_late(
        late, date(2026, 10, 11), SETTINGS
    )
    # Winter: the clock moves with the zone, so the lock is 15:00 UTC in December.
    assert lock_moment(date(2026, 12, 11), SETTINGS) == datetime(2026, 12, 11, 15, 0, tzinfo=UTC)
    central = load_settings(CARD_LOCK_TIME="9:30", CARD_TZ="America/Chicago")
    assert central.CARD_LOCK_TIME == "09:30"
    assert lock_moment(date(2026, 10, 11), central) == datetime(2026, 10, 11, 14, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        load_settings(CARD_LOCK_TIME="ten")
    with pytest.raises(ValueError):
        load_settings(CARD_TZ="Mars/Olympus")


def test_trust_waits_for_twenty_closed_lines_then_follows_the_closing_line_record():
    assert trust_for(None) == {"trust": 1.0, "closed": 0, "positive_share": None, "weighted": False}
    few = {"clv": {"closed": 19, "positive_share": 0.9}}
    assert trust_for(few)["trust"] == 1.0 and not trust_for(few)["weighted"]
    enough = trust_for({"clv": {"closed": 20, "positive_share": 0.6}})
    assert enough == {"trust": 1.1, "closed": 20, "positive_share": 0.6, "weighted": True}
    assert trust_for({"clv": {"closed": 40, "positive_share": 0.3}})["trust"] == pytest.approx(0.8)


def test_candidates_take_the_stronger_side_of_each_market_at_the_books_price():
    out = candidates_for("nfl", {**NFL, "game_id": "g1"}, BOOK)
    assert [c.market for c in out] == [MARKET_SPREAD, MARKET_TOTAL]
    spread, total = out
    assert spread == Candidate(
        "nfl", "g1", MARKET_SPREAD, "home", -2.5, -110, 6.0, 2.5, 3.5, 3.5 / 13.5
    )
    assert total.side == "over" and total.line == 41.5 and total.odds == -105
    assert total.z == pytest.approx(6.5 / 10.5)
    # The model below the book: the away side and the under, at their own prices.
    low = {**NFL, "raw": {"margin": -1.0, "total": 38.0, "home_win": 0.4}}
    away, under = candidates_for("nfl", low, BOOK)
    assert away.side == "away" and away.gap == -3.5 and under.side == "under" and under.odds == -115
    # Without the book's prices the usual -110 applies; without its line there is no candidate.
    assert [c.odds for c in candidates_for("nfl", NFL, None)] == [None, None]
    assert payout(None) == pytest.approx(100 / 110) and payout(-150) == pytest.approx(2 / 3)
    assert payout(130) == 1.3
    no_total = {**NFL, "book": {"margin": 2.5, "total": None, "home_win": 0.58}}
    assert [c.market for c in candidates_for("nfl", no_total, BOOK)] == [MARKET_SPREAD]
    assert candidates_for("nfl", {**NFL, "thin": True}, BOOK) == []
    assert candidates_for("nfl", {**NFL, "book": None}, BOOK) == []
    same = {**NFL, "raw": {"margin": 2.5, "total": 41.5, "home_win": 0.58}}
    assert candidates_for("nfl", same, BOOK) == []


def test_hockey_takes_the_moneyline_since_the_puck_line_never_moves():
    nhl = {
        "thin": False,
        "raw": {"margin": 0.6, "total": 6.4, "home_win": 0.6},
        "book": {"margin": 0.1, "total": 6.0, "home_win": 0.52},
        "league": {"sigma": 2.3, "sigma_total": 2.4},
    }
    book = BookLine("DraftKings", -120, 100, None, 6.0, -110, -110, -1.5, 150, -180)
    side, total = candidates_for("nhl", nhl, book)
    assert side.market == MARKET_MONEYLINE and side.side == "home" and side.line is None
    assert side.odds == -120 and side.z == pytest.approx(0.5 / 2.3)
    assert total.side == "over" and total.z == pytest.approx(0.4 / 2.4)
    # The away side at its own price; with no moneyline there is no side to take.
    dog = {**nhl, "raw": {"margin": -0.5, "total": 6.4, "home_win": 0.42}}
    assert candidates_for("nhl", dog, book)[0].odds == 100
    assert [c.market for c in candidates_for("nhl", nhl, None)] == [MARKET_TOTAL]


def game(game_id, projection, hours=3, book=BOOK, home=HOME, away=AWAY, sport="nfl"):
    kickoff = datetime(2026, 10, 11, 14, 0, tzinfo=UTC) + timedelta(hours=hours)
    return {
        "sport": sport,
        "game_id": game_id,
        "kickoff": kickoff,
        "home": home,
        "away": away,
        "book": book,
        "projection": {**projection, "game_id": game_id},
        "updated_at": "2026-10-11T13:50:00+00:00",
    }


def test_choose_ranks_one_side_per_game_by_conviction_and_keeps_the_top_few():
    mild = {**NFL, "raw": {"margin": 3.5, "total": 43.0, "home_win": 0.6}}  # 1 point, 1.5 points
    games = [
        game("a", NFL),  # spread 3.5/13.5 = 0.26, total 6.5/10.5 = 0.62: the total
        game("b", mild, hours=9),  # total 1.5/10.5 = 0.14
        game("c", {**NFL, "thin": True}),  # never
        game("d", {**NFL, "raw": {"margin": -6.0, "total": 41.5, "home_win": 0.3}}, hours=1),
    ]
    trust = {"nfl": {"trust": 1.0, "closed": 3, "positive_share": None, "weighted": False}}
    picks = choose(games, trust, picks=5, draws=2_000)
    assert [(p["rank"], p["game_id"], p["market"], p["side_label"]) for p in picks] == [
        (1, "d", MARKET_SPREAD, "PIT +2.5"),  # 8.5 points off the book
        (2, "a", MARKET_TOTAL, "Over 41.5"),
        (3, "b", MARKET_TOTAL, "Over 41.5"),
    ]
    top = picks[0]
    assert top["conviction"] == pytest.approx(8.5 / 13.5) and top["trust"] == 1.0
    assert top["odds"] == -110 and top["price"] == pytest.approx(100 / 110)
    assert top["line"] == -2.5 and top["model_value"] == -6.0 and top["book_value"] == 2.5
    assert top["home"] == "CLE" and top["away"] == "PIT"
    detail = top["detail"]
    assert detail["home_team"] == HOME and detail["sim"]["draws"] == 2_000
    assert 0.6 < detail["chance"] < 0.8  # PIT +2.5 when the model has PIT by 6
    assert detail["projection"]["raw"]["margin"] == -6.0 and detail["trust"] == trust["nfl"]
    assert picks[1]["side_label"] == "Over 41.5" and picks[1]["odds"] == -105
    # A sport's trust scales its conviction; a tie in conviction goes to the earlier game.
    weighted = {"nfl": {"trust": 1.5, "closed": 30, "positive_share": 1.0, "weighted": True}}
    assert choose(games, weighted, 1, draws=100)[0]["conviction"] == pytest.approx(8.5 / 13.5 * 1.5)
    assert len(choose(games, trust, 2, draws=100)) == 2
    assert choose([], trust, 5) == []


def test_settling_and_the_closing_line():
    spread = {"market": MARKET_SPREAD, "side": "home", "line": -2.5, "price": 100 / 110}
    assert settle_pick(spread, 27, 24) == ("win", pytest.approx(100 / 110))
    assert settle_pick(spread, 24, 27) == ("loss", -1.0)
    push = {**spread, "line": -3.0}
    assert settle_pick(push, 27, 24) == ("push", 0.0)
    over = {"market": MARKET_TOTAL, "side": "over", "line": 41.5, "price": 100 / 105}
    assert settle_pick(over, 27, 24) == ("win", pytest.approx(100 / 105))
    assert settle_pick({**over, "side": "under"}, 27, 24) == ("loss", -1.0)
    ml = {"market": MARKET_MONEYLINE, "side": "away", "line": None, "price": 1.3}
    assert settle_pick(ml, 2, 3) == ("win", 1.3)
    assert settle_pick(ml, 3, 3) == ("push", 0.0)
    assert settle_pick({**over, "line": None}, 27, 24) == ("not_graded", None)

    close = replace(BOOK, home_spread=-4.5, total=44.0)
    home_side = {"sport": "nfl", "market": MARKET_SPREAD, "side": "home", "book_value": 2.5}
    assert closing_for(home_side, close) == (4.5, 2.0)  # the line came the home side's way
    assert closing_for({**home_side, "side": "away"}, close) == (4.5, -2.0)
    assert closing_for({**home_side, "market": MARKET_TOTAL, "side": "under",
                        "book_value": 41.5}, close) == (44.0, -2.5)  # fmt: skip
    assert closing_for(home_side, None) == (None, None)
    hockey = {"sport": "nhl", "market": MARKET_MONEYLINE, "side": "home", "book_value": 0.1}
    nhl_close = BookLine("DK", -200, 170, None, 6.0, -110, -110, -1.5, 150, -180)
    closing, moved = closing_for(hockey, nhl_close)
    assert closing > 0.1 and moved == pytest.approx(closing - 0.1)


def test_summarise_counts_by_rank_sport_and_market():
    rows = [
        {
            "rank": 1,
            "sport": "nfl",
            "market": MARKET_SPREAD,
            "outcome": "win",
            "units": 0.9,
            "clv": 1,
        },
        {
            "rank": 2,
            "sport": "nfl",
            "market": MARKET_TOTAL,
            "outcome": "loss",
            "units": -1,
            "clv": -1,
        },
        {
            "rank": 1,
            "sport": "nhl",
            "market": MARKET_MONEYLINE,
            "outcome": "push",
            "units": 0,
            "clv": 0.5,
        },
        {
            "rank": 3,
            "sport": "nhl",
            "market": MARKET_TOTAL,
            "outcome": None,
            "units": None,
            "clv": None,
        },
        {"rank": 4, "sport": "cfb", "market": MARKET_SPREAD, "outcome": "not_graded"},
    ]
    s = summarise(rows, picks=5)
    assert (s["picks"], s["graded"], s["open"], s["not_graded"]) == (5, 3, 1, 1)
    assert (s["wins"], s["losses"], s["pushes"]) == (1, 1, 1) and s["win_rate"] == 0.5
    assert s["units"] == pytest.approx(-0.1) and s["roi"] == pytest.approx(-0.1 / 3)
    assert s["clv"] == pytest.approx(0.5 / 3) and s["clv_positive_share"] == pytest.approx(2 / 3)
    assert list(s["by_rank"]) == ["1", "2", "3", "4", "5"]
    assert s["by_rank"]["1"]["wins"] == 1 and s["by_rank"]["1"]["pushes"] == 1
    assert s["by_rank"]["5"]["picks"] == 0
    assert s["by_sport"]["nhl"]["graded"] == 1 and s["by_market"]["total"]["losses"] == 1
    assert list(s["by_market"]) == ["spread", "moneyline", "total"]


def seeded_diary():
    """A log with one finished game, two upcoming games with book lines, both projected."""
    diary = Diary(":memory:")
    t0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    record = parse_game_summary(load_fixture("espn_nfl_summary_final.json"), "nfl")
    diary.store_football_game(record, t0)
    _, _, games = parse_week_scoreboard(load_fixture("espn_nfl_week4.json"), "nfl")
    kickoff = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)  # 1 PM ET on the Sunday
    first = replace_slate(games[0], completed=False, date=kickoff, book=BOOK)
    diary.store_football_upcoming(first, t0)
    ratings = fit(diary.football_games("nfl"), "nfl")
    projection = {**project(ratings, "5", "23", False, BOOK), "thin": False}
    diary.upsert_projection("nfl", first.game_id, kickoff, "CLE", "PIT", projection, t0)
    later = kickoff + timedelta(hours=3)
    second_book = replace(BOOK, total=48.5, home_spread=3.5)
    second = replace_slate(games[1], completed=False, date=later, book=second_book)
    diary.store_football_upcoming(second, t0)
    swapped = {**project(ratings, "23", "5", False, second_book), "thin": False}
    diary.upsert_projection("nfl", second.game_id, later, "PIT", "CLE", swapped, t0)
    return diary, record, first, second


def test_the_card_locks_once_when_due_and_is_graded_from_the_log():
    diary, record, first, second = seeded_diary()
    settings = load_settings(GAMELOG_SPORTS="nfl,cfb", CARD_PICKS=5)
    before = datetime(2026, 10, 11, 13, 50, tzinfo=UTC)  # 9:50 ET: yesterday's card, no games
    assert build_if_due(diary, settings, before) is None
    assert diary.latest_card_date() is None
    now = datetime(2026, 10, 11, 14, 5, tzinfo=UTC)
    built = build_if_due(diary, settings, now)
    assert built == {
        "card_date": "2026-10-11",
        "picks": 2,
        "late": False,
        "locked_at": now.isoformat(),
    }
    assert build_if_due(diary, settings, now + timedelta(minutes=15)) is None  # locked once
    card = diary.card("2026-10-11")
    assert card["locked_at"] == now.isoformat() and card["picks"] == 2 and not card["late"]
    top, other = card["rows"]
    assert top["rank"] == 1 and top["game_id"] == first.game_id and top["market"] == "total"
    assert top["side_label"] == "Over 41.5" and top["odds"] == -105
    assert top["outcome"] is None and top["detail"]["sim"]["draws"] == 100_000
    assert other["game_id"] == second.game_id and other["side_label"] == "WSH +3.5"
    assert diary.card_pick("2026-10-11", 2)["rank"] == 2
    assert diary.card_pick("2026-10-11", 3) is None and diary.card("2026-10-10") is None
    assert diary.card_exists("2026-10-11") and diary.latest_card_date() == "2026-10-11"

    # Grading waits for the grace period and the game's record in the log, then settles
    # at the final. The first game's record is the fixture's own, already in the log.
    kickoff = datetime.fromisoformat(top["kickoff"])
    assert diary.grade_card_picks(kickoff + timedelta(hours=1)) == 0
    assert diary.grade_card_picks(kickoff + timedelta(hours=6)) == 1
    graded = diary.card_pick("2026-10-11", 1)
    assert graded["outcome"] == "win" and graded["units"] == pytest.approx(100 / 105)
    assert (graded["final_home"], graded["final_away"]) == (27, 24)
    assert graded["closing_value"] == 38.5 and graded["clv"] == -3.0  # the total closed lower
    assert diary.card_pick("2026-10-11", 2)["outcome"] is None  # not in the log yet
    # The other game never lands: given up after ten days.
    assert diary.grade_card_picks(kickoff + timedelta(days=11)) == 0
    assert diary.card_pick("2026-10-11", 2)["outcome"] == "not_graded"

    history = diary.card_history()
    assert len(history) == 1 and history[0]["card_date"] == "2026-10-11"
    assert (history[0]["wins"], history[0]["graded"], history[0]["open"]) == (1, 1, 0)
    summary = diary.card_summary(picks=5)
    assert summary["days"] == 1 and summary["graded"] == 1 and summary["not_graded"] == 1
    assert (
        summary["by_rank"]["1"]["wins"] == 1 and summary["recent"][0]["side_label"] == "Over 41.5"
    )
    assert diary.card_summary(since=kickoff + timedelta(days=1))["picks"] == 0
    assert diary.scorecard()["card"]["wins"] == 1

    text = card_report(diary, settings)
    assert "Daily card 2026-10-11, locked Sun Oct 11 10:05 AM" in text
    assert "#1 NFL PIT at CLE Sun 1:00 PM: Over 41.5 (-105)" in text and "-> win +0.95u" in text
    assert "Record over 1 days: 1W 0L 0P, +0.95 units" in text and "rank 1: 1W" in text
    assert card_report(Diary(":memory:"), settings).startswith("No daily card yet")


def test_a_card_is_marked_late_and_nothing_is_recorded_without_eligible_games():
    diary, _, _, _ = seeded_diary()
    settings = load_settings(GAMELOG_SPORTS="nfl,cfb")
    late = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)  # 11 AM ET, an hour after the lock
    built = build_if_due(diary, settings, late)
    assert built["late"] is True and diary.card("2026-10-11")["late"] is True
    empty = Diary(":memory:")
    assert build_if_due(empty, settings, late) is None and empty.latest_card_date() is None
    off = load_settings(GAMELOG_SPORTS="nfl,cfb", CARD_ENABLED=False)
    assert build_if_due(seeded_diary()[0], off, late) is None
    no_model = load_settings(GAMELOG_SPORTS="nfl,cfb", PROJECTION_ENABLED=False)
    assert build_if_due(seeded_diary()[0], no_model, late) is None
    # Games that have already started are left out of a late card.
    too_late = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)  # 2 PM ET: the 1 PM game is on
    diary, _, _, second = seeded_diary()
    built = build_if_due(diary, settings, too_late)
    assert built["picks"] == 1 and diary.card("2026-10-11")["rows"][0]["game_id"] == second.game_id
