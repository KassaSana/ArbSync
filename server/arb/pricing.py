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
from arb.types import PriceLevel, PricingLedger, TopOfBook

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


@dataclass(frozen=True)
class ExecutableRoutePrice:
    """Depth- and fee-aware price for one directed cross-venue route."""

    pair: str
    buy_exchange: str
    sell_exchange: str
    ledger: PricingLedger

    def as_payload(self) -> dict[str, object]:
        return {
            "pair": self.pair,
            "buy_exchange": self.buy_exchange,
            "sell_exchange": self.sell_exchange,
            **self.ledger.as_payload(),
        }


def pricing_ledger(
    *,
    notional: Decimal,
    buy_top: TopOfBook,
    sell_top: TopOfBook,
    buy_fill: DepthFill,
    sell_fill: DepthFill,
    buy_taker_fee_pct: Decimal,
    sell_taker_fee_pct: Decimal,
) -> PricingLedger:
    """Build the explicit top-of-book -> depth -> fees -> net ledger."""
    top_spread = (sell_top.best_bid_price - buy_top.best_ask_price) / buy_top.best_ask_price * 100
    if buy_fill.vwap is None or sell_fill.vwap is None:
        return PricingLedger(
            notional=notional,
            top_of_book_spread_pct=top_spread,
            buy_vwap=buy_fill.vwap,
            sell_vwap=sell_fill.vwap,
            gross_executable_spread_pct=None,
            depth_impact_pct=None,
            buy_taker_fee_pct=buy_taker_fee_pct,
            sell_taker_fee_pct=sell_taker_fee_pct,
            fee_impact_pct=None,
            net_executable_spread_pct=None,
            insufficient_depth=True,
        )
    gross = (sell_fill.vwap - buy_fill.vwap) / buy_fill.vwap * 100
    net = (
        (
            sell_fill.vwap * (Decimal(1) - sell_taker_fee_pct / 100)
            - buy_fill.vwap * (Decimal(1) + buy_taker_fee_pct / 100)
        )
        / buy_fill.vwap
        * 100
    )
    return PricingLedger(
        notional=notional,
        top_of_book_spread_pct=top_spread,
        buy_vwap=buy_fill.vwap,
        sell_vwap=sell_fill.vwap,
        gross_executable_spread_pct=gross,
        depth_impact_pct=gross - top_spread,
        buy_taker_fee_pct=buy_taker_fee_pct,
        sell_taker_fee_pct=sell_taker_fee_pct,
        fee_impact_pct=net - gross,
        net_executable_spread_pct=net,
        insufficient_depth=False,
    )


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

    Fill-rate sampling is periodic rather than per book update, which makes it
    time-weighted instead of weighted toward busy books. `route_prices` is also
    used only when an episode opens or reaches a new peak, not on every update,
    to snapshot that episode's product ledger. Replay drives `sample_all` from
    its recorded clock; live runs use `run`.
    """

    def __init__(
        self,
        book_manager: OrderBookManager,
        notionals: Sequence[Decimal],
        depth_levels: Mapping[str, int | None],
        taker_fees_pct: Mapping[str, Decimal],
        *,
        interval_seconds: float,
    ) -> None:
        self.book_manager = book_manager
        self.notionals = tuple(notionals)
        self.depth_levels = dict(depth_levels)
        self.taker_fees_pct = dict(taker_fees_pct)
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

    def route_prices(
        self, pair: str, now_monotonic_ns: int | None = None
    ) -> list[ExecutableRoutePrice]:
        """Price every directed eligible route at each configured notional."""
        by_exchange: dict[str, dict[tuple[Decimal, Side], DepthQuote]] = {}
        tops: dict[str, TopOfBook] = {}
        for exchange, known_pair in self.book_manager.known_pairs():
            if known_pair != pair:
                continue
            quotes = self.quote(exchange, pair, now_monotonic_ns)
            top = self.book_manager.top_of_book(exchange, pair)
            if quotes and top is not None:
                by_exchange[exchange] = {(q.fill.notional, q.side): q for q in quotes}
                tops[exchange] = top
        results: list[ExecutableRoutePrice] = []
        for buy_exchange in sorted(by_exchange):
            for sell_exchange in sorted(by_exchange):
                if buy_exchange == sell_exchange:
                    continue
                if (
                    buy_exchange not in self.taker_fees_pct
                    or sell_exchange not in self.taker_fees_pct
                ):
                    continue
                for notional in self.notionals:
                    buy = by_exchange[buy_exchange][(notional, "buy")]
                    sell = by_exchange[sell_exchange][(notional, "sell")]
                    results.append(
                        ExecutableRoutePrice(
                            pair=pair,
                            buy_exchange=buy_exchange,
                            sell_exchange=sell_exchange,
                            ledger=pricing_ledger(
                                notional=notional,
                                buy_top=tops[buy_exchange],
                                sell_top=tops[sell_exchange],
                                buy_fill=buy.fill,
                                sell_fill=sell.fill,
                                buy_taker_fee_pct=self.taker_fees_pct[buy_exchange],
                                sell_taker_fee_pct=self.taker_fees_pct[sell_exchange],
                            ),
                        )
                    )
        return results

    def ledgers_for_route(
        self,
        pair: str,
        buy_exchange: str,
        sell_exchange: str,
        now_monotonic_ns: int | None = None,
    ) -> tuple[PricingLedger, ...]:
        return tuple(
            result.ledger
            for result in self.route_prices(pair, now_monotonic_ns)
            if result.buy_exchange == buy_exchange and result.sell_exchange == sell_exchange
        )

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
