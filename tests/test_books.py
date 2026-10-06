from __future__ import annotations

import pytest

from scanner.books import (
    SPORTS,
    BookLine,
    american_to_int,
    fair,
    implied,
    parse_book_line,
    parse_pregame_scoreboard,
    scoreboard_url,
)
from scanner.config import PREGAME_SPORT_KEYS
from scanner.models import League
from tests.conftest import at, load_fixture


def test_american_odds_arithmetic():
    assert american_to_int("-142") == -142 and american_to_int("+120") == 120
    assert american_to_int("EVEN") == 100 and american_to_int(400) == 400
    assert american_to_int("0") is None and american_to_int("n/a") is None
    assert american_to_int(True) is None and american_to_int(None) is None
    assert implied(-110) == pytest.approx(110 / 210)
    assert implied(120) == pytest.approx(100 / 220)
    home, away = fair(-110, -110)
    assert home == pytest.approx(0.5) and away == pytest.approx(0.5)
    home, away = fair(-142, 120)
    assert home == pytest.approx(0.5635, abs=0.0005) and home + away == pytest.approx(1.0)
    three = fair(-110, 400, 250)
    assert sum(three) == pytest.approx(1.0) and three[0] > three[2] > three[1]


def test_wnba_line_parses_moneyline_total_and_spread():
    raw = load_fixture("espn_pregame_wnba.json")
    events = parse_pregame_scoreboard(raw, League.WNBA, at(0))
    assert len(events) == 2
    event = events[0]
    assert (event.away.abbreviation, event.home.abbreviation) == ("NY", "ATL")
    assert event.status == "pre" and event.kickoff.isoformat() == "2026-10-07T23:30:00+00:00"
    assert event.home.nickname == "Dream" and event.home.name == "Atlanta Dream"
    assert event.league is League.WNBA and event.fetched_at == at(0)
    book = event.book
    odds = raw["events"][0]["competitions"][0]["odds"][0]
    assert book.provider == "DraftKings"
    assert (book.home_ml, book.away_ml, book.draw_ml) == (-142, 120, None)
    assert (book.home_ml_open, book.away_ml_open) == (
        -180,
        american_to_int(odds["moneyline"]["away"]["open"]["odds"]),
    )
    assert book.total == 170.5 and book.home_spread == -2.5
    assert book.over_odds == american_to_int(odds["total"]["over"]["close"]["odds"])
    assert book.under_odds == american_to_int(odds["total"]["under"]["close"]["odds"])
    assert book.home_spread_odds == american_to_int(odds["pointSpread"]["home"]["close"]["odds"])
    assert book.away_spread_odds == american_to_int(odds["pointSpread"]["away"]["close"]["odds"])
    home, away = book.fair_moneyline()
    assert home == pytest.approx(0.5635, abs=0.0005) and away == pytest.approx(0.4365, abs=0.0005)
    assert sum(book.fair_total()) == pytest.approx(1.0) and sum(
        book.fair_spread()
    ) == pytest.approx(1.0)
    assert BookLine.from_dict(book.as_dict()) == book


def test_soccer_line_has_a_draw_price_and_no_two_way_moneyline():
    events = parse_pregame_scoreboard(load_fixture("espn_pregame_epl.json"), League.EPL, at(0))
    event = next(e for e in events if e.home.abbreviation == "ARS")
    book = event.book
    assert book.draw_ml == 400 and book.home_ml is not None and book.away_ml is not None
    assert book.fair_moneyline() is None
    assert book.total == 2.5 and book.fair_total() is not None
    assert book.home_spread == -1.5 and book.fair_spread() is not None
    assert SPORTS["epl"].two_way_moneyline is False


def test_mlb_scoreboard_carries_finals_and_a_near_pickem():
    events = parse_pregame_scoreboard(load_fixture("espn_pregame_mlb.json"), League.MLB, at(0))
    final = next(e for e in events if e.status == "final")
    assert (final.home.abbreviation, final.away.abbreviation) == ("CLE", "CHW")
    assert (final.home_score, final.away_score) == (3, 4) and final.book is None
    game = next(
        e
        for e in events
        if e.away.abbreviation == "LAD"
        and e.kickoff == at(0).replace(year=2026, month=10, day=6, hour=22, minute=0, second=0)
    )
    assert game.status == "pre"
    assert (game.book.home_ml, game.book.away_ml, game.book.total) == (-109, -110, 6.0)
    home, away = game.book.fair_moneyline()
    assert home == pytest.approx(0.4989, abs=0.0005) and away == pytest.approx(0.5011, abs=0.0005)


def test_odds_block_without_anything_usable_is_none():
    assert parse_book_line({"provider": {"name": "X"}}) is None
    assert parse_book_line({"provider": {"name": "X"}, "overUnder": 44.5}).total == 44.5
    disagree = parse_book_line(
        {
            "provider": {"name": "X"},
            "pointSpread": {
                "home": {"close": {"line": "-3.5", "odds": "-110"}},
                "away": {"close": {"line": "+2.5", "odds": "-110"}},
            },
            "overUnder": 44.5,
        }
    )
    assert disagree.home_spread is None and disagree.fair_spread() is None


def test_scoreboard_urls_and_sport_table():
    assert scoreboard_url(SPORTS["cfb"], "20261010") == (
        "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
        "?groups=80&dates=20261010&limit=300"
    )
    assert scoreboard_url(SPORTS["mlb"], "20261006").endswith(
        "baseball/mlb/scoreboard?dates=20261006&limit=300"
    )
    for key in PREGAME_SPORT_KEYS:
        assert SPORTS[key].league is League(key)
