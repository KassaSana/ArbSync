from __future__ import annotations

from decimal import Decimal
from itertools import permutations

from arb.types import ArbitrageOpportunity, TopOfBook


class ArbitrageDetector:
    def __init__(self, threshold_pct: Decimal) -> None:
        self.threshold_pct = threshold_pct

    def detect_for_pair(
        self, pair: str, books: list[TopOfBook], timestamp_ns: int
    ) -> list[ArbitrageOpportunity]:
        opportunities: list[ArbitrageOpportunity] = []
        base_asset, separator, quote_asset = pair.rpartition("-")
        if not base_asset or not separator or not quote_asset:
            return opportunities
        books = [book for book in books if book.pair == pair]
        if len(books) < 2:
            return opportunities

        for buy_book, sell_book in permutations(books, 2):
            if sell_book.best_bid_price <= buy_book.best_ask_price:
                continue

            spread_pct = (
                (sell_book.best_bid_price - buy_book.best_ask_price) / buy_book.best_ask_price
            ) * Decimal("100")
            if spread_pct < self.threshold_pct:
                continue

            max_size = min(buy_book.best_ask_size, sell_book.best_bid_size)
            theoretical_profit = max_size * (sell_book.best_bid_price - buy_book.best_ask_price)
            opportunities.append(
                ArbitrageOpportunity(
                    timestamp_ns=timestamp_ns,
                    pair=pair,
                    quote_asset=quote_asset,
                    buy_exchange=buy_book.exchange,
                    sell_exchange=sell_book.exchange,
                    buy_price=buy_book.best_ask_price,
                    sell_price=sell_book.best_bid_price,
                    spread_pct=spread_pct,
                    max_size=max_size,
                    theoretical_profit=theoretical_profit,
                )
            )
        return opportunities
