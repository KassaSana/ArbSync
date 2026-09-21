from __future__ import annotations

import asyncio
import json
import types
from decimal import Decimal

from arb.adapters.coinbase import CoinbaseAdapter
from arb.types import EventKind, MarketEvent, PriceLevel


def test_coinbase_snapshot_parsing() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    payload = '{"type":"snapshot","product_id":"BTC-USD","sequence_num":10,"updates":[{"side":"bid","price_level":"100","new_quantity":"1"},{"side":"offer","price_level":"101","new_quantity":"2"}]}'
    events = asyncio.run(adapter.parse_message(payload))
    assert len(events) == 1
    assert events[0].kind is EventKind.SNAPSHOT
    assert events[0].pair == "BTC-USD"


def test_coinbase_nested_envelope_preserves_outer_sequence_per_product() -> None:
    adapter = CoinbaseAdapter(["BTC-USD", "ETH-USD"])
    payload = (
        '{"channel":"l2_data","sequence_num":42,"events":['
        '{"type":"snapshot","product_id":"BTC-USD","updates":[]},'
        '{"type":"snapshot","product_id":"ETH-USD","updates":[]}]}'
    )
    events = asyncio.run(adapter.parse_message(payload))
    assert [(event.pair, event.sequence) for event in events] == [("BTC-USD", 1), ("ETH-USD", 1)]
    assert [event.exchange_last_sequence for event in events] == [42, 42]


def test_coinbase_combines_multiple_events_for_one_product() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    payload = (
        '{"channel":"l2_data","sequence_num":7,"events":['
        '{"type":"snapshot","product_id":"BTC-USD","updates":['
        '{"side":"bid","price_level":"100","new_quantity":"1"}]},'
        '{"type":"update","product_id":"BTC-USD","updates":['
        '{"side":"offer","price_level":"101","new_quantity":"2"}]}]}'
    )

    events = asyncio.run(adapter.parse_message(payload))

    assert len(events) == 1
    assert events[0].kind is EventKind.SNAPSHOT
    assert events[0].sequence == 1
    assert events[0].exchange_last_sequence == 7
    assert events[0].bids == (PriceLevel(Decimal("100"), Decimal("1")),)
    assert events[0].asks == (PriceLevel(Decimal("101"), Decimal("2")),)


def test_coinbase_tracks_product_sequences_independently() -> None:
    adapter = CoinbaseAdapter(["BTC-USD", "ETH-USD"])
    snapshots = (
        '{"channel":"l2_data","sequence_num":10,"events":['
        '{"type":"snapshot","product_id":"BTC-USD","updates":[]},'
        '{"type":"snapshot","product_id":"ETH-USD","updates":[]}]}'
    )
    updates = (
        '{"channel":"l2_data","sequence_num":11,"events":['
        '{"type":"update","product_id":"BTC-USD","updates":[]},'
        '{"type":"update","product_id":"ETH-USD","updates":[]}]}'
    )

    asyncio.run(adapter.parse_message(snapshots))
    events = asyncio.run(adapter.parse_message(updates))

    assert [(event.pair, event.sequence) for event in events] == [
        ("BTC-USD", 2),
        ("ETH-USD", 2),
    ]


def test_coinbase_envelope_is_atomic_when_one_product_has_a_gap() -> None:
    adapter = CoinbaseAdapter(["BTC-USD", "ETH-USD"])
    asyncio.run(
        adapter.parse_message(
            '{"channel":"l2_data","sequence_num":10,"events":['
            '{"type":"snapshot","product_id":"BTC-USD","updates":[]},'
            '{"type":"snapshot","product_id":"ETH-USD","updates":[]}]}'
        )
    )
    events = asyncio.run(
        adapter.parse_message(
            '{"channel":"l2_data","sequence_num":12,"events":['
            '{"type":"update","product_id":"BTC-USD","updates":[]},'
            '{"type":"update","product_id":"ETH-USD","updates":[]}]}'
        )
    )

    assert events == []
    assert adapter._last_sequence_by_pair["BTC-USD"] == 1
    assert adapter._last_sequence_by_pair["ETH-USD"] == 1
    assert adapter.gap_count == 1
    assert adapter._reconnect_requested is True


def test_coinbase_non_level2_envelopes_advance_connection_sequence() -> None:
    adapter = CoinbaseAdapter(["BTC-USD", "ETH-USD"])
    btc = (
        '{"channel":"l2_data","sequence_num":0,"events":['
        '{"type":"snapshot","product_id":"BTC-USD","updates":[]}]}'
    )
    subscription = (
        '{"channel":"subscriptions","sequence_num":1,"events":['
        '{"subscriptions":{"level2":["BTC-USD","ETH-USD"]}}]}'
    )
    eth = (
        '{"channel":"l2_data","sequence_num":2,"events":['
        '{"type":"snapshot","product_id":"ETH-USD","updates":[]}]}'
    )

    assert len(asyncio.run(adapter.parse_message(btc))) == 1
    assert asyncio.run(adapter.parse_message(subscription)) == []
    events = asyncio.run(adapter.parse_message(eth))

    assert len(events) == 1
    assert events[0].pair == "ETH-USD"
    assert events[0].sequence == 1
    assert events[0].exchange_last_sequence == 2
    assert adapter.gap_count == 0


def test_coinbase_interleaved_products_keep_local_sequences_contiguous() -> None:
    adapter = CoinbaseAdapter(["BTC-USD", "ETH-USD"])
    messages = [
        '{"channel":"l2_data","sequence_num":0,"events":[{"type":"snapshot","product_id":"BTC-USD","updates":[]}]}',
        '{"channel":"l2_data","sequence_num":1,"events":[{"type":"update","product_id":"BTC-USD","updates":[]}]}',
        '{"channel":"l2_data","sequence_num":2,"events":[{"type":"snapshot","product_id":"ETH-USD","updates":[]}]}',
        '{"channel":"subscriptions","sequence_num":3,"events":[{"subscriptions":{}}]}',
        '{"channel":"l2_data","sequence_num":4,"events":[{"type":"update","product_id":"BTC-USD","updates":[]}]}',
    ]

    events = [
        event for message in messages for event in asyncio.run(adapter.parse_message(message))
    ]

    assert [(event.pair, event.sequence, event.exchange_last_sequence) for event in events] == [
        ("BTC-USD", 1, 0),
        ("BTC-USD", 2, 1),
        ("ETH-USD", 1, 2),
        ("BTC-USD", 3, 4),
    ]


def test_coinbase_missing_envelope_sequence_requests_reconnect() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    events = asyncio.run(
        adapter.parse_message(
            '{"channel":"l2_data","events":['
            '{"type":"snapshot","product_id":"BTC-USD","updates":[]}]}'
        )
    )
    assert events == []
    assert adapter._reconnect_requested is True


def test_coinbase_malformed_event_order_requests_reconnect() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    events = asyncio.run(
        adapter.parse_message(
            '{"channel":"l2_data","sequence_num":1,"events":['
            '{"type":"update","product_id":"BTC-USD","updates":[]},'
            '{"type":"snapshot","product_id":"BTC-USD","updates":[]}]}'
        )
    )
    assert events == []
    assert adapter._reconnect_requested is True


def test_coinbase_subscribes_to_level2_and_heartbeats() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    sent: list[str] = []

    class Socket:
        async def send(self, message: str) -> None:
            sent.append(message)

    asyncio.run(adapter.subscribe(Socket()))

    assert [json.loads(message)["channel"] for message in sent] == ["level2", "heartbeats"]


def _stub_snapshot(adapter: CoinbaseAdapter, sequence: int = 1) -> None:
    async def fetch_snapshot(
        self: CoinbaseAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=sequence,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
            exchange_last_sequence=sequence,
        )

    adapter.fetch_snapshot = types.MethodType(fetch_snapshot, adapter)  # type: ignore[method-assign]


def test_coinbase_gap_waits_for_stream_snapshot_without_using_rest_sequence() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    # First message establishes a sequence baseline.
    asyncio.run(
        adapter.parse_message(
            '{"type":"snapshot","product_id":"BTC-USD","sequence_num":1,"updates":[{"side":"bid","price_level":"100","new_quantity":"1"}]}'
        )
    )
    # Skipping sequence 2 invalidates the stream; REST sequence numbers must not
    # be used to pretend the Advanced Trade stream is synchronized again.
    events = asyncio.run(
        adapter.parse_message(
            '{"type":"update","product_id":"BTC-USD","sequence_num":3,"updates":[{"side":"bid","price_level":"99","new_quantity":"1"}]}'
        )
    )
    assert events == []
    assert adapter.gap_count == 1
    assert adapter._reconnect_requested is True
    asyncio.run(adapter.reset_state())
    assert adapter._reconnect_requested is False
    events = asyncio.run(
        adapter.parse_message(
            '{"type":"snapshot","product_id":"BTC-USD","sequence_num":10,"updates":[]}'
        )
    )
    assert len(events) == 1
    assert events[0].kind is EventKind.SNAPSHOT


def test_coinbase_update_before_stream_snapshot_is_ignored() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    events = asyncio.run(
        adapter.parse_message(
            '{"type":"update","product_id":"BTC-USD","sequence_num":1,"updates":[]}'
        )
    )
    assert events == []
    assert adapter.gap_count == 0
    assert adapter._reconnect_requested is True
    asyncio.run(adapter.reset_state())
    snapshot = asyncio.run(
        adapter.parse_message(
            '{"type":"snapshot","product_id":"BTC-USD","sequence_num":2,"updates":[]}'
        )
    )
    assert snapshot[0].kind is EventKind.SNAPSHOT


def test_coinbase_delta_parses_offer_to_ask() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])
    _stub_snapshot(adapter)
    asyncio.run(
        adapter.parse_message(
            '{"type":"snapshot","product_id":"BTC-USD","sequence_num":1,"updates":[]}'
        )
    )
    events = asyncio.run(
        adapter.parse_message(
            '{"type":"update","product_id":"BTC-USD","sequence_num":2,"updates":[{"side":"offer","price_level":"105","new_quantity":"3"}]}'
        )
    )
    assert len(events) == 1
    assert events[0].kind is EventKind.DELTA
    assert events[0].asks == (PriceLevel(price=Decimal("105"), size=Decimal("3")),)
    assert events[0].bids == ()
