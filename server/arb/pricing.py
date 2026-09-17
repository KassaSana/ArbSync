"""Depth-aware executable pricing: walk a book to the VWAP for a quote notional.

Top-of-book answers "what is the best price" but not "what would an order of
this size pay". Walking the book gives the volume-weighted average price for
a notional, or, when the subscribed depth cannot cover it, an explicit
insufficient-depth result. A price is never fabricated from a short book.

Every result carries the venue's subscribed depth ceiling because fill rates
are only comparable between books with the same cap: Binance.US snapshots are
limited to a fixed number of levels while Coinbase and Gemini stream full
books, so "could not fill" means different things on each.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from arb.orderbook import OrderBookManager
from arb.types import PriceLevel

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class DepthFill:
    """Outcome of walking one side of a book for one quote notional.

    `vwap` is None exactly when `insufficient_depth` is True. `filled_notional`
    and `filled_base` then describe what the book could supply; otherwise
    `filled_notional` equals the requested notional. All arithmetic is decimal.
    """

    notional: Decimal
    vwap: Decimal | None
    filled_notional: Decimal
    filled_base: Decimal
    levels_used: int
    insufficient_depth: bool


def _to_decimal(value: Fraction) -> Decimal:
    """One correctly rounded division; exact whenever the rational terminates."""
    return Decimal(value.numerator) / Decimal(value.denominator)


def walk_levels(levels: Iterable[PriceLevel], notional: Decimal) -> DepthFill:
    """Consume `levels` in the order given until `notional` of quote is spent.

    Callers pass asks ascending for a buy and bids descending for a sell. The
    last level is taken partially so the fill spends exactly `notional`. The
    walk runs in exact rationals and each output is rounded once, so a fill
    that ends inside one level prices at exactly that level and VWAP never
    improves with size because of accumulated rounding.
    """
    if notional <= 0:
        raise ValueError("notional must be positive")
    remaining = Fraction(notional)
    filled_base = Fraction(0)
    levels_used = 0
    for level in levels:
        if level.size <= 0:
            continue
        price = Fraction(level.price)
        level_notional = price * Fraction(level.size)
        levels_used += 1
        if level_notional >= remaining:
            filled_base += remaining / price
            return DepthFill(
                notional=notional,
                vwap=_to_decimal(Fraction(notional) / filled_base),
                filled_notional=notional,
                filled_base=_to_decimal(filled_base),
                levels_used=levels_used,
                insufficient_depth=False,
            )
        remaining -= level_notional
        filled_base += Fraction(level.size)
    return DepthFill(
        notional=notional,
        vwap=None,
        filled_notional=_to_decimal(Fraction(notional) - remaining),
        filled_base=_to_decimal(filled_base),
        levels_used=levels_used,
        insufficient_depth=True,
    )


@dataclass(frozen=True)
class DepthQuote:
    exchange: str
    pair: str
    side: Side
    subscribed_depth_levels: int | None
    fill: DepthFill

    def as_payload(self) -> dict[str, object]:
        fill = self.fill
        return {
            "exchange": self.exchange,
            "pair": self.pair,
            "side": self.side,
            "notional": str(fill.notional),
            "vwap": None if fill.vwap is None else str(fill.vwap),
            "insufficient_depth": fill.insufficient_depth,
            "filled_notional": str(fill.filled_notional),
            "filled_base": str(fill.filled_base),
            "levels_used": fill.levels_used,
            "subscribed_depth_levels": self.subscribed_depth_levels,
        }


def price_book(
    exchange: str,
    pair: str,
    bids: Sequence[PriceLevel],
    asks: Sequence[PriceLevel],
    notionals: Sequence[Decimal],
    subscribed_depth_levels: int | None,
) -> list[DepthQuote]:
    """Quote every notional on both sides of one eligible book."""
    quotes: list[DepthQuote] = []
    for notional in notionals:
        quotes.append(
            DepthQuote(exchange, pair, "buy", subscribed_depth_levels, walk_levels(asks, notional))
        )
        quotes.append(
            DepthQuote(exchange, pair, "sell", subscribed_depth_levels, walk_levels(bids, notional))
        )
    return quotes


FillKey = tuple[str, str, Decimal, Side]


@dataclass
class _FillCounts:
    observations: int = 0
    filled: int = 0


@dataclass
class FillRateTracker:
    """Count how often each (venue, pair, notional, side) could be filled.

    Only eligible books are observed; an ineligible book is counted separately
    so an outage or resync never reads as a depth shortfall. Counts live in
    memory for the process lifetime.
    """

    notionals: tuple[Decimal, ...]
    _counts: dict[FillKey, _FillCounts] = field(default_factory=dict)
    _ineligible: dict[tuple[str, str], int] = field(default_factory=dict)
    _depth_levels: dict[tuple[str, str], int | None] = field(default_factory=dict)

    def observe(self, quotes: Iterable[DepthQuote]) -> None:
        for quote in quotes:
            self._depth_levels[(quote.exchange, quote.pair)] = quote.subscribed_depth_levels
            key: FillKey = (quote.exchange, quote.pair, quote.fill.notional, quote.side)
            counts = self._counts.setdefault(key, _FillCounts())
            counts.observations += 1
            if not quote.fill.insufficient_depth:
                counts.filled += 1

    def observe_ineligible(self, exchange: str, pair: str) -> None:
        self._ineligible[(exchange, pair)] = self._ineligible.get((exchange, pair), 0) + 1

    def rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for (exchange, pair, notional, side), counts in sorted(
            self._counts.items(), key=lambda item: (item[0][0], item[0][1], item[0][2], item[0][3])
        ):
            rows.append(
                {
                    "exchange": exchange,
                    "pair": pair,
                    "notional": str(notional),
                    "side": side,
                    "observations": counts.observations,
                    "filled": counts.filled,
                    # A ratio for display; the exact counts are alongside.
                    "fill_rate": counts.filled / counts.observations,
                    "ineligible_samples": self._ineligible.get((exchange, pair), 0),
                    "subscribed_depth_levels": self._depth_levels.get((exchange, pair)),
                }
            )
        return rows


class DepthSampler:
    """Walk every known book on a schedule and feed the fill-rate tracker.

    Sampling is periodic rather than per book update so the walk, which can
    touch every level of a thin book before declaring insufficient depth,
    never runs on the ingestion path. It also makes fill rates time-weighted
    instead of weighted toward busy books. Replay drives `sample_all` from
    its recorded clock; live runs use `run`.
    """

    def __init__(
        self,
        book_manager: OrderBookManager,
        notionals: Sequence[Decimal],
        depth_levels: Mapping[str, int | None],
        *,
        interval_seconds: float,
    ) -> None:
        self.book_manager = book_manager
        self.notionals = tuple(notionals)
        self.depth_levels = dict(depth_levels)
        self.interval_seconds = interval_seconds
        self.tracker = FillRateTracker(self.notionals)
        self.samples = 0

    def quote(
        self, exchange: str, pair: str, now_monotonic_ns: int | None = None
    ) -> list[DepthQuote]:
        """Current quotes for one book, or an empty list when it is not eligible."""
        sides = self.book_manager.depth_levels(exchange, pair, now_monotonic_ns)
        if sides is None:
            return []
        bids, asks = sides
        return price_book(
            exchange, pair, bids, asks, self.notionals, self.depth_levels.get(exchange)
        )

    def quote_pair(self, pair: str, now_monotonic_ns: int | None = None) -> list[DepthQuote]:
        return [
            quote
            for exchange, known_pair in self.book_manager.known_pairs()
            if known_pair == pair
            for quote in self.quote(exchange, known_pair, now_monotonic_ns)
        ]

    def sample_all(self, now_monotonic_ns: int | None = None) -> None:
        self.samples += 1
        for exchange, pair in self.book_manager.known_pairs():
            quotes = self.quote(exchange, pair, now_monotonic_ns)
            if quotes:
                self.tracker.observe(quotes)
            else:
                self.tracker.observe_ineligible(exchange, pair)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            self.sample_all()
