from __future__ import annotations

import asyncio
from typing import Any

import pytest
import websockets
from arb.adapters.base import ExchangeAdapter
from arb.adapters.binance import BinanceAdapter, normalize_binance_symbol
from arb.adapters.coinbase import CoinbaseAdapter, normalize_coinbase_symbol
from arb.adapters.gemini import GeminiAdapter, normalize_gemini_symbol
from arb.types import EventKind, MarketEvent


class ReceiptTimeAdapter(ExchangeAdapter):
    name = "receipt-test"
    ws_url = "wss://example.test"
    snapshot_url = "https://example.test"

    async def subscribe(self, websocket: Any) -> None:
        return None

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return [
            MarketEvent(
                exchange=self.name,
                pair="BTC-USD",
                kind=EventKind.SNAPSHOT,
                sequence=1,
                timestamp_ns=1,
            )
        ]

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        raise AssertionError("not used")


class OneMessageSocket:
    async def __aenter__(self) -> OneMessageSocket:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def __aiter__(self):
        yield "{}"


def test_symbol_normalization() -> None:
    assert normalize_gemini_symbol("btcusd") == "BTC-USD"
    assert normalize_gemini_symbol("btcusdt") == "BTC-USDT"
    assert normalize_coinbase_symbol("BTC-USD") == "BTC-USD"
    assert normalize_binance_symbol("BTCUSDT") == "BTC-USDT"


@pytest.mark.asyncio
async def test_connect_stamps_receipt_time_before_parsing(monkeypatch) -> None:
    adapter = ReceiptTimeAdapter(["BTC-USD"])
    socket = OneMessageSocket()
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: socket)
    monkeypatch.setattr("arb.adapters.base.time.monotonic_ns", lambda: 123)

    events = adapter.connect()
    event = await anext(events)
    await events.aclose()

    assert event.received_monotonic_ns == 123


def test_status_snapshot_reports_age_and_counters() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    adapter.connected = True
    adapter.reconnect_count = 4
    adapter.gap_count = 2
    adapter.last_error = "boom"
    adapter.last_message_ns = 1_000_000_000
    status = adapter.status_snapshot(now_ns=2_500_000_000)
    assert status.exchange == "binance"
    assert status.connected is True
    assert status.last_message_age_ms == 1_500
    assert status.gap_count == 2
    assert status.reconnect_count == 4
    assert status.last_error == "boom"


def test_status_snapshot_age_is_none_before_first_message() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    status = adapter.status_snapshot(now_ns=1_000_000_000)
    assert status.last_message_age_ms is None
    assert status.connected is False


def test_unrecognized_message_returns_no_events() -> None:
    adapter = GeminiAdapter(["btcusd"])
    assert asyncio.run(adapter.parse_message('{"type":"heartbeat"}')) == []
    coinbase = CoinbaseAdapter(["BTC-USD"])
    assert asyncio.run(coinbase.parse_message('{"type":"subscriptions"}')) == []
    binance = BinanceAdapter(["BTCUSDT"])
    assert asyncio.run(binance.parse_message('{"result":null,"id":1}')) == []
