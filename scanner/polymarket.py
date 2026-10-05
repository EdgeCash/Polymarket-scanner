"""Read-only Polymarket US client.

Everything here only reads public endpoints. There is no code path that places,
changes or cancels an order, and the client is built with no credentials.

Pricing rule, confirmed against live data on 5 October 2026 (see README):

* A game's moneyline is one instrument with a long side and a short side.
* Buying the long-side team costs the best ask.
* Buying the short-side team costs 1 minus the best bid.

The event feed carries each side's ``quote`` which equals exactly those values,
and the BBO carries ``longQuote``/``shortQuote``. The reader computes the price
from ``bestBid``/``bestAsk`` and refuses to price a side when the feed's own
quote disagrees, so a surprise in the data turns into silence rather than a
wrong alert.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from scanner.config import DEFAULT_THETA, POLYMARKET_MAX_RPS
from scanner.models import (
    Availability,
    Book,
    BookLevel,
    League,
    MarketTeam,
    PolymarketGame,
    Quote,
    TotalMarket,
)

log = logging.getLogger(__name__)

MONEYLINE_TYPE = "football_team_full_game_winner"
GAME_TOTAL_TYPE = "football_team_full_game_total"
MONEYLINE_TYPE_V2 = "SPORTS_MARKET_TYPE_MONEYLINE"
OPEN_STATE = "MARKET_STATE_OPEN"
EVENTS_PAGE_SIZE = 100
LEAGUES_PAGE_SIZE = 50
PRICE_TICK = 0.0025  # orderPriceMinTickSize on football markets

# Names a college football league might go by. The slug itself is discovered.
CFB_NAMES = {"cfb", "ncaaf", "college football", "ncaa football"}


class PolymarketError(Exception):
    """A read failed. The caller treats it as 'price feed unavailable'."""


class RateLimited(PolymarketError):
    """The API returned 429. The reader is backing off; do not retry yet."""


class Transport(Protocol):
    """The one call the reader needs: a GET that returns parsed JSON."""

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any: ...


class SdkTransport:
    """Transport backed by the official SDK, created with no credentials."""

    def __init__(self, timeout: float = 10.0) -> None:
        from polymarket_us import PolymarketUS

        # No key_id, no secret_key: public endpoints only.
        self._client = PolymarketUS(timeout=timeout, max_retries=1)

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        from polymarket_us import APIConnectionError, APITimeoutError, RateLimitError

        try:
            return self._client.get(path, query=query)
        except RateLimitError as exc:
            raise RateLimited(str(exc)) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise PolymarketError(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        self._client.close()


class RateLimiter:
    """Keeps requests at or below ``max_rps`` by spacing them evenly."""

    def __init__(
        self,
        max_rps: float = POLYMARKET_MAX_RPS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = 1.0 / max_rps
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = clock()

    def wait(self) -> None:
        now = self._clock()
        if now < self._next_allowed:
            self._sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + self.min_interval


def _amount(value: Any) -> float | None:
    """Parse a ``{"value": "0.93", "currency": "USD"}`` amount. None if absent."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price != price or price < 0.0 or price > 1.0:
        return None
    return price


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _theta(market: dict[str, Any]) -> float:
    value = market.get("feeCoefficient")
    try:
        theta = float(value) if value is not None else DEFAULT_THETA
    except (TypeError, ValueError):
        theta = DEFAULT_THETA
    if theta <= 0 or theta > 1:
        theta = DEFAULT_THETA
    return theta


def _is_moneyline(market: dict[str, Any]) -> bool:
    return (
        market.get("sportsMarketType") == MONEYLINE_TYPE
        or market.get("sportsMarketTypeV2") == MONEYLINE_TYPE_V2
        or market.get("marketType") == "moneyline"
    )


def _is_game_total(market: dict[str, Any]) -> bool:
    return market.get("sportsMarketType") == GAME_TOTAL_TYPE


def _parse_teams(raw_event: dict[str, Any]) -> dict[int, dict[str, Any]]:
    teams: dict[int, dict[str, Any]] = {}
    for team in raw_event.get("teams") or []:
        if not isinstance(team, dict) or team.get("id") is None:
            continue
        try:
            teams[int(team["id"])] = team
        except (TypeError, ValueError):
            continue
    return teams


def _team_abbreviation(team: dict[str, Any]) -> str:
    for key in ("displayAbbreviation", "abbreviation", "alias"):
        value = team.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _parse_total(market: dict[str, Any]) -> TotalMarket | None:
    slug = market.get("slug")
    line = market.get("line")
    if not slug or line is None:
        return None
    try:
        line_value = float(line)
    except (TypeError, ValueError):
        return None
    over_side = None
    under_side = None
    for side in market.get("marketSides") or []:
        label = (side.get("description") or "").strip().lower()
        if label == "over":
            over_side = side
        elif label == "under":
            under_side = side
    if over_side is None or under_side is None or over_side.get("long") is None:
        log.debug("total %s: cannot tell which side is the Over, skipped", slug)
        return None
    if bool(over_side.get("long")) == bool(under_side.get("long")):
        return None
    return TotalMarket(
        market_slug=str(slug),
        line=line_value,
        over_is_long=bool(over_side.get("long")),
        theta=_theta(market),
        active=bool(market.get("active")),
        closed=bool(market.get("closed")),
        over_tradable=bool(over_side.get("tradable", True)),
    )


def parse_event(raw_event: dict[str, Any], league: League) -> PolymarketGame | None:
    """Turn one raw event into a :class:`PolymarketGame`, or None if unusable."""
    event_id = raw_event.get("id")
    slug = raw_event.get("slug")
    if event_id is None or not slug:
        return None
    teams_by_id = _parse_teams(raw_event)
    if len(teams_by_id) != 2:
        log.info("event %s: expected 2 teams, found %d, skipped", slug, len(teams_by_id))
        return None

    moneylines = [m for m in raw_event.get("markets") or [] if _is_moneyline(m)]
    moneyline = None
    if moneylines:
        # Prefer an open one if there are several (there should be one).
        moneylines.sort(key=lambda m: (bool(m.get("closed")), not bool(m.get("active"))))
        moneyline = moneylines[0]

    market_teams: list[MarketTeam] = []
    moneyline_slug: str | None = None
    if moneyline is not None and moneyline.get("slug"):
        long_ids: dict[int, bool] = {}
        for side in moneyline.get("marketSides") or []:
            team_id = side.get("teamId")
            if team_id is None or side.get("long") is None:
                continue
            try:
                long_ids[int(team_id)] = bool(side["long"])
            except (TypeError, ValueError):
                continue
        if set(long_ids) == set(teams_by_id) and len(set(long_ids.values())) == 2:
            moneyline_slug = str(moneyline["slug"])
            for team_id, team in teams_by_id.items():
                market_teams.append(
                    MarketTeam(
                        team_id=team_id,
                        name=str(team.get("name") or ""),
                        abbreviation=_team_abbreviation(team),
                        is_long=long_ids[team_id],
                        nickname=str(team.get("alias") or ""),
                    )
                )
        else:
            log.info("event %s: moneyline sides do not map onto the two teams", slug)
    if not market_teams:
        for team_id, team in teams_by_id.items():
            market_teams.append(
                MarketTeam(
                    team_id=team_id,
                    name=str(team.get("name") or ""),
                    abbreviation=_team_abbreviation(team),
                    is_long=False,
                    nickname=str(team.get("alias") or ""),
                )
            )

    totals = []
    for market in raw_event.get("markets") or []:
        if _is_game_total(market):
            total = _parse_total(market)
            if total is not None:
                totals.append(total)
    totals.sort(key=lambda t: t.line)

    state = raw_event.get("eventState") or {}
    return PolymarketGame(
        event_id=str(event_id),
        event_slug=str(slug),
        league=league,
        title=str(raw_event.get("title") or slug),
        start_time=_parse_time(raw_event.get("startTime") or raw_event.get("startDate")),
        live=raw_event.get("live") if isinstance(raw_event.get("live"), bool) else None,
        ended=raw_event.get("ended") if isinstance(raw_event.get("ended"), bool) else None,
        teams=tuple(market_teams),
        moneyline_slug=moneyline_slug,
        moneyline_theta=_theta(moneyline) if moneyline else DEFAULT_THETA,
        moneyline_active=bool(moneyline.get("active")) if moneyline else False,
        moneyline_closed=bool(moneyline.get("closed")) if moneyline else True,
        totals=tuple(totals),
        display_score=raw_event.get("score") or state.get("score"),
        display_period=raw_event.get("period") or state.get("period"),
        display_elapsed=raw_event.get("elapsed") or state.get("elapsed"),
    )


def buy_price_from_bbo(
    market_data: dict[str, Any], is_long: bool
) -> tuple[float | None, float | None, float | None, str | None]:
    """Return (buy price, best bid, best ask, reason-if-none) for one side of a BBO."""
    best_bid = _amount(market_data.get("bestBid"))
    best_ask = _amount(market_data.get("bestAsk"))
    if is_long:
        price = best_ask
        feed_quote = _amount(market_data.get("longQuote"))
    else:
        price = None if best_bid is None else round(1.0 - best_bid, 6)
        feed_quote = _amount(market_data.get("shortQuote"))
    if price is None:
        return None, best_bid, best_ask, "no price on this side"
    if feed_quote is not None and abs(feed_quote - price) > PRICE_TICK / 2:
        return (
            None,
            best_bid,
            best_ask,
            (f"feed quote {feed_quote:.4f} disagrees with computed {price:.4f}"),
        )
    return price, best_bid, best_ask, None


def parse_quote(
    raw: dict[str, Any],
    market_slug: str,
    is_long: bool,
    theta: float,
    tradable: bool,
    fetched_at: datetime,
) -> Quote:
    """Build a :class:`Quote` from a ``markets.bbo`` response."""
    market_data = raw.get("marketData") if isinstance(raw, dict) else None
    if not isinstance(market_data, dict):
        return Quote(
            market_slug,
            "long" if is_long else "short",
            None,
            None,
            None,
            None,
            False,
            theta,
            fetched_at,
        )
    price, best_bid, best_ask, reason = buy_price_from_bbo(market_data, is_long)
    if reason:
        log.debug("quote %s %s: %s", market_slug, "long" if is_long else "short", reason)
    state = market_data.get("state")
    return Quote(
        market_slug=market_slug,
        side_label="long" if is_long else "short",
        buy_price=price,
        best_bid=best_bid,
        best_ask=best_ask,
        state=state if isinstance(state, str) else None,
        tradable=tradable,
        theta=theta,
        fetched_at=fetched_at,
    )


def _parse_levels(levels: Any, reverse: bool) -> tuple[BookLevel, ...]:
    out: list[BookLevel] = []
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        price = _amount(level.get("px"))
        try:
            qty = float(level.get("qty"))
        except (TypeError, ValueError):
            continue
        if price is None or qty <= 0:
            continue
        out.append(BookLevel(price=price, quantity=qty))
    out.sort(key=lambda lvl: lvl.price, reverse=reverse)
    return tuple(out)


def parse_book(raw: dict[str, Any], market_slug: str, fetched_at: datetime) -> Book:
    market_data = raw.get("marketData") if isinstance(raw, dict) else None
    if not isinstance(market_data, dict):
        return Book(market_slug, (), (), None, fetched_at)
    state = market_data.get("state")
    return Book(
        market_slug=market_slug,
        bids=_parse_levels(market_data.get("bids"), reverse=True),
        offers=_parse_levels(market_data.get("offers"), reverse=False),
        state=state if isinstance(state, str) else None,
        fetched_at=fetched_at,
    )


def buy_levels(book: Book, is_long: bool) -> tuple[BookLevel, ...]:
    """Prices one can buy a side at, cheapest first, with the contracts at each.

    Long side: the offers. Short side: each bid at ``p`` is a short-side offer at
    ``1 - p`` for the same number of contracts.
    """
    if is_long:
        return book.offers
    levels = [
        BookLevel(price=round(1.0 - lvl.price, 6), quantity=lvl.quantity) for lvl in book.bids
    ]
    levels.sort(key=lambda lvl: lvl.price)
    return tuple(levels)


def walk_book(levels: tuple[BookLevel, ...], max_price: float) -> Availability:
    """Add up what is for sale at or below ``max_price``, cheapest first."""
    dollars = 0.0
    contracts = 0.0
    best: float | None = None
    for level in levels:
        if level.price > max_price + 1e-9:
            break
        if best is None:
            best = level.price
        dollars += level.price * level.quantity
        contracts += level.quantity
    average = dollars / contracts if contracts > 0 else None
    return Availability(
        dollars=dollars, contracts=contracts, average_price=average, best_price=best
    )


class PolymarketReader:
    """Lists games and reads prices, staying under the request budget."""

    def __init__(
        self,
        transport: Transport | None = None,
        max_rps: float = POLYMARKET_MAX_RPS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transport = transport or SdkTransport()
        self._limiter = RateLimiter(max_rps=max_rps, clock=clock, sleep=sleep)
        self._clock = clock
        self._now = now
        self._backoff_until = 0.0
        self._backoff_seconds = 1.0
        self.request_count = 0

    # -- plumbing ---------------------------------------------------------

    def _get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        now = self._clock()
        if now < self._backoff_until:
            raise RateLimited(f"backing off for {self._backoff_until - now:.1f}s more")
        self._limiter.wait()
        self.request_count += 1
        try:
            result = self._transport.get(path, query)
        except RateLimited:
            # Stop immediately, wait at least one second, then grow the wait.
            self._backoff_until = self._clock() + self._backoff_seconds
            self._backoff_seconds = min(self._backoff_seconds * 2, 30.0)
            raise
        self._backoff_seconds = 1.0
        return result

    # -- leagues ------------------------------------------------------------

    def discover_leagues(self) -> dict[League, str]:
        """Find the NFL and college football league slugs from the leagues list."""
        leagues: list[dict[str, Any]] = []
        offset = 0
        for _ in range(20):  # a hard stop so a broken endpoint cannot loop forever
            page = self._get("/v2/leagues", {"limit": LEAGUES_PAGE_SIZE, "offset": offset})
            batch = page.get("leagues") if isinstance(page, dict) else None
            if not batch:
                break
            leagues.extend(b for b in batch if isinstance(b, dict))
            if len(batch) < LEAGUES_PAGE_SIZE:
                break
            offset += LEAGUES_PAGE_SIZE

        found: dict[League, str] = {}
        nfl = next((lg for lg in leagues if (lg.get("slug") or "").lower() == "nfl"), None)
        if nfl is None:
            raise PolymarketError("league 'nfl' not found in the leagues list")
        found[League.NFL] = "nfl"
        football_sport = nfl.get("sportId")
        for lg in leagues:
            if lg is nfl:
                continue
            names = {
                str(lg.get(key) or "").strip().lower() for key in ("slug", "name", "abbreviation")
            }
            same_sport = football_sport is None or lg.get("sportId") == football_sport
            if same_sport and names & CFB_NAMES:
                found[League.CFB] = str(lg.get("slug"))
                break
        if League.CFB not in found:
            raise PolymarketError("college football league not found in the leagues list")
        log.info("leagues discovered: %s", found)
        return found

    # -- games --------------------------------------------------------------

    def list_games(self, league: League, slug: str) -> list[PolymarketGame]:
        """Every event Polymarket lists for the league, parsed. Malformed ones are skipped."""
        games: list[PolymarketGame] = []
        offset = 0
        for _ in range(50):
            page = self._get(
                f"/v2/leagues/{slug}/events", {"limit": EVENTS_PAGE_SIZE, "offset": offset}
            )
            batch = page.get("events") if isinstance(page, dict) else None
            if not batch:
                break
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                game = parse_event(raw, league)
                if game is not None:
                    games.append(game)
            if len(batch) < EVENTS_PAGE_SIZE:
                break
            offset += EVENTS_PAGE_SIZE
        return games

    # -- prices -------------------------------------------------------------

    def quote(self, market_slug: str, is_long: bool, theta: float, tradable: bool = True) -> Quote:
        raw = self._get(f"/v1/markets/{market_slug}/bbo")
        return parse_quote(raw, market_slug, is_long, theta, tradable, self._now())

    def quotes_for_game(self, game: PolymarketGame) -> dict[str, Quote]:
        """Both teams' buy prices from one BBO read, keyed by team abbreviation."""
        if not game.moneyline_slug:
            return {}
        raw = self._get(f"/v1/markets/{game.moneyline_slug}/bbo")
        fetched = self._now()
        out: dict[str, Quote] = {}
        for team in game.teams:
            out[team.abbreviation] = parse_quote(
                raw, game.moneyline_slug, team.is_long, game.moneyline_theta, True, fetched
            )
        return out

    def book(self, market_slug: str) -> Book:
        raw = self._get(f"/v1/markets/{market_slug}/book")
        return parse_book(raw, market_slug, self._now())

    def over_quote(self, total: TotalMarket) -> Quote:
        return self.quote(total.market_slug, total.over_is_long, total.theta, total.over_tradable)
