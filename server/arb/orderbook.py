from __future__ import annotations

import time
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from arb.types import (
    BookEligibility,
    BookUpdateResult,
    EventKind,
    MarketEvent,
    PriceLevel,
    TopOfBook,
)


class SortedLevels:
    """Maintains sorted price levels with deterministic top-of-book lookup."""

    def __init__(self, descending: bool) -> None:
        self.descending = descending
        self._prices: list[Decimal] = []
        self._sizes: dict[Decimal, Decimal] = {}

    def clear(self) -> None:
        self._prices.clear()
        self._sizes.clear()

    def set_level(self, price: Decimal, size: Decimal) -> None:
        if size <= 0:
            self.remove(price)
            return

        if price not in self._sizes:
            idx = bisect_left(self._prices, price)
            self._prices.insert(idx, price)
        self._sizes[price] = size

    def remove(self, price: Decimal) -> None:
        if price not in self._sizes:
            return
        del self._sizes[price]
        idx = bisect_left(self._prices, price)
        if idx < len(self._prices) and self._prices[idx] == price:
            self._prices.pop(idx)

    def best(self) -> PriceLevel | None:
        if not self._prices:
            return None
        price = self._prices[-1] if self.descending else self._prices[0]
        return PriceLevel(price=price, size=self._sizes[price])

    def top_n(self, limit: int) -> list[PriceLevel]:
        if self.descending:
            prices = list(reversed(self._prices[-limit:]))
        else:
            prices = self._prices[:limit]
        return [PriceLevel(price=price, size=self._sizes[price]) for price in prices]


@dataclass
class OrderBook:
    bids: SortedLevels = field(default_factory=lambda: SortedLevels(descending=True))
    asks: SortedLevels = field(default_factory=lambda: SortedLevels(descending=False))
    sequence: int | None = None
    stale: bool = True
    last_timestamp_ns: int = 0
    initialized: bool = False
    continuous: bool = False
    connected: bool = True
    last_received_monotonic_ns: int | None = None
    cached_top: TopOfBook | None = None

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.sequence = None
        self.stale = True
        self.last_timestamp_ns = 0
        self.initialized = False
        self.continuous = False
        self.last_received_monotonic_ns = None
        self.cached_top = None


class OrderBookManager:
    def __init__(
        self, max_age_seconds: float = 30.0, clock: Callable[[], int] = time.monotonic_ns
    ) -> None:
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._max_age_ns = int(max_age_seconds * 1_000_000_000)
        self._clock = clock
        self._exchange_connected: dict[str, bool] = {}

    def set_exchange_connected(self, exchange: str, connected: bool) -> list[BookEligibility]:
        self._exchange_connected[exchange] = connected
        affected: list[BookEligibility] = []
        for (book_exchange, _pair), book in self._books.items():
            if book_exchange != exchange:
                continue
            book.connected = connected
            if not connected:
                book.clear()
                book.connected = False
            affected.append(self.eligibility(book_exchange, _pair))
        return affected

    def eligibility_for(self, pairs: list[tuple[str, str]]) -> list[BookEligibility]:
        """Return the canonical current status for each requested book."""
        now = self._clock()
        return [self.eligibility(exchange, pair, now) for exchange, pair in pairs]

    def apply(
        self, event: MarketEvent, received_monotonic_ns: int | None = None
    ) -> BookUpdateResult:
        key = (event.exchange, event.pair)
        book = self._books.get(key)
        if book is None:
            book = self._books[key] = OrderBook()
        book.connected = self._exchange_connected.get(event.exchange, True)
        received_at = self._clock() if received_monotonic_ns is None else received_monotonic_ns

        if not book.connected:
            return BookUpdateResult(accepted=False, reason="disconnected", stale=True)

        if event.kind is EventKind.SNAPSHOT:
            self._apply_snapshot(book, event, received_at)
            top = self.top_of_book(event.exchange, event.pair)
            if top is None:
                book.clear()
                return BookUpdateResult(
                    accepted=False,
                    reason="snapshot_incomplete",
                    stale=True,
                    requires_resync=True,
                )
            if top.best_bid_price >= top.best_ask_price:
                book.clear()
                return BookUpdateResult(
                    accepted=False,
                    reason="snapshot_crossed",
                    stale=True,
                    requires_resync=True,
                )
            return BookUpdateResult(accepted=True, top_of_book=top)

        if not book.initialized or not book.continuous or book.stale or book.sequence is None:
            return BookUpdateResult(accepted=False, reason="book_stale", stale=True)

        if event.sequence <= book.sequence:
            return BookUpdateResult(accepted=False, reason="out_of_order")

        if event.sequence != book.sequence + 1:
            book.clear()
            return BookUpdateResult(
                accepted=False,
                reason="sequence_gap",
                stale=True,
                requires_resync=True,
            )

        self._apply_delta(book, event, received_at)
        top = self.top_of_book(event.exchange, event.pair)
        if top is None:
            return BookUpdateResult(accepted=False, reason="book_incomplete")
        if top.best_bid_price >= top.best_ask_price:
            book.clear()
            return BookUpdateResult(
                accepted=False,
                reason="crossed_book",
                stale=True,
                requires_resync=True,
            )
        return BookUpdateResult(accepted=True, top_of_book=top)

    def best_bid(self, exchange: str, pair: str) -> Decimal | None:
        book = self._books.get((exchange, pair))
        if not book:
            return None
        best = book.bids.best()
        return best.price if best else None

    def best_ask(self, exchange: str, pair: str) -> Decimal | None:
        book = self._books.get((exchange, pair))
        if not book:
            return None
        best = book.asks.best()
        return best.price if best else None

    def top_of_book(self, exchange: str, pair: str) -> TopOfBook | None:
        book = self._books.get((exchange, pair))
        if not book or book.stale or book.sequence is None:
            return None
        if book.cached_top is not None:
            return book.cached_top
        best_bid = book.bids.best()
        best_ask = book.asks.best()
        if best_bid is None or best_ask is None:
            return None
        book.cached_top = TopOfBook(
            exchange=exchange,
            pair=pair,
            best_bid_price=best_bid.price,
            best_bid_size=best_bid.size,
            best_ask_price=best_ask.price,
            best_ask_size=best_ask.size,
            sequence=book.sequence,
            timestamp_ns=book.last_timestamp_ns,
        )
        return book.cached_top

    def eligibility(
        self, exchange: str, pair: str, now_monotonic_ns: int | None = None
    ) -> BookEligibility:
        book = self._books.get((exchange, pair))
        now = self._clock() if now_monotonic_ns is None else now_monotonic_ns
        if book is None:
            connected = self._exchange_connected.get(exchange, True)
            return BookEligibility(
                exchange,
                pair,
                False,
                False,
                connected,
                None,
                self._max_age_ns,
                False,
                "missing" if connected else "disconnected",
            )
        age_ns = (
            None
            if book.last_received_monotonic_ns is None
            else max(0, now - book.last_received_monotonic_ns)
        )
        reason = None
        if not book.connected:
            reason = "disconnected"
        elif not book.initialized:
            reason = "uninitialized"
        elif not book.continuous or book.stale:
            reason = "discontinuous"
        elif age_ns is None or age_ns > self._max_age_ns:
            reason = "too_old"
        else:
            top = self.top_of_book(exchange, pair)
            if top is None:
                reason = "incomplete"
            elif top.best_bid_price >= top.best_ask_price:
                reason = "crossed"
        return BookEligibility(
            exchange,
            pair,
            book.initialized,
            book.continuous,
            book.connected,
            age_ns,
            self._max_age_ns,
            reason is None,
            reason,
        )

    def eligible_top_of_book(
        self, exchange: str, pair: str, now_monotonic_ns: int | None = None
    ) -> TopOfBook | None:
        if not self.eligibility(exchange, pair, now_monotonic_ns).eligible:
            return None
        return self.top_of_book(exchange, pair)

    def eligible_books(self, pair: str, now_monotonic_ns: int | None = None) -> list[TopOfBook]:
        """Every eligible top of book for one pair, or nothing if fewer than two.

        The manager already knows which exchanges hold a book for this pair, so
        callers do not supply a venue roster and cannot drift from the adapters
        that are actually running.
        """
        books = [
            top
            for exchange, book_pair in sorted(self._books)
            if book_pair == pair
            if (top := self.eligible_top_of_book(exchange, pair, now_monotonic_ns)) is not None
        ]
        return books if len(books) >= 2 else []

    def known_pairs(self) -> list[tuple[str, str]]:
        return sorted(self._books.keys())

    def level_snapshot(
        self, exchange: str, pair: str, limit: int = 10
    ) -> tuple[list[PriceLevel], list[PriceLevel]] | None:
        book = self._books.get((exchange, pair))
        if not book or book.stale or book.sequence is None:
            return None
        return (book.bids.top_n(limit), book.asks.top_n(limit))

    def _apply_snapshot(
        self, book: OrderBook, event: MarketEvent, received_monotonic_ns: int
    ) -> None:
        book.clear()
        for level in event.bids:
            book.bids.set_level(level.price, level.size)
        for level in event.asks:
            book.asks.set_level(level.price, level.size)
        book.sequence = event.sequence
        book.stale = False
        book.initialized = True
        book.continuous = True
        book.last_timestamp_ns = event.timestamp_ns
        book.last_received_monotonic_ns = received_monotonic_ns

    def _apply_delta(self, book: OrderBook, event: MarketEvent, received_monotonic_ns: int) -> None:
        book.cached_top = None
        for level in event.bids:
            book.bids.set_level(level.price, level.size)
        for level in event.asks:
            book.asks.set_level(level.price, level.size)
        book.sequence = event.sequence
        book.last_timestamp_ns = event.timestamp_ns
        book.last_received_monotonic_ns = received_monotonic_ns
