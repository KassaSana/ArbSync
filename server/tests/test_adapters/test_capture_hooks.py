from __future__ import annotations

import asyncio
import types
from decimal import Decimal
from typing import Any

import pytest
from arb.adapters.binance import BinanceAdapter
from arb.adapters.gemini import GeminiAdapter
from arb.capture import CaptureWriter, SnapshotProvenance, read_capture
from arb.types import EventKind, MarketEvent, PriceLevel


class FakeSocket:
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class RecordingSink:
    def __init__(self) -> None:
        self.ws_calls: list[dict[str, Any]] = []
        self.snapshot_calls: list[dict[str, Any]] = []

    def record_ws(
        self,
        exchange: str,
        raw: str,
        events: list[Any],
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> bool:
        self.ws_calls.append(
            {"exchange": exchange, "raw": raw, "events": events, "wall_ns": wall_ns}
        )
        return True

    def record_snapshot(
        self,
        exchange: str,
        url: str,
        payload: dict[str, Any],
        *,
        provenance: SnapshotProvenance | None = None,
    ) -> bool:
        self.snapshot_calls.append(
            {"exchange": exchange, "url": url, "payload": payload, "provenance": provenance}
        )
        return True

    def record_connection(
        self,
        exchange: str,
        connected: bool,
        generation: int,
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
        reason: str | None = None,
    ) -> bool:
        self.snapshot_calls.append(
            {
                "exchange": exchange,
                "connected": connected,
                "generation": generation,
                "wall_ns": wall_ns,
                "mono_ns": mono_ns,
                "reason": reason,
            }
        )
        return True


GEMINI_MESSAGE = (
    '{"e":"depthUpdate","s":"BTCUSD","U":1,"u":1,"E":1720000000000,'
    '"b":[["100","1"]],"a":[["101","2"]]}'
)


@pytest.mark.asyncio
async def test_base_stream_events_records_exact_raw_text() -> None:
    adapter = GeminiAdapter(["btcusd"])
    sink = RecordingSink()
    adapter.set_capture_sink(sink)

    events = [event async for event in adapter.stream_events(FakeSocket([GEMINI_MESSAGE]))]

    assert len(events) == 1
    assert events[0].kind is EventKind.SNAPSHOT
    assert len(sink.ws_calls) == 1
    assert sink.ws_calls[0]["raw"] == GEMINI_MESSAGE
    # The sink sees parse output before stream_events stamps receipt time;
    # replay re-stamps from the frame's own monotonic reading instead.
    assert [(event.pair, event.sequence) for event in sink.ws_calls[0]["events"]] == [
        ("BTC-USD", 1)
    ]
    assert events[0].received_monotonic_ns is not None


@pytest.mark.asyncio
async def test_base_stream_events_without_sink_still_yields() -> None:
    adapter = GeminiAdapter(["btcusd"])

    events = [event async for event in adapter.stream_events(FakeSocket([GEMINI_MESSAGE]))]

    assert len(events) == 1


@pytest.mark.asyncio
async def test_snapshot_fetch_records_url_and_payload() -> None:
    adapter = GeminiAdapter(["btcusd"])
    sink = RecordingSink()
    adapter.set_capture_sink(sink)
    payload: dict[str, Any] = {"bids": [], "asks": []}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return payload

    class FakeClient:
        async def get(self, url: str) -> FakeResponse:
            assert url.endswith("/btcusd?limit_bids=100&limit_asks=100")
            return FakeResponse()

    adapter.http_client = lambda: FakeClient()  # type: ignore[method-assign, assignment, return-value]
    event = await adapter.fetch_snapshot_with_context("BTC-USD", 0, purpose="initial_sync")

    assert event.kind is EventKind.SNAPSHOT
    assert len(sink.snapshot_calls) == 1
    assert sink.snapshot_calls[0]["url"].endswith("/btcusd?limit_bids=100&limit_asks=100")
    assert sink.snapshot_calls[0]["payload"] == payload
    provenance = sink.snapshot_calls[0]["provenance"]
    assert isinstance(provenance, SnapshotProvenance)
    assert provenance.purpose == "initial_sync"
    assert provenance.pair == "BTC-USD"
    assert provenance.connection_generation == 0
    assert provenance.request_wall_ns is not None
    assert provenance.response_wall_ns >= provenance.request_wall_ns


@pytest.mark.asyncio
async def test_connection_boundaries_are_captured_with_generations() -> None:
    adapter = GeminiAdapter(["btcusd"])
    sink = RecordingSink()
    adapter.set_capture_sink(sink)

    await adapter._report_connection_state(True)
    await adapter._report_connection_state(False)

    boundaries = [entry for entry in sink.snapshot_calls if "connected" in entry]
    assert [(entry["connected"], entry["generation"]) for entry in boundaries] == [
        (True, 1),
        (False, 1),
    ]


@pytest.mark.asyncio
async def test_binance_loop_records_each_message_once() -> None:
    adapter = BinanceAdapter(["BTCUSDT"])
    sink = RecordingSink()
    adapter.set_capture_sink(sink)

    async def snapshot_at_100(
        self: BinanceAdapter, pair: str, trigger_sequence: int
    ) -> MarketEvent:
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

    adapter.fetch_snapshot = types.MethodType(snapshot_at_100, adapter)  # type: ignore[method-assign]
    depth = '{"s":"BTCUSDT","U":99,"u":105,"E":1,"b":[["100","2"]],"a":[]}'
    stream = adapter.stream_events(FakeSocket([depth]))
    first = await anext(stream)
    await stream.aclose()

    assert first.kind is EventKind.SNAPSHOT
    assert len(sink.ws_calls) == 1
    assert sink.ws_calls[0]["raw"] == depth


@pytest.mark.asyncio
async def test_capture_end_to_end_through_real_writer(tmp_path: Any) -> None:
    path = tmp_path / "capture.jsonl"
    writer = CaptureWriter(path, {"gemini": ["btcusd"]})
    task = asyncio.create_task(writer.run())
    adapter = GeminiAdapter(["btcusd"])
    adapter.set_capture_sink(writer)

    events = [event async for event in adapter.stream_events(FakeSocket([GEMINI_MESSAGE]))]

    await writer.close()
    await task
    _, frames = read_capture(path)
    assert len(events) == 1
    assert len(frames) == 1
    assert frames[0].raw == GEMINI_MESSAGE
    assert frames[0].events[0].pair == "BTC-USD"
