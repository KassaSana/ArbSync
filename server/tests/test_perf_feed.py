from __future__ import annotations

import json
from decimal import Decimal

import pytest
from arb.adapters.binance import BinanceAdapter
from arb.adapters.coinbase import CoinbaseAdapter
from arb.adapters.gemini import GeminiAdapter
from arb.detector import ArbitrageDetector
from arb.orderbook import OrderBookManager
from perf_feed import ASSETS, EXCHANGES, Feed, levels


@pytest.mark.asyncio
async def test_burst_workload_keeps_all_books_continuous_and_exercises_opportunities():
    feed = Feed(depth=20)
    adapters = {
        "gemini": GeminiAdapter([f"{a.lower()}usd" for a in ASSETS]),
        "coinbase": CoinbaseAdapter([f"{a}-USD" for a in ASSETS]),
        "binance": BinanceAdapter([f"{a}USDT" for a in ASSETS]),
    }
    manager = OrderBookManager()
    for exchange, adapter in adapters.items():
        for asset in ASSETS:
            message, _ = feed.message(exchange, asset, snapshot=True)
            if exchange == "binance":
                bids, asks = levels(exchange, asset, feed.depth)
                message = json.dumps(
                    {"symbol": f"{asset}USDT", "lastUpdateId": 1, "bids": bids, "asks": asks}
                )
            for event in await adapter.parse_message(message):
                assert manager.apply(event).accepted

    detector = ArbitrageDetector(Decimal("0.1"))
    emitted = 0
    for counter in range(2500):
        exchange = EXCHANGES[counter % 3]
        asset = ASSETS[(counter // 3) % len(ASSETS)]
        message, _ = feed.message(exchange, asset)
        events = await adapters[exchange].parse_message(message)
        assert len(events) == 1
        for event in events:
            assert manager.apply(event).accepted
            books = manager.eligible_books(event.pair, EXCHANGES)
            emitted += len(detector.detect_for_pair(event.pair, books, 0))
    assert emitted > 0
    assert len(manager.known_pairs()) == 27
    assert all(manager.eligibility(e, p).eligible for e, p in manager.known_pairs())
    assert all(adapter.gap_count == 0 for adapter in adapters.values())
