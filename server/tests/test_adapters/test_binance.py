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


def test_binance_gap_resyncs_pair_without_requesting_reconnect() -> None:
    # A sequence gap demotes only the affected pair: no full-venue reconnect
    # is requested, the triggering update is buffered, and the next update
    # for the pair re-aligns it from a fresh REST snapshot.
    from arb.metrics import adapter_pair_resyncs_total

    def pair_resyncs() -> float:
        return adapter_pair_resyncs_total.labels(
            exchange="binance", trigger="sequence_gap"
        )._value.get()

    adapter = BinanceAdapter(["BTCUSDT"])
    asyncio.run(
        adapter.parse_message(
            '{"symbol":"BTCUSDT","lastUpdateId":100,"bids":[["100","1"]],"asks":[["101","1"]]}'
        )
    )
    _stub_snapshot(adapter, sequence=109)
    before = pair_resyncs()
    events = asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":110,"U":108,"E":1,"b":[],"a":[]}')
    )
    assert events == []
    assert adapter.gap_count == 1
    assert adapter._reconnect_requested is False
    assert "BTC-USDT" not in adapter._initialized
    assert pair_resyncs() - before == 1

    resumed = asyncio.run(
        adapter.parse_message('{"s":"BTCUSDT","u":115,"U":111,"E":1,"b":[],"a":[]}')
    )
    assert [event.kind for event in resumed] == [
        EventKind.SNAPSHOT,
        EventKind.DELTA,
        EventKind.DELTA,
    ]
    assert adapter._reconnect_requested is False
    assert "BTC-USDT" in adapter._initialized


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
async def test_binance_single_pair_gap_keeps_other_pair_flowing() -> None:
    # ARB-028: a sequence gap on one Binance.US pair re-fetches only that
    # pair. The untouched pair's deltas keep flowing with unbroken local
    # sequence and no full-venue reconnect is requested.
    from arb.orderbook import OrderBookManager

    adapter = BinanceAdapter(["BTCUSDT", "ETHUSDT"])
    btc_snapshot = await adapter.parse_message(
        '{"symbol":"BTCUSDT","lastUpdateId":100,"bids":[["100","1"]],"asks":[["101","1"]]}'
    )
    eth_snapshot = await adapter.parse_message(
        '{"symbol":"ETHUSDT","lastUpdateId":50,"bids":[["200","1"]],"asks":[["201","1"]]}'
    )

    async def fetch_btc_snapshot(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
        assert pair == "BTC-USDT"
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=111,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
            exchange_last_sequence=111,
        )

    adapter.fetch_snapshot = types.MethodType(fetch_btc_snapshot, adapter)
    socket = ControlledSocket()
    await socket.push('{"s":"BTCUSDT","U":110,"u":112,"E":1,"b":[["100","1"]],"a":[["101","1"]]}')
    await socket.push('{"s":"ETHUSDT","U":51,"u":51,"E":1,"b":[["200","1"]],"a":[["201","1"]]}')
    stream = adapter.stream_events(socket)

    try:
        events = [await anext(stream) for _ in range(3)]
    finally:
        await stream.aclose()

    by_pair: dict[str, list[MarketEvent]] = {}
    for event in events:
        by_pair.setdefault(event.pair, []).append(event)
    assert [event.kind for event in by_pair["ETH-USDT"]] == [EventKind.DELTA]
    assert [event.kind for event in by_pair["BTC-USDT"]] == [
        EventKind.SNAPSHOT,
        EventKind.DELTA,
    ]
    # The untouched pair advances by exactly one local sequence number.
    assert by_pair["ETH-USDT"][0].sequence == 51
    assert adapter._reconnect_requested is False
    assert adapter.gap_count == 1

    manager = OrderBookManager()
    for event in [*btc_snapshot, *eth_snapshot, *events]:
        manager.apply(event)
    assert manager.eligibility("binance", "ETH-USDT").eligible is True
    assert manager.eligibility("binance", "BTC-USDT").eligible is True


def test_single_pair_resync_keeps_venue_eligible_in_replay() -> None:
    # ARB-028: replay a two-pair Binance.US capture with a sequence gap on
    # one pair. The gapped pair re-aligns from REST, the other pair's deltas
    # stay accepted throughout, and no full-venue resync is ever requested.
    import time

    from arb.capture import CaptureFrame, CaptureHeader
    from arb.orderbook import OrderBookManager
    from arb.replay import replay_frames

    base_mono = time.monotonic_ns()
    base_wall = time.time_ns()

    def ws_frame(index: int, raw: str) -> CaptureFrame:
        return CaptureFrame(
            exchange="binance",
            kind="ws",
            wall_ns=base_wall + index * 100_000_000,
            mono_ns=base_mono + index * 100_000_000,
            raw=raw,
            payload=None,
            url=None,
            events=(),
        )

    def rest_frame(index: int, symbol: str, last_id: int, bid: str, ask: str) -> CaptureFrame:
        return CaptureFrame(
            exchange="binance",
            kind="snapshot",
            wall_ns=base_wall + index * 100_000_000,
            mono_ns=base_mono + index * 100_000_000,
            raw=None,
            payload={
                "lastUpdateId": last_id,
                "bids": [[bid, "1"]],
                "asks": [[ask, "1"]],
            },
            url=f"https://api.binance.us/api/v3/depth?symbol={symbol}&limit=5000",
            events=(),
        )

    header = CaptureHeader(exchanges={"binance": ["BTCUSDT", "ETHUSDT"]}, started_wall_ns=base_wall)
    frames = [
        rest_frame(0, "BTCUSDT", 3, "100", "101"),
        rest_frame(0, "ETHUSDT", 2, "200", "201"),
        ws_frame(1, '{"s":"BTCUSDT","U":1,"u":5,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'),
        ws_frame(2, '{"s":"ETHUSDT","U":1,"u":3,"E":1,"b":[["200","1"]],"a":[["201","1"]]}'),
        ws_frame(3, '{"s":"BTCUSDT","U":6,"u":6,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'),
        ws_frame(4, '{"s":"ETHUSDT","U":4,"u":4,"E":1,"b":[["200","1"]],"a":[["201","1"]]}'),
        # Sequence gap on BTC-USDT: last seen exchange id 6, range starts at 20.
        ws_frame(5, '{"s":"BTCUSDT","U":20,"u":20,"E":1,"b":[["100","1"]],"a":[["101","1"]]}'),
        rest_frame(5, "BTCUSDT", 21, "100", "101"),
        ws_frame(6, '{"s":"ETHUSDT","U":5,"u":5,"E":1,"b":[["200","1"]],"a":[["201","1"]]}'),
        ws_frame(7, '{"s":"BTCUSDT","U":21,"u":25,"E":1,"b":[["100","2"]],"a":[["101","1"]]}'),
    ]

    manager = OrderBookManager(max_age_seconds=60.0)
    report = asyncio.run(replay_frames(header, frames, book_manager=manager))
    repeat = asyncio.run(replay_frames(header, frames))

    assert report.digest == repeat.digest
    assert report.transitions, "expected the capture to replay to transitions"
    assert all(transition.resync_requested is False for transition in report.transitions), (
        "no frame may request a full-venue reconnect"
    )
    btc_kinds = [
        transition.kind for transition in report.transitions if transition.pair == "BTC-USDT"
    ]
    assert btc_kinds == ["snapshot", "delta", "delta", "snapshot", "delta"]
    eth = [transition for transition in report.transitions if transition.pair == "ETH-USDT"]
    assert [transition.kind for transition in eth] == [
        "snapshot",
        "delta",
        "delta",
        "delta",
    ]
    assert all(transition.accepted for transition in eth)
    assert manager.eligibility("binance", "ETH-USDT").eligible is True
    assert manager.eligibility("binance", "BTC-USDT").eligible is True


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
