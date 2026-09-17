from __future__ import annotations

import asyncio
import types
from decimal import Decimal

import pytest
from arb.adapters.binance import BinanceAdapter, normalize_binance_symbol
from arb.types import EventKind, MarketEvent, PriceLevel


class ControlledSocket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self) -> ControlledSocket:
        return self

    async def __anext__(self) -> str:
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def push(self, message: str) -> None:
        await self.messages.put(message)


def binance_snapshot(sequence: int) -> MarketEvent:
    return MarketEvent(
        exchange="binance",
        pair="BTC-USDT",
        kind=EventKind.SNAPSHOT,
        sequence=sequence,
        timestamp_ns=1,
        bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
        asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        exchange_last_sequence=sequence,
        received_monotonic_ns=1,
    )


def test_binance_depth_update_parsing() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    asyncio.run(
        adapter.parse_message(
            '{"symbol":"BTCUSDT","lastUpdateId":14,"bids":[["99","1"]],"asks":[["101","2"]]}'
        )
    )
    payload = '{"s":"BTCUSDT","U":15,"u":15,"E":123,"b":[["100","1"]],"a":[["101","2"]]}'
    events = asyncio.run(adapter.parse_message(payload))
    assert len(events) == 1
    assert events[0].kind is EventKind.DELTA
    assert events[0].pair == "BTC-USDT"


def test_binance_subsequent_deltas_are_sequential() -> None:
    # Binance exchange IDs may advance by a range, while local event sequences
    # remain contiguous for OrderBookManager.
    adapter = BinanceAdapter(["BTCUSDT"])
    asyncio.run(
        adapter.parse_message(
            '{"symbol":"BTCUSDT","lastUpdateId":100,"bids":[["100","1"]],"asks":[["101","2"]]}'
        )
    )
    e1 = asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":105,"U":101,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    e2 = asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":120,"U":106,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    assert e1[0].kind is EventKind.DELTA
    assert e2[0].kind is EventKind.DELTA
    assert e2[0].sequence == e1[0].sequence + 1
    assert e1[0].exchange_first_sequence == 101
    assert e1[0].exchange_last_sequence == 105


def _stub_snapshot(adapter: object, sequence: int = 1) -> None:
    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
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

    adapter.fetch_snapshot = types.MethodType(fetch_snapshot, adapter)


def test_binance_subsequent_messages_dont_trigger_rest_calls() -> None:
    # Once aligned, exchange update ranges advance without another REST call.
    adapter = BinanceAdapter(["BTCUSDT"])
    fetch_calls = 0

    async def counting_fetch(self: BinanceAdapter, pair: str, trigger_sequence: int) -> MarketEvent:
        nonlocal fetch_calls
        fetch_calls += 1
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=100,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        )

    adapter.fetch_snapshot = types.MethodType(counting_fetch, adapter)
    asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":101,"U":100,"E":1,"b":[["100","1"]],"a":[]}')
    )
    asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":120,"U":102,"E":1,"b":[["100","1"]],"a":[]}')
    )
    asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":300,"U":121,"E":1,"b":[["100","1"]],"a":[]}')
    )
    assert fetch_calls == 1


def test_first_delta_with_no_prior_baseline_triggers_snapshot() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    _stub_snapshot(adapter)
    # No snapshot yet — the first delta is buffered while REST snapshot state is fetched.
    events = asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":7,"U":1,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    assert len(events) == 2
    assert events[0].kind is EventKind.SNAPSHOT
    assert events[1].kind is EventKind.DELTA


def test_sequential_deltas_pass_through_without_snapshot() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    _stub_snapshot(adapter)
    asyncio.run(
        adapter.parse_message(
            '{"symbol":"BTCUSDT","lastUpdateId":1,"bids":[["100","1"]],"asks":[["101","2"]]}'
        )
    )
    e2 = asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":2,"U":2,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    e3 = asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":3,"U":3,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    assert e2[0].kind is EventKind.DELTA
    assert e3[0].kind is EventKind.DELTA
    assert e3[0].sequence == e2[0].sequence + 1
    assert adapter.gap_count == 0


def test_binance_reset_state_forces_resnapshot_after_reconnect() -> None:
    # Regression: previously _initialized persisted across reconnects, so the
    # first message after a reconnect skipped the REST snapshot and emitted a
    # delta on top of a stale book. reset_state() must clear that state.
    adapter = BinanceAdapter(["BTCUSDT"])
    _stub_snapshot(adapter, sequence=2)
    asyncio.run(
        adapter.parse_message(
            '{"symbol":"BTCUSDT","lastUpdateId":1,"bids":[["100","1"]],"asks":[["101","2"]]}'
        )
    )
    asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":2,"U":2,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )

    asyncio.run(adapter.reset_state())

    events = asyncio.run(
        adapter.parse_message(
            '{"s":"BTCUSDT","u":3,"U":2,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    assert events[0].kind is EventKind.SNAPSHOT


def test_binance_initial_buffer_aligns_snapshot_and_discards_covered_updates() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    snapshots = 0

    async def snapshot_at_100(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        nonlocal snapshots
        snapshots += 1
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=100,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
            exchange_last_sequence=100,
        )

    adapter.fetch_snapshot = types.MethodType(snapshot_at_100, adapter)
    first = asyncio.run(adapter.parse_message('{"s":"BTCUSDT","u":99,"U":95,"E":1,"b":[],"a":[]}'))
    assert [event.kind for event in first] == [EventKind.SNAPSHOT]
    second = asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":105,"U":100,"E":1,"b":[["100","2"]],"a":[]}')
    )
    assert snapshots == 1
    assert [event.kind for event in second] == [EventKind.DELTA]
    assert second[0].exchange_first_sequence == 100


def test_binance_gap_requests_reconnect_and_does_not_emit_delta() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    asyncio.run(
        adapter.parse_message(
            '{"symbol":"BTCUSDT","lastUpdateId":100,"bids":[["100","1"]],"asks":[["101","1"]]}'
        )
    )
    events = asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":110,"U":108,"E":1,"b":[],"a":[]}')
    )
    assert events == []
    assert adapter.gap_count == 1
    assert adapter._reconnect_requested is True


@pytest.mark.asyncio
async def test_binance_buffers_updates_while_snapshot_is_in_flight() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    socket = ControlledSocket()
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def delayed_snapshot(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        snapshot_started.set()
        await release_snapshot.wait()
        return binance_snapshot(100)

    adapter.fetch_snapshot = types.MethodType(delayed_snapshot, adapter)
    await socket.push('{"s":"BTCUSDT","U":95,"u":99,"b":[],"a":[]}')
    stream = adapter.stream_events(socket)
    first_event = asyncio.create_task(anext(stream))
    await snapshot_started.wait()
    await socket.push('{"s":"BTCUSDT","U":99,"u":105,"b":[["100","2"]],"a":[]}')

    for _ in range(10):
        if len(adapter._buffers["BTC-USDT"]) == 2:
            break
        await asyncio.sleep(0)
    assert len(adapter._buffers["BTC-USDT"]) == 2

    release_snapshot.set()
    snapshot = await first_event
    delta = await anext(stream)
    await stream.aclose()

    assert snapshot.kind is EventKind.SNAPSHOT
    assert delta.kind is EventKind.DELTA
    assert delta.exchange_first_sequence == 99
    assert delta.exchange_last_sequence == 105
    assert delta.received_monotonic_ns is not None


@pytest.mark.asyncio
async def test_binance_stops_before_buffered_delta_after_reconnect_request() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    socket = ControlledSocket()

    async def snapshot_at_100(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        return binance_snapshot(100)

    adapter.fetch_snapshot = types.MethodType(snapshot_at_100, adapter)
    await socket.push('{"s":"BTCUSDT","U":99,"u":105,"b":[["100","2"]],"a":[["101","1"]]}')
    stream = adapter.stream_events(socket)

    snapshot = await anext(stream)
    adapter.request_reconnect()

    assert snapshot.kind is EventKind.SNAPSHOT
    with pytest.raises(RuntimeError, match="adapter requested reconnect"):
        await anext(stream)


@pytest.mark.asyncio
async def test_binance_buffer_overflow_aborts_synchronization() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    adapter.max_buffered_updates = 2
    socket = ControlledSocket()
    snapshot_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_snapshot(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        snapshot_started.set()
        await never_release.wait()
        return binance_snapshot(1)

    adapter.fetch_snapshot = types.MethodType(blocked_snapshot, adapter)
    stream = adapter.stream_events(socket)
    pending = asyncio.create_task(anext(stream))
    await socket.push('{"s":"BTCUSDT","U":1,"u":1,"b":[],"a":[]}')
    await snapshot_started.wait()
    await socket.push('{"s":"BTCUSDT","U":2,"u":2,"b":[],"a":[]}')
    await socket.push('{"s":"BTCUSDT","U":3,"u":3,"b":[],"a":[]}')

    with pytest.raises(RuntimeError, match="requested reconnect"):
        await pending
    assert adapter._reconnect_requested is True
    assert "BTC-USDT" not in adapter._buffers


@pytest.mark.asyncio
async def test_binance_snapshot_failure_aborts_synchronization() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    socket = ControlledSocket()

    async def failed_snapshot(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        raise OSError("REST unavailable")

    adapter.fetch_snapshot = types.MethodType(failed_snapshot, adapter)
    await socket.push('{"s":"BTCUSDT","U":1,"u":1,"b":[],"a":[]}')
    stream = adapter.stream_events(socket)
    with pytest.raises(RuntimeError, match="snapshot retrieval failed"):
        await anext(stream)
    assert adapter._reconnect_requested is True


@pytest.mark.asyncio
async def test_binance_reconnects_when_snapshot_never_catches_up() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    socket = ControlledSocket()
    calls = 0

    async def stale_snapshot(self: BinanceAdapter, pair: str, trigger_sequence: int) -> MarketEvent:
        nonlocal calls
        calls += 1
        return binance_snapshot(90)

    adapter.fetch_snapshot = types.MethodType(stale_snapshot, adapter)
    await socket.push('{"s":"BTCUSDT","U":95,"u":105,"b":[],"a":[]}')
    stream = adapter.stream_events(socket)

    with pytest.raises(RuntimeError, match="snapshot did not catch up"):
        await anext(stream)
    assert calls == 3
    assert adapter._reconnect_requested is True


@pytest.mark.asyncio
async def test_binance_server_shutdown_requests_reconnect() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    socket = ControlledSocket()
    await socket.push('{"e":"serverShutdown"}')
    stream = adapter.stream_events(socket)

    with pytest.raises(RuntimeError, match="requested reconnect"):
        await anext(stream)
    assert adapter._reconnect_requested is True


def test_binance_initial_update_must_span_snapshot_id() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    adapter._buffer(
        adapter._decode_depth_update({"s": "BTCUSDT", "U": 95, "u": 99, "b": [], "a": []})
    )
    adapter._buffer(
        adapter._decode_depth_update({"s": "BTCUSDT", "U": 101, "u": 105, "b": [], "a": []})
    )

    events = adapter._align_snapshot("BTC-USDT", binance_snapshot(100))

    assert events == []
    assert adapter.gap_count == 1
    assert adapter._reconnect_requested is True


def test_binance_retries_snapshot_that_predates_first_buffered_update() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    sequences = iter((90, 100))

    async def advancing_snapshot(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        return binance_snapshot(next(sequences))

    adapter.fetch_snapshot = types.MethodType(advancing_snapshot, adapter)
    events = asyncio.run(adapter.parse_message('{"s":"BTCUSDT","U":95,"u":105,"b":[],"a":[]}'))
    assert [event.kind for event in events] == [EventKind.SNAPSHOT, EventKind.DELTA]
    assert events[0].sequence == 100


def test_binance_snapshot_requests_documented_depth_limit() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    requested_url = ""

    async def get_json(url: str) -> dict[str, object]:
        nonlocal requested_url
        requested_url = url
        return {"lastUpdateId": 1, "bids": [], "asks": []}

    adapter.client_get_json = get_json  # type: ignore[method-assign]
    asyncio.run(adapter.fetch_snapshot("BTC-USDT", trigger_sequence=0))
    assert requested_url.endswith("symbol=BTCUSDT&limit=5000")


def test_binance_symbol_normalization_separates_usd_and_usdt() -> None:
    assert normalize_binance_symbol("ethusdt") == "ETH-USDT"
    assert normalize_binance_symbol("BTCUSDT") == "BTC-USDT"
    # Binance.US lists USD-quoted markets; they must land on the same canonical
    # pair as Coinbase and Gemini so cross-venue detection can see them.
    assert normalize_binance_symbol("btcusd") == "BTC-USD"
    assert normalize_binance_symbol("AAVEUSD") == "AAVE-USD"
    # Stablecoin quotes are distinct markets and must not be mistaken for USD.
    assert normalize_binance_symbol("btcbusd") == "BTC-BUSD"
    assert normalize_binance_symbol("ETHUSDC") == "ETH-USDC"
    # Unknown quote assets pass through uppercased.
    assert normalize_binance_symbol("btceth") == "BTCETH"
