from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from arb.adapters.gemini import PAIR_RESYNC_TIMEOUT_NS, GeminiAdapter
from arb.orderbook import OrderBookManager
from arb.types import EventKind, MarketEvent

SECOND = 1_000_000_000


def depth(symbol: str, first: int, last: int, bid: str = "100", ask: str = "101") -> str:
    return json.dumps(
        {
            "e": "depthUpdate",
            "E": 1,
            "s": symbol,
            "U": first,
            "u": last,
            "b": [[bid, "1"]],
            "a": [[ask, "1"]],
        }
    )


def ack(request_id: object, status: int = 200) -> str:
    return json.dumps({"id": request_id, "status": status})


def ingest(adapter: GeminiAdapter, message: str, mono_ns: int = 0) -> list[MarketEvent]:
    return asyncio.run(adapter.ingest_message(message, mono_ns)).events


def two_pair_adapter() -> GeminiAdapter:
    adapter = GeminiAdapter(["btcusd", "ethusd"])
    adapter.connected = True
    ingest(adapter, depth("btcusd", 10, 10))
    ingest(adapter, depth("ethusd", 20, 20, "200", "201"))
    return adapter


def sent(adapter: GeminiAdapter) -> list[dict[str, Any]]:
    frames = [json.loads(frame) for frame in adapter._outbox]
    adapter._outbox.clear()
    return frames


def test_gemini_first_depth_frame_is_stream_snapshot() -> None:
    adapter = GeminiAdapter(["btcusd"])
    payload = (
        '{"e":"depthUpdate","E":123,"s":"btcusd","U":5,"u":7,"b":[["100","1"]],"a":[["101","2"]]}'
    )
    events = asyncio.run(adapter.parse_message(payload))
    assert len(events) == 1
    assert events[0].kind is EventKind.SNAPSHOT
    assert events[0].pair == "BTC-USD"
    assert events[0].sequence == 1
    assert events[0].exchange_first_sequence == 5
    assert events[0].exchange_last_sequence == 7


def test_gemini_subscribes_to_current_depth_stream_with_full_snapshot() -> None:
    adapter = GeminiAdapter(["btcusd"])
    sent: list[str] = []

    class Socket:
        async def send(self, message: str) -> None:
            sent.append(message)

    asyncio.run(adapter.subscribe(Socket()))

    assert adapter.ws_url == "wss://ws.gemini.com?snapshot=-1"
    assert json.loads(sent[0]) == {
        "id": 1,
        "method": "SUBSCRIBE",
        "params": ["btcusd@depth", "btcusd@depth20"],
    }


def test_gemini_subsequent_depth_frames_are_sequential_local_deltas() -> None:
    adapter = GeminiAdapter(["btcusd"])
    asyncio.run(
        adapter.parse_message(
            '{"e":"depthUpdate","s":"BTCUSD","U":10,"u":12,"b":[["100","1"]],"a":[["101","2"]]}'
        )
    )
    e2 = asyncio.run(
        adapter.parse_message(
            '{"e":"depthUpdate","s":"BTCUSD","U":13,"u":15,"b":[["100","2"]],"a":[]}'
        )
    )
    e3 = asyncio.run(
        adapter.parse_message(
            '{"e":"depthUpdate","s":"BTCUSD","U":15,"u":18,"b":[],"a":[["101","1"]]}'
        )
    )
    assert e2[0].kind is EventKind.DELTA
    assert e3[0].kind is EventKind.DELTA
    assert e3[0].sequence == e2[0].sequence + 1
    assert e3[0].exchange_first_sequence == 15
    assert e3[0].exchange_last_sequence == 18
    assert adapter.gap_count == 0


def test_gemini_duplicate_depth_frame_is_ignored() -> None:
    adapter = GeminiAdapter(["btcusd"])
    message = '{"e":"depthUpdate","s":"BTCUSD","U":10,"u":12,"b":[],"a":[]}'
    asyncio.run(adapter.parse_message(message))

    assert asyncio.run(adapter.parse_message(message)) == []
    assert adapter.gap_count == 0


def test_gemini_reset_state_forces_resnapshot_after_reconnect() -> None:
    adapter = GeminiAdapter(["btcusd"])
    asyncio.run(
        adapter.parse_message(
            '{"e":"depthUpdate","s":"BTCUSD","U":1,"u":1,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    delta = asyncio.run(
        adapter.parse_message(
            '{"e":"depthUpdate","s":"BTCUSD","U":2,"u":2,"b":[["100","2"]],"a":[]}'
        )
    )
    assert delta[0].kind is EventKind.DELTA

    asyncio.run(adapter.reset_state())

    events = asyncio.run(
        adapter.parse_message(
            '{"e":"depthUpdate","s":"BTCUSD","U":10,"u":10,"b":[["100","1"]],"a":[["101","1"]]}'
        )
    )
    assert events[0].kind is EventKind.SNAPSHOT


def test_gemini_gap_resubscribes_only_that_pair() -> None:
    adapter = two_pair_adapter()

    events = ingest(adapter, depth("btcusd", 15, 16))

    assert [event.kind for event in events] == [EventKind.RESET]
    assert events[0].pair == "BTC-USD"
    assert adapter.gap_count == 1
    assert adapter._reconnect_requested is False
    assert "BTC-USD" not in adapter._initialized
    assert sent(adapter) == [
        {"id": "resync:btcusd:unsubscribe:1", "method": "UNSUBSCRIBE", "params": ["btcusd@depth"]}
    ]
    # The untouched pair keeps its chain.
    assert ingest(adapter, depth("ethusd", 20, 21, "200", "201"))[0].sequence == 2


def test_gemini_pair_resync_rebuilds_one_book_over_the_open_socket() -> None:
    adapter = two_pair_adapter()
    ingest(adapter, depth("btcusd", 10, 11))

    assert adapter.request_pair_resync("BTC-USD") is True
    assert sent(adapter)[0]["method"] == "UNSUBSCRIBE"
    # Tail frames of the old subscription are dropped, not applied.
    assert ingest(adapter, depth("btcusd", 11, 12)) == []
    assert ingest(adapter, depth("ethusd", 20, 22, "200", "201"))[0].kind is EventKind.DELTA

    assert ingest(adapter, ack("resync:btcusd:unsubscribe:1")) == []
    assert sent(adapter) == [
        {"id": "resync:btcusd:subscribe:2", "method": "SUBSCRIBE", "params": ["btcusd@depth"]}
    ]
    snapshot = ingest(adapter, depth("btcusd", 40, 40, "99", "102"))
    assert snapshot[0].kind is EventKind.SNAPSHOT
    assert snapshot[0].sequence == 1
    assert ingest(adapter, ack("resync:btcusd:subscribe:2")) == []
    assert ingest(adapter, depth("btcusd", 40, 41))[0].kind is EventKind.DELTA
    assert adapter._resync_deadline_ns == {}
    assert adapter._reconnect_requested is False


def test_gemini_repeated_request_does_not_restart_a_pending_resync() -> None:
    adapter = two_pair_adapter()

    assert adapter.request_pair_resync("BTC-USD") is True
    assert adapter.request_pair_resync("BTC-USD") is True

    assert len(sent(adapter)) == 1


def test_gemini_pair_resync_needs_a_live_connection_and_a_known_pair() -> None:
    adapter = GeminiAdapter(["btcusd"])
    assert adapter.request_pair_resync("BTC-USD") is False

    adapter.connected = True
    assert adapter.request_pair_resync("DOGE-USD") is False
    adapter.request_reconnect("sequence_gap")
    assert adapter.request_pair_resync("BTC-USD") is False
    assert adapter._outbox == []


def test_gemini_rejected_resync_falls_back_to_a_labelled_reconnect() -> None:
    adapter = two_pair_adapter()
    adapter.request_pair_resync("BTC-USD")

    ingest(adapter, ack("resync:btcusd:unsubscribe:1", status=400))

    assert adapter._reconnect_requested is True
    assert adapter.reconnect_request().cause == "pair_resync_failed"


def test_gemini_resync_without_a_snapshot_times_out_to_a_reconnect() -> None:
    adapter = two_pair_adapter()
    assert adapter.request_pair_resync("BTC-USD") is True

    # The deadline runs from the first frame received after the request, on
    # the frames' own clock, so replay escalates exactly when live did.
    start = 7_000 * SECOND
    ingest(adapter, depth("ethusd", 20, 21, "200", "201"), start)
    ingest(adapter, depth("ethusd", 21, 22, "200", "201"), start + PAIR_RESYNC_TIMEOUT_NS - 1)
    assert adapter._reconnect_requested is False
    ingest(adapter, depth("ethusd", 22, 23, "200", "201"), start + PAIR_RESYNC_TIMEOUT_NS)

    assert adapter._reconnect_requested is True
    assert adapter.reconnect_request().cause == "pair_resync_timeout"


def test_gemini_unrelated_acknowledgements_are_ignored() -> None:
    adapter = two_pair_adapter()

    for message in (ack(1), ack("resync:dogeusd:unsubscribe:3"), ack("other", status=400)):
        assert ingest(adapter, message) == []

    assert adapter._reconnect_requested is False
    assert adapter._outbox == []


def test_gemini_recorded_acknowledgement_replays_a_resync_it_did_not_request() -> None:
    # Replay does not run the reconciler, so the capture's acknowledgement is
    # the only sign that live resubscribed the pair.
    adapter = two_pair_adapter()

    events = ingest(adapter, ack("resync:btcusd:unsubscribe:7"))

    assert [event.kind for event in events] == [EventKind.RESET]
    assert ingest(adapter, depth("btcusd", 50, 50))[0].kind is EventKind.SNAPSHOT


def test_gemini_reset_state_clears_pending_resyncs() -> None:
    adapter = two_pair_adapter()
    adapter.request_pair_resync("BTC-USD")

    asyncio.run(adapter.reset_state())

    assert adapter._resync_phase == {}
    assert adapter._resync_deadline_ns == {}
    assert adapter._outbox == []
    assert ingest(adapter, depth("btcusd", 60, 60))[0].kind is EventKind.SNAPSHOT


@pytest.mark.asyncio
async def test_gemini_stream_sends_a_resync_requested_while_the_read_is_blocked() -> None:
    adapter = GeminiAdapter(["btcusd", "ethusd"])
    adapter.connected = True
    inbound: asyncio.Queue[str] = asyncio.Queue()
    outbound: list[str] = []

    class Socket:
        def __aiter__(self) -> Socket:
            return self

        async def __anext__(self) -> str:
            return await inbound.get()

        async def send(self, message: str) -> None:
            outbound.append(message)
            request = json.loads(message)
            if request["method"] == "UNSUBSCRIBE":
                await inbound.put(ack(request["id"]))
            else:
                await inbound.put(depth("btcusd", 30, 30, "98", "103"))

    await inbound.put(depth("btcusd", 10, 10))
    await inbound.put(depth("ethusd", 20, 20, "200", "201"))
    stream = adapter.stream_events(Socket())
    first = [await anext(stream), await anext(stream)]
    assert [event.kind for event in first] == [EventKind.SNAPSHOT, EventKind.SNAPSHOT]

    # The inbound queue is now empty, so the next read blocks until the
    # request wakes the loop and the socket answers.
    next_event = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    assert adapter.request_pair_resync("BTC-USD") is True
    rebuilt = await asyncio.wait_for(next_event, timeout=1)
    await stream.aclose()

    assert rebuilt.kind is EventKind.SNAPSHOT
    assert rebuilt.pair == "BTC-USD"
    assert rebuilt.received_monotonic_ns is not None
    assert [json.loads(message)["method"] for message in outbound] == [
        "UNSUBSCRIBE",
        "SUBSCRIBE",
    ]


def test_gemini_pair_resync_keeps_venue_eligible_in_replay() -> None:
    # ARB-046: a captured resubscription of one pair rebuilds that book while
    # the other pair stays eligible and no full-venue reconnect is requested.
    import time

    from arb.capture import CaptureFrame, CaptureHeader
    from arb.replay import replay_frames

    # Live-clock bases, as in the Binance replay test: the caller-supplied
    # manager judges final eligibility against its own monotonic clock.
    base_mono = time.monotonic_ns()
    base_wall = time.time_ns()

    def ws_frame(index: int, raw: str) -> CaptureFrame:
        return CaptureFrame(
            exchange="gemini",
            kind="ws",
            wall_ns=base_wall + index * SECOND // 10,
            mono_ns=base_mono + index * SECOND // 10,
            raw=raw,
            payload=None,
            url=None,
            events=(),
        )

    header = CaptureHeader(exchanges={"gemini": ["btcusd", "ethusd"]}, started_wall_ns=0)
    frames = [
        ws_frame(1, depth("btcusd", 10, 10)),
        ws_frame(2, depth("ethusd", 20, 20, "200", "201")),
        ws_frame(3, depth("btcusd", 10, 11)),
        ws_frame(4, ack("resync:btcusd:unsubscribe:1")),
        ws_frame(5, depth("ethusd", 20, 21, "200", "201")),
        ws_frame(6, depth("btcusd", 90, 90, "99", "102")),
        ws_frame(7, ack("resync:btcusd:subscribe:2")),
        ws_frame(8, depth("btcusd", 90, 91, "99.5", "102")),
        ws_frame(9, depth("ethusd", 21, 22, "200", "201")),
    ]

    manager = OrderBookManager(max_age_seconds=60.0)
    report = asyncio.run(replay_frames(header, frames, book_manager=manager))
    repeat = asyncio.run(replay_frames(header, frames))

    assert report.digest == repeat.digest
    assert all(transition.resync_requested is False for transition in report.transitions)
    btc = [transition.kind for transition in report.transitions if transition.pair == "BTC-USD"]
    assert btc == ["snapshot", "delta", "reset", "snapshot", "delta"]
    eth = [transition for transition in report.transitions if transition.pair == "ETH-USD"]
    assert [transition.kind for transition in eth] == ["snapshot", "delta", "delta"]
    assert all(transition.accepted for transition in eth)
    assert manager.eligibility("gemini", "ETH-USD").eligible is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True
    assert manager.level_snapshot("gemini", "BTC-USD") is not None


def test_gemini_stray_delta_while_awaiting_the_snapshot_never_seeds_the_book() -> None:
    adapter = two_pair_adapter()
    adapter.request_pair_resync("BTC-USD")
    ingest(adapter, ack("resync:btcusd:unsubscribe:1"))

    # A chained delta (U < u) is not a subscription snapshot.
    assert ingest(adapter, depth("btcusd", 10, 11, "98", "103")) == []
    assert ingest(adapter, depth("btcusd", 13, 12)) == []
    assert "BTC-USD" not in adapter._initialized
    assert adapter._reconnect_requested is False
    assert len(sent(adapter)) == 2

    snapshot = ingest(adapter, depth("btcusd", 11, 11, "99", "102"))
    assert snapshot[0].kind is EventKind.SNAPSHOT


def test_gemini_repeated_resyncs_of_one_pair_escalate_to_a_reconnect() -> None:
    adapter = two_pair_adapter()

    def resync_and_rebuild(at_ns: int) -> bool:
        ingest(adapter, depth("ethusd", 20, 20, "200", "201"), at_ns)
        accepted = adapter.request_pair_resync("BTC-USD")
        if accepted:
            ingest(adapter, ack(f"resync:btcusd:unsubscribe:{adapter._resync_number}"), at_ns)
            ingest(adapter, depth("btcusd", 90, 90), at_ns)
        return accepted

    assert all(resync_and_rebuild(index * SECOND) for index in range(1, 4))
    assert adapter._reconnect_requested is False

    assert resync_and_rebuild(4 * SECOND) is False
    assert adapter.reconnect_request().cause == "pair_resync_repeated"


def test_gemini_resyncs_spread_beyond_the_window_stay_scoped() -> None:
    adapter = two_pair_adapter()

    for index in range(6):
        at_ns = index * 30 * SECOND
        ingest(adapter, depth("ethusd", 20, 20, "200", "201"), at_ns)
        assert adapter.request_pair_resync("BTC-USD") is True
        ingest(adapter, ack(f"resync:btcusd:unsubscribe:{adapter._resync_number}"), at_ns)
        ingest(adapter, depth("btcusd", 90 + index, 90 + index), at_ns)

    assert adapter._reconnect_requested is False


def test_gemini_replay_started_resync_does_not_depend_on_the_host_clock() -> None:
    # A crossed snapshot makes replay itself request the pair resync. Whether
    # the recording's clock sits far below or far above this host's, the
    # outcome must be identical: no deadline may come from a clock read.
    import time

    from arb.capture import CaptureFrame, CaptureHeader, ConnectionBoundary
    from arb.replay import replay_frames

    def frames_at(base_ns: int) -> list[CaptureFrame]:
        def at(index: int) -> tuple[int, int]:
            return base_ns + index * SECOND // 10, base_ns + index * SECOND // 10

        def ws_frame(index: int, raw: str) -> CaptureFrame:
            wall, mono = at(index)
            return CaptureFrame("gemini", "ws", wall, mono, raw, None, None, ())

        wall, mono = at(0)
        boundary = ConnectionBoundary(connected=True, generation=1, reason=None)
        return [
            CaptureFrame("gemini", "connection", wall, mono, None, None, None, (), boundary),
            ws_frame(1, depth("ethusd", 20, 20, "200", "201")),
            ws_frame(2, depth("btcusd", 10, 10, "102", "101")),  # crossed
            ws_frame(3, ack("resync:btcusd:unsubscribe:1")),
            ws_frame(4, depth("btcusd", 30, 30, "99", "102")),
            ws_frame(5, depth("ethusd", 20, 21, "200", "201")),
        ]

    header = CaptureHeader(exchanges={"gemini": ["btcusd", "ethusd"]}, started_wall_ns=0)
    early = asyncio.run(replay_frames(header, frames_at(SECOND)))
    late = asyncio.run(replay_frames(header, frames_at(time.monotonic_ns() + 10_000 * SECOND)))

    for report in (early, late):
        assert [t.kind for t in report.transitions if t.pair == "BTC-USD"] == [
            "snapshot",
            "snapshot",
        ]
        assert any(event.kind == "scoped_resync" for event in report.lifecycle)
        assert not any(event.kind == "reconnect_requested" for event in report.lifecycle)
    assert [event.kind for event in early.lifecycle] == [event.kind for event in late.lifecycle]


def partial(symbol: str, update_id: int, bid: str = "100", ask: str = "101") -> str:
    return json.dumps(
        {"lastUpdateId": update_id, "symbol": symbol, "bids": [[bid, "1"]], "asks": [[ask, "1"]]}
    )


def test_gemini_partial_snapshot_waits_for_its_exact_update_id() -> None:
    adapter = two_pair_adapter()

    # Gemini usually sends the partial just before the depth frame ending at its id.
    assert ingest(adapter, partial("btcusd", 12)) == []
    events = ingest(adapter, depth("btcusd", 10, 12))

    assert [event.kind for event in events] == [EventKind.DELTA, EventKind.VERIFY]
    verify = events[1]
    assert verify.sequence == events[0].sequence
    assert verify.exchange_last_sequence == 12
    assert verify.verify_depth == 20
    assert [(str(level.price), str(level.size)) for level in verify.bids] == [("100", "1")]


def test_gemini_partial_snapshot_at_the_current_id_verifies_immediately() -> None:
    adapter = two_pair_adapter()

    events = ingest(adapter, partial("ethusd", 20, "200", "201"))

    assert [event.kind for event in events] == [EventKind.VERIFY]
    assert events[0].sequence == 1


def test_gemini_partial_snapshot_the_book_passed_is_skipped() -> None:
    adapter = two_pair_adapter()

    ingest(adapter, partial("btcusd", 11))
    assert ingest(adapter, depth("btcusd", 10, 13))[-1].kind is EventKind.DELTA
    assert adapter._pending_verification == {}
    # Already past this id: nothing to compare at the same update.
    assert ingest(adapter, partial("btcusd", 12)) == []


def test_gemini_partial_snapshot_is_ignored_while_the_pair_resyncs() -> None:
    adapter = two_pair_adapter()
    ingest(adapter, partial("btcusd", 15))
    adapter.request_pair_resync("BTC-USD")

    assert adapter._pending_verification == {}
    assert ingest(adapter, partial("btcusd", 16)) == []
    ingest(adapter, ack("resync:btcusd:unsubscribe:1"))
    assert ingest(adapter, partial("btcusd", 30)) == []
    snapshot = ingest(adapter, depth("btcusd", 30, 30))
    # The rebuilt book is checked from its next partial onwards.
    assert [event.kind for event in snapshot] == [EventKind.SNAPSHOT]
    assert [event.kind for event in ingest(adapter, partial("btcusd", 30))] == [EventKind.VERIFY]


def test_gemini_verification_catches_divergence_in_replay() -> None:
    # ARB-048: the incremental BTC book misses a bid Gemini's own top-N shows.
    # The next aligned partial invalidates it and replay resubscribes only
    # BTC-USD; ETH-USD stays eligible and no reconnect is requested.
    import time

    from arb.capture import CaptureFrame, CaptureHeader, ConnectionBoundary
    from arb.replay import replay_frames

    base_mono, base_wall = time.monotonic_ns(), time.time_ns()

    def at(index: int) -> tuple[int, int]:
        return base_wall + index * SECOND // 10, base_mono + index * SECOND // 10

    def ws_frame(index: int, raw: str) -> CaptureFrame:
        wall, mono = at(index)
        return CaptureFrame("gemini", "ws", wall, mono, raw, None, None, ())

    wall, mono = at(0)
    boundary = ConnectionBoundary(connected=True, generation=1, reason=None)
    true_bids = [["100", "1"], ["99.5", "2"]]
    frames = [
        CaptureFrame("gemini", "connection", wall, mono, None, None, None, (), boundary),
        ws_frame(1, depth("btcusd", 10, 10)),
        ws_frame(2, depth("ethusd", 20, 20, "200", "201")),
        ws_frame(
            3,
            json.dumps(
                {"lastUpdateId": 11, "symbol": "btcusd", "bids": true_bids, "asks": [["101", "1"]]}
            ),
        ),
        ws_frame(4, depth("btcusd", 10, 11)),  # aligned at id 11: 99.5 is missing
        ws_frame(5, ack("resync:btcusd:unsubscribe:1")),
        ws_frame(6, json.dumps({"id": "resync:btcusd:subscribe:2", "status": 200})),
        ws_frame(
            7,
            json.dumps(
                {
                    "e": "depthUpdate",
                    "E": 1,
                    "s": "btcusd",
                    "U": 40,
                    "u": 40,
                    "b": true_bids,
                    "a": [["101", "1"]],
                }
            ),
        ),
        ws_frame(8, depth("ethusd", 20, 21, "200", "201")),
    ]
    header = CaptureHeader(exchanges={"gemini": ["btcusd", "ethusd"]}, started_wall_ns=0)
    manager = OrderBookManager(max_age_seconds=60.0)

    report = asyncio.run(replay_frames(header, frames, book_manager=manager))
    repeat = asyncio.run(replay_frames(header, frames))

    assert report.digest == repeat.digest
    btc = [(t.kind, t.reason) for t in report.transitions if t.pair == "BTC-USD"]
    assert btc == [
        ("snapshot", None),
        ("delta", None),
        ("verify", "verification_mismatch"),
        ("snapshot", None),
    ]
    assert any(event.kind == "scoped_resync" for event in report.lifecycle)
    assert not any(event.kind == "reconnect_requested" for event in report.lifecycle)
    assert manager.eligibility("gemini", "ETH-USD").eligible is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True


def test_gemini_stalled_verification_forces_a_labelled_reconnect() -> None:
    from arb.adapters.gemini import VERIFY_STALL_NS

    adapter = two_pair_adapter()
    ingest(adapter, partial("btcusd", 10), 1 * SECOND)  # verified: the book is at id 10

    # Depth keeps flowing but no partial ever aligns again.
    ingest(adapter, depth("btcusd", 10, 11), 1 * SECOND + VERIFY_STALL_NS)
    assert adapter._reconnect_requested is False
    ingest(adapter, depth("btcusd", 11, 12), 2 * SECOND + VERIFY_STALL_NS)

    assert adapter._reconnect_requested is True
    assert adapter.reconnect_request().cause == "verification_stalled"


def test_gemini_without_partial_snapshots_is_never_watched() -> None:
    # Captures recorded before ARB-048 carry no @depth20 frames and must
    # replay without a spurious stall.
    adapter = two_pair_adapter()

    ingest(adapter, depth("btcusd", 10, 11), 3_600 * SECOND)

    assert adapter._reconnect_requested is False
