from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import permutations
from typing import Literal

from arb.types import (
    EpisodeCloseReason,
    OpportunityEpisode,
    PricingLedger,
    RouteAgeEvent,
    RouteLegAges,
    TopOfBook,
)

Route = tuple[str, str, str]
LedgerFactory = Callable[[str, str, str, int], tuple[PricingLedger, ...]]
RouteObserver = Callable[[RouteAgeEvent], None]


def leg_ages(buy_book: TopOfBook, sell_book: TopOfBook, now_monotonic_ns: int) -> RouteLegAges:
    """Receipt ages of both legs at `now_monotonic_ns`, from local receipt times only."""
    buy_age = _age(buy_book.received_monotonic_ns, now_monotonic_ns)
    sell_age = _age(sell_book.received_monotonic_ns, now_monotonic_ns)
    skew = None if buy_age is None or sell_age is None else abs(buy_age - sell_age)
    return RouteLegAges(buy_age_ns=buy_age, sell_age_ns=sell_age, skew_ns=skew)


def _age(received_monotonic_ns: int | None, now_monotonic_ns: int) -> int | None:
    if received_monotonic_ns is None:
        return None
    return max(0, now_monotonic_ns - received_monotonic_ns)


_NO_AGES = RouteLegAges(buy_age_ns=None, sell_age_ns=None, skew_ns=None)


@dataclass(frozen=True)
class _Quote:
    """One route's spread as computed from a pair of top-of-book snapshots."""

    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    max_size: Decimal
    theoretical_profit: Decimal


@dataclass
class _OpenEpisode:
    episode: OpportunityEpisode
    start_monotonic_ns: int
    # Leg ages the last time both legs were present, reported on a close that
    # loses a leg. Never recomputed from the surviving leg alone.
    last_ages: RouteLegAges


class ArbitrageDetector:
    """Track theoretical cross-venue spreads as episodes rather than samples.

    The detector is stateful: it remembers which (pair, buy venue, sell venue)
    routes are currently above threshold. Each call reports only transitions,
    an episode that opened or one that closed, so a spread that rests across
    hundreds of book updates produces two events, not hundreds of rows. Peak
    spread and the size at that moment are tracked in memory and delivered on
    close.

    Wall-clock `timestamp_ns` identifies episodes and correlates them with
    books; `monotonic_ns` measures their lifetime. `end_ns` is always
    `start_ns` plus a monotonic delta, never a second wall-clock reading.
    """

    def __init__(
        self,
        threshold_pct: Decimal,
        *,
        monotonic_clock: Callable[[], int] = time.monotonic_ns,
        ledger_factory: LedgerFactory | None = None,
        route_observer: RouteObserver | None = None,
        observe_evaluations: bool = False,
    ) -> None:
        self.threshold_pct = threshold_pct
        self._monotonic_clock = monotonic_clock
        self._ledger_factory = ledger_factory
        # Leg-age diagnostics are observed, never gating: route eligibility is
        # decided by the canonical book status alone. `evaluated` events cost an
        # allocation per compared pair on every update, so research opts in and
        # live ingestion does not.
        self._route_observer = route_observer
        self._observe_evaluations = observe_evaluations and route_observer is not None
        self._open: dict[Route, _OpenEpisode] = {}
        # Wall clocks tick coarsely (about a millisecond on Windows), so a route
        # that closes and reopens inside one tick would otherwise reuse its
        # identity. Each route's starts are kept strictly increasing instead.
        self._last_start_ns: dict[Route, int] = {}

    def open_episodes(self) -> list[OpportunityEpisode]:
        return [state.episode for state in self._open.values()]

    def detect_for_pair(
        self,
        pair: str,
        books: list[TopOfBook],
        timestamp_ns: int,
        monotonic_ns: int | None = None,
    ) -> list[OpportunityEpisode]:
        """Return episodes that opened or closed given the pair's eligible books.

        A route absent from `books` (a leg's book is no longer eligible) closes
        as `book_ineligible`; a route whose legs are present but no longer
        above threshold closes as `spread_closed`.
        """
        now_monotonic_ns = self._monotonic_clock() if monotonic_ns is None else monotonic_ns
        base_asset, separator, quote_asset = pair.rpartition("-")
        books = [book for book in books if book.pair == pair]
        quotes: dict[Route, _Quote] = {}
        ages: dict[Route, RouteLegAges] = {}
        if base_asset and separator and quote_asset and len(books) >= 2:
            for buy_book, sell_book in permutations(books, 2):
                route = (pair, buy_book.exchange, sell_book.exchange)
                if self._route_observer is not None:
                    ages[route] = leg_ages(buy_book, sell_book, now_monotonic_ns)
                    if self._observe_evaluations:
                        self._route_observer(
                            RouteAgeEvent(
                                kind="evaluated",
                                pair=pair,
                                buy_exchange=route[1],
                                sell_exchange=route[2],
                                start_ns=None,
                                monotonic_ns=now_monotonic_ns,
                                spread_pct=_spread_pct(buy_book, sell_book),
                                ages=ages[route],
                            )
                        )
                quote = self._quote(buy_book, sell_book)
                if quote is not None:
                    quotes[route] = quote

        events: list[OpportunityEpisode] = []
        present = {book.exchange for book in books}
        for route, state in list(self._open.items()):
            if route[0] != pair or route in quotes:
                continue
            _, buy_exchange, sell_exchange = route
            legs_present = buy_exchange in present and sell_exchange in present
            reason: EpisodeCloseReason = "spread_closed" if legs_present else "book_ineligible"
            close_spread = self._spread_pct(buy_exchange, sell_exchange, books)
            if route in ages:
                # Both legs were compared at this instant, so the close carries
                # these ages; a missing leg keeps the last pair actually seen.
                state.last_ages = ages[route]
            events.append(
                self._close(route, state, timestamp_ns, now_monotonic_ns, reason, close_spread)
            )

        for route, quote in quotes.items():
            existing = self._open.get(route)
            if existing is None:
                start_ns = max(timestamp_ns, self._last_start_ns.get(route, timestamp_ns - 1) + 1)
                self._last_start_ns[route] = start_ns
                episode = OpportunityEpisode(
                    start_ns=start_ns,
                    pair=pair,
                    quote_asset=quote_asset,
                    buy_exchange=route[1],
                    sell_exchange=route[2],
                    buy_price=quote.buy_price,
                    sell_price=quote.sell_price,
                    spread_pct=quote.spread_pct,
                    max_size=quote.max_size,
                    theoretical_profit=quote.theoretical_profit,
                    peak_spread_pct=quote.spread_pct,
                    peak_size=quote.max_size,
                    peak_profit=quote.theoretical_profit,
                    pricing_ledgers=self._ledgers(route, now_monotonic_ns),
                )
                self._open[route] = _OpenEpisode(
                    episode, now_monotonic_ns, ages.get(route, _NO_AGES)
                )
                events.append(episode)
                self._observe("open", route, start_ns, now_monotonic_ns, quote.spread_pct, ages)
            else:
                existing.last_ages = ages.get(route, _NO_AGES)
                if quote.spread_pct > existing.episode.peak_spread_pct:
                    existing.episode = replace(
                        existing.episode,
                        peak_spread_pct=quote.spread_pct,
                        peak_size=quote.max_size,
                        peak_profit=quote.theoretical_profit,
                        pricing_ledgers=self._ledgers(route, now_monotonic_ns),
                    )
                    self._observe(
                        "peak",
                        route,
                        existing.episode.start_ns,
                        now_monotonic_ns,
                        quote.spread_pct,
                        ages,
                    )
        return events

    def _observe(
        self,
        kind: Literal["open", "peak"],
        route: Route,
        start_ns: int,
        now_monotonic_ns: int,
        spread_pct: Decimal,
        ages: dict[Route, RouteLegAges],
    ) -> None:
        if self._route_observer is None:
            return
        self._route_observer(
            RouteAgeEvent(
                kind=kind,
                pair=route[0],
                buy_exchange=route[1],
                sell_exchange=route[2],
                start_ns=start_ns,
                monotonic_ns=now_monotonic_ns,
                spread_pct=spread_pct,
                ages=ages[route],
            )
        )

    def _ledgers(self, route: Route, now_monotonic_ns: int) -> tuple[PricingLedger, ...]:
        if self._ledger_factory is None:
            return ()
        return self._ledger_factory(*route, now_monotonic_ns)

    def close_for_book(
        self,
        exchange: str,
        pair: str,
        timestamp_ns: int,
        monotonic_ns: int | None = None,
    ) -> list[OpportunityEpisode]:
        """Close every open episode on `pair` with `exchange` as a leg.

        Called when a book turns ineligible on its own update, which returns
        before detection would otherwise notice the leg is gone.
        """
        now_monotonic_ns = self._monotonic_clock() if monotonic_ns is None else monotonic_ns
        return [
            self._close(route, state, timestamp_ns, now_monotonic_ns, "book_ineligible", None)
            for route, state in list(self._open.items())
            if route[0] == pair and exchange in route[1:]
        ]

    def close_all(
        self, timestamp_ns: int, monotonic_ns: int | None = None
    ) -> list[OpportunityEpisode]:
        """Close every open episode as `shutdown`, so none is left dangling in storage."""
        now_monotonic_ns = self._monotonic_clock() if monotonic_ns is None else monotonic_ns
        return [
            self._close(route, state, timestamp_ns, now_monotonic_ns, "shutdown", None)
            for route, state in list(self._open.items())
        ]

    def _close(
        self,
        route: Route,
        state: _OpenEpisode,
        timestamp_ns: int,
        now_monotonic_ns: int,
        reason: EpisodeCloseReason,
        close_spread_pct: Decimal | None,
    ) -> OpportunityEpisode:
        del self._open[route]
        # The wall clock is deliberately not read again here: a step during the
        # episode would otherwise inflate or negate its lifetime.
        duration_ns = max(0, now_monotonic_ns - state.start_monotonic_ns)
        closed = replace(
            state.episode,
            end_ns=state.episode.start_ns + duration_ns,
            close_spread_pct=close_spread_pct,
            close_reason=reason,
        )
        if self._route_observer is not None:
            self._route_observer(
                RouteAgeEvent(
                    kind="close",
                    pair=route[0],
                    buy_exchange=route[1],
                    sell_exchange=route[2],
                    start_ns=closed.start_ns,
                    monotonic_ns=now_monotonic_ns,
                    spread_pct=close_spread_pct,
                    ages=state.last_ages,
                    close_reason=reason,
                )
            )
        return closed

    def _quote(self, buy_book: TopOfBook, sell_book: TopOfBook) -> _Quote | None:
        if sell_book.best_bid_price <= buy_book.best_ask_price:
            return None
        spread_pct = _spread_pct(buy_book, sell_book)
        if spread_pct < self.threshold_pct:
            return None
        max_size = min(buy_book.best_ask_size, sell_book.best_bid_size)
        return _Quote(
            buy_price=buy_book.best_ask_price,
            sell_price=sell_book.best_bid_price,
            spread_pct=spread_pct,
            max_size=max_size,
            theoretical_profit=max_size * (sell_book.best_bid_price - buy_book.best_ask_price),
        )

    @staticmethod
    def _spread_pct(
        buy_exchange: str, sell_exchange: str, books: list[TopOfBook]
    ) -> Decimal | None:
        """The route's raw spread at close, possibly negative; None when a leg is missing."""
        by_exchange = {book.exchange: book for book in books}
        buy_book = by_exchange.get(buy_exchange)
        sell_book = by_exchange.get(sell_exchange)
        if buy_book is None or sell_book is None:
            return None
        return _spread_pct(buy_book, sell_book)


def _spread_pct(buy_book: TopOfBook, sell_book: TopOfBook) -> Decimal:
    return (
        (sell_book.best_bid_price - buy_book.best_ask_price) / buy_book.best_ask_price
    ) * Decimal("100")
