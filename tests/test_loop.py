from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta

import pytest

from scanner.config import load_settings
from scanner.diary import Diary
from scanner.loop import Scanner
from scanner.models import GameStatus, League
from scanner.notify import Notifier
from scanner.polymarket import PolymarketError, parse_book, parse_event, parse_quote
from scanner.scores import ScoreFeedError
from scanner.scores import parse_event as espn_parse
from scanner.web import RuntimeStatus
from scanner.winprob import WinProbModel
from tests.conftest import T0, at, load_fixture

REPLAY = load_fixture("replay_phi_jax.json")
SLUG = "aec-nfl-phi-jax-2026-10-11"
KICKOFF = T0 - timedelta(hours=4)  # both feeds agree on it; the game is in its window at T0


class Clock:
    def __init__(self, start=0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self):
        return at(self.t)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeReader:
    def __init__(self, clock: Clock, games, bbo=None, books=None):
        self.clock = clock
        self.games = games  # {League: [PolymarketGame]}
        self.bbo = bbo or {}
        self.books = books or {}
        self.calls: list[str] = []
        self.fail = False

    def discover_leagues(self):
        if self.fail:
            raise PolymarketError("down")
        return {League.NFL: "nfl", League.CFB: "cfb"}

    def list_games(self, league, slug):
        self.calls.append(f"games:{league.value}")
        if self.fail:
            raise PolymarketError("down")
        return self.games.get(league, [])

    def quotes_for_game(self, game):
        self.calls.append(f"bbo:{game.moneyline_slug}")
        if self.fail:
            raise PolymarketError("down")
        raw = self.bbo[game.moneyline_slug]
        return {
            t.abbreviation: parse_quote(
                raw, game.moneyline_slug, t.is_long, game.moneyline_theta, True, self.clock.now()
            )
            for t in game.teams
        }

    def quote(self, slug, is_long, theta, tradable=True):
        self.calls.append(f"quote:{slug}")
        if self.fail:
            raise PolymarketError("down")
        return parse_quote(
            self.bbo[slug], slug, is_long, theta or 0.0695, tradable, self.clock.now()
        )

    def over_quote(self, total):
        return self.quote(total.market_slug, total.over_is_long, total.theta, total.over_tradable)

    def book(self, slug):
        self.calls.append(f"book:{slug}")
        if self.fail:
            raise PolymarketError("down")
        return parse_book(self.books[slug], slug, self.clock.now())


class FakeFeed:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.states = {}  # League -> list of raw ESPN events, or an exception
        self.calls = 0

    def fetch(self, league):
        self.calls += 1
        raw = self.states.get(league, [])
        if isinstance(raw, Exception):
            raise raw
        return [espn_parse(e, league, self.clock.now()) for e in raw]


class FakeSender:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text):
        self.messages.append(text)
        return True


@pytest.fixture(scope="module")
def model():
    return WinProbModel.load()


def phi_jax_game(start_offset_hours=-4.0):
    game = parse_event(load_fixture("pm_nfl_events.json")["events"][1], League.NFL)
    return replace(game, start_time=T0 + timedelta(hours=start_offset_hours))


def poll(t):
    """One recorded poll, with the ESPN kickoff moved to KICKOFF."""
    original = next(p for p in REPLAY["polls"] if p["t"] == t)
    p = copy.deepcopy(original)
    stamp = KICKOFF.strftime("%Y-%m-%dT%H:%MZ")
    p["espn_event"]["date"] = stamp
    p["espn_event"]["competitions"][0]["date"] = stamp
    p["espn_event"]["competitions"][0]["startDate"] = stamp
    return p


def build(clock, model, games=None, enabled=False, **settings_overrides):
    settings = load_settings(ALERTS_ENABLED=enabled, DATABASE_PATH=":memory:", **settings_overrides)
    games = games if games is not None else {League.NFL: [phi_jax_game()]}
    p20 = poll(20)
    reader = FakeReader(clock, games, bbo={SLUG: p20["bbo"]}, books={SLUG: p20["book"]})
    feed = FakeFeed(clock)
    sender = FakeSender()
    scanner = Scanner(
        settings,
        reader=reader,
        feed=feed,
        notifier=Notifier(settings, sender),
        diary=Diary(":memory:"),
        model=model,
        status=RuntimeStatus(),
        now=clock.now,
        sleep=clock.sleep,
    )
    scanner.start()
    return scanner, reader, feed, sender


# -- window -------------------------------------------------------------------


def test_window_wakes_thirty_minutes_before_kickoff_and_sleeps_after(model):
    clock = Clock()
    scanner, reader, feed, _ = build(
        clock, model, games={League.NFL: [phi_jax_game(start_offset_hours=1.0)]}
    )
    scanner._refresh_games(clock.now(), force=True)
    awake, note, change = scanner.window(at(0))
    assert awake is False and note.startswith("next wake")
    assert change == T0 + timedelta(minutes=30)
    awake, note, _ = scanner.window(T0 + timedelta(minutes=30))
    assert awake is True and "in window" in note
    awake, _, _ = scanner.window(T0 + timedelta(hours=6, minutes=1))
    assert awake is False  # five hours after kickoff with no final: treated as over
    scanner.finished.add((League.NFL, "nfl-phi-jax-2026-10-11"))
    awake, note, _ = scanner.window(T0 + timedelta(hours=2))
    assert awake is False and note == "no games listed"


def test_asleep_run_once_returns_without_polling_scores(model):
    clock = Clock()
    scanner, reader, feed, _ = build(
        clock, model, games={League.NFL: [phi_jax_game(start_offset_hours=4.0)]}
    )
    assert scanner.run_forever(once=True) == 0
    assert feed.calls == 0
    assert scanner.status.awake is False


# -- the alert path -----------------------------------------------------------


def test_two_passes_produce_one_alert_and_record_everything(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=True)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    first = scanner.scan_once()
    assert first.alerts == [] and len(first.near_misses) == 1
    assert first.near_misses[0].reason == "not confirmed on two polls"
    assert first.candidates == 1 and first.live_games == 1 and first.matched == 1
    assert "book:" not in "".join(reader.calls)  # no book read before the cheaper checks pass

    clock.t = 20
    feed.states[League.NFL] = [poll(20)["espn_event"]]
    second = scanner.scan_once()
    assert len(second.alerts) == 1
    alert = second.alerts[0]
    assert alert.pick == "PHI" and alert.buy_price == pytest.approx(0.8915)
    assert alert.message.startswith("NFL - PHI at JAX\nPHI leads 24-14, Q4 7:10, PHI ball")
    assert sender.messages == [alert.message]
    assert reader.calls.count(f"book:{SLUG}") == 1
    rows = scanner.diary.alerts()
    assert len(rows) == 1 and rows[0]["sent"] == 1 and rows[0]["message"] == alert.message
    assert scanner.history.count_today(clock.now()) == 1
    assert scanner.status.alerts_today == 1 and scanner.status.last_alert_at is not None
    # the game list is not re-read inside 60 seconds
    assert reader.calls.count("games:nfl") == 1


def test_nothing_is_sent_in_shadow_mode_but_the_alert_is_recorded(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=False)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    scanner.scan_once()
    clock.t = 20
    feed.states[League.NFL] = [poll(20)["espn_event"]]
    summary = scanner.scan_once()
    assert len(summary.alerts) == 1
    assert sender.messages == []
    row = scanner.diary.alerts()[0]
    assert (
        row["sent"] == 0 and row["enabled"] == 0 and row["message"].startswith("NFL - PHI at JAX")
    )


def test_follow_up_prices_are_read_30_and_120_seconds_after_an_alert(model):
    clock = Clock()
    scanner, reader, feed, _ = build(clock, model, enabled=True)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    scanner.scan_once()
    clock.t = 20
    feed.states[League.NFL] = [poll(20)["espn_event"]]
    scanner.scan_once()
    alert_id = scanner.diary.alerts()[0]["id"]
    clock.t = 45
    feed.states[League.NFL] = [poll(25)["espn_event"]]
    reader.bbo[SLUG] = poll(30)["bbo"]  # price moved to 0.8615 by then
    scanner.scan_once()
    assert scanner.diary.alert(alert_id)["followup_30"] is None  # not due until t=50
    clock.t = 55
    scanner.scan_once()
    row = scanner.diary.alert(alert_id)
    assert row["followup_30"] == pytest.approx(0.8615) and row["followup_120"] is None
    clock.t = 145
    scanner.scan_once()
    row = scanner.diary.alert(alert_id)
    assert row["followup_120"] == pytest.approx(0.8615)


def test_near_misses_are_not_written_again_inside_a_minute(model):
    clock = Clock()
    scanner, reader, feed, _ = build(clock, model)
    for t in (0, 5, 10, 15):
        clock.t = t
        feed.states[League.NFL] = [poll(t)["espn_event"]]
        scanner.scan_once()
    assert scanner.diary.near_misses_by_reason() == {
        "not confirmed on two polls": 1,
        "rule 5: score changed recently": 1,
    }


def test_finished_game_is_graded_and_closes_the_window(model):
    clock = Clock()
    scanner, reader, feed, _ = build(clock, model, enabled=True)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    scanner.scan_once()
    clock.t = 20
    feed.states[League.NFL] = [poll(20)["espn_event"]]
    scanner.scan_once()
    final = dict(poll(340)["espn_event"])
    final["status"] = {
        **final["status"],
        "type": {
            **final["status"]["type"],
            "name": "STATUS_FINAL",
            "state": "post",
            "completed": True,
        },
    }
    clock.t = 400
    feed.states[League.NFL] = [final]
    summary = scanner.scan_once()
    assert summary.live_games == 0
    row = scanner.diary.alerts()[0]
    assert row["outcome"] == "win" and row["final_away"] == 31 and row["final_home"] == 21
    assert (League.NFL, "nfl-phi-jax-2026-10-11") in scanner.finished
    assert scanner.window(clock.now())[0] is False


def test_daily_cap_sends_the_paused_message_once(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=True, MAX_ALERTS_PER_DAY=1)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    scanner.scan_once()
    clock.t = 20
    feed.states[League.NFL] = [poll(20)["espn_event"]]
    scanner.scan_once()
    assert len(sender.messages) == 2
    assert sender.messages[1] == "Alerts paused for today (1 sent, the daily cap)"
    clock.t = 30
    feed.states[League.NFL] = [poll(30)["espn_event"]]
    reader.bbo[SLUG] = poll(30)["bbo"]
    summary = scanner.scan_once()
    assert summary.alerts == [] and summary.near_misses[0].reason == "rule 7: daily cap"
    assert len(sender.messages) == 2


# -- feeds failing ------------------------------------------------------------


def test_score_feed_failure_message_after_two_minutes_then_every_fifteen(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=True)
    feed.states[League.NFL] = ScoreFeedError("nfl: timeout")
    for t in (0, 60, 119):
        clock.t = t
        scanner.scan_once()
    assert sender.messages == []
    assert scanner.status.score_feed_failing_since == at(0).isoformat()
    clock.t = 125
    scanner.scan_once()
    assert sender.messages == ["Score feed is failing (since 3:00:00 PM CT)"]
    clock.t = 600
    scanner.scan_once()
    assert len(sender.messages) == 1
    clock.t = 125 + 900
    scanner.scan_once()
    assert len(sender.messages) == 2
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    clock.t = 1100
    scanner.scan_once()
    assert scanner.score_fail_since is None and scanner.status.score_feed_failing_since is None
    kinds = [e["kind"] for e in scanner.diary.events()]
    assert kinds[0] == "score_feed_recovered" and "score_feed_failure" in kinds


def test_price_feed_failure_is_tracked_and_the_pass_survives(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=True)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    scanner.scan_once()
    clock.t = 20
    feed.states[League.NFL] = [poll(20)["espn_event"]]
    reader.fail = True
    summary = scanner.scan_once()
    assert summary.alerts == [] and summary.errors == ["down"]
    assert scanner.status.price_feed_failing_since == at(20).isoformat()
    clock.t = 150
    scanner.scan_once()
    assert sender.messages == ["Price feed is failing (since 3:00:20 PM CT)"]
    reader.fail = False
    clock.t = 155
    summary = scanner.scan_once()
    assert len(summary.alerts) == 1 and scanner.price_fail_since is None


# -- clinched overs ----------------------------------------------------------


def test_clinched_over_path_reads_only_clinched_lines_and_sends_the_deepest(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=True)
    game = phi_jax_game()
    lines = {t.line: t for t in game.totals}
    assert set(lines) == {41.5, 47.5}
    for total in lines.values():
        reader.bbo[total.market_slug] = {
            "marketData": {
                "bestBid": {"value": "0.9500", "currency": "USD"},
                "bestAsk": {"value": "0.9600", "currency": "USD"},
                "state": "MARKET_STATE_OPEN",
            }
        }
    reader.books[lines[41.5].market_slug] = {
        "marketData": {
            "bids": [],
            "offers": [{"px": {"value": "0.9600", "currency": "USD"}, "qty": "60.0000"}],
            "state": "MARKET_STATE_OPEN",
        }
    }
    reader.books[lines[47.5].market_slug] = {
        "marketData": {
            "bids": [],
            "offers": [{"px": {"value": "0.9600", "currency": "USD"}, "qty": "500.0000"}],
            "state": "MARKET_STATE_OPEN",
        }
    }
    event = poll(340)["espn_event"]  # 31-21: 52 points, both lines clinched; JAX ball, 0:15 left
    feed.states[League.NFL] = [event]
    scanner.scan_once()  # first sight of the score
    clock.t = 65
    scanner.scan_once()
    assert reader.calls.count(f"quote:{lines[41.5].market_slug}") == 1
    assert reader.calls.count(f"quote:{lines[47.5].market_slug}") == 1
    over_alerts = [a for a in scanner.diary.alerts() if a["alert_type"] == "clinched_over"]
    assert len(over_alerts) == 1 and over_alerts[0]["pick"] == "OVER 47.5"
    assert over_alerts[0]["dollars_available"] == pytest.approx(480.0)
    assert any(
        m.startswith("NFL - PHI at JAX\nOVER 47.5 is clinched: 31-21 (52 points)")
        for m in sender.messages
    )


def test_clinched_overs_off_reads_no_total_prices(model):
    clock = Clock()
    scanner, reader, feed, _ = build(clock, model, CLINCHED_OVERS_ENABLED=False)
    feed.states[League.NFL] = [poll(340)["espn_event"]]
    scanner.scan_once()
    clock.t = 65
    scanner.scan_once()
    assert not any(c.startswith("quote:tsc") for c in reader.calls)


# -- run_forever --------------------------------------------------------------


def test_run_once_awake_sends_the_heartbeat_and_scans(model):
    clock = Clock()
    scanner, reader, feed, sender = build(clock, model, enabled=True)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    assert scanner.run_forever(once=True) == 0
    assert sender.messages == ["Scanner is up, watching 1 game"]
    assert scanner.status.awake is True and feed.calls == 1
    # a second day's first window sends another heartbeat; the same day does not
    scanner._heartbeat(clock.now(), 1)
    assert len(sender.messages) == 1
    scanner._heartbeat(clock.now() + timedelta(days=1), 2)
    assert sender.messages[-1] == "Scanner is up, watching 2 games"


def test_start_retries_until_polymarket_answers(model):
    clock = Clock()
    settings = load_settings(DATABASE_PATH=":memory:")
    reader = FakeReader(clock, {League.NFL: [phi_jax_game(start_offset_hours=4.0)]})
    reader.fail = True
    scanner = Scanner(
        settings,
        reader=reader,
        feed=FakeFeed(clock),
        notifier=Notifier(settings, FakeSender()),
        diary=Diary(":memory:"),
        model=model,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert scanner.run_forever(once=True) == 1
    reader.fail = False
    assert scanner.run_forever(once=True) == 0
    assert scanner.leagues == {League.NFL: "nfl", League.CFB: "cfb"}


def test_pass_survives_an_unexpected_exception(model, monkeypatch):
    clock = Clock()
    scanner, reader, feed, _ = build(clock, model)
    feed.states[League.NFL] = [poll(0)["espn_event"]]
    monkeypatch.setattr(scanner, "scan_once", lambda now=None: 1 / 0)
    assert scanner.run_forever(once=True) == 1
    assert "ZeroDivisionError" in scanner.status.last_error


def test_unknown_or_delayed_states_never_reach_the_rules(model):
    clock = Clock()
    scanner, reader, feed, _ = build(clock, model)
    delayed = dict(poll(20)["espn_event"])
    delayed["status"] = {
        **delayed["status"],
        "type": {**delayed["status"]["type"], "name": "STATUS_DELAYED"},
    }
    feed.states[League.NFL] = [delayed]
    scanner.scan_once()
    clock.t = 20
    summary = scanner.scan_once()
    assert summary.candidates == 0 and summary.alerts == []
    assert all(s.status is GameStatus.DELAYED for s in scanner.last_scores[League.NFL])
