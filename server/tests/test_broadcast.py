import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from arb import broadcast as broadcast_module
from arb.broadcast import LiveBroadcaster
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.types import BookEligibility, EventKind, LiveMessage, MarketEvent, PriceLevel
from fastapi import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed


class FakeWebSocket:
    def __init__(self, *, send_error: Exception | None = None) -> None:
        self.send_error = send_error
        self.accepted = False
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self.client: object | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True


class _RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_broadcaster_sends_to_all_clients() -> None:
    broadcaster = LiveBroadcaster()
    a, b = FakeWebSocket(), FakeWebSocket()
    await broadcaster.connect(a)
    await broadcaster.connect(b)
    await broadcaster.broadcast(LiveMessage(type="opportunity", payload={"pair": "BTC-USD"}))
    await asyncio.sleep(0)
    assert a.sent == [
        {
            "type": "opportunity",
            "payload": {"pair": "BTC-USD"},
            "stream_sequence": 1,
        }
    ]
    assert b.sent == a.sent


@pytest.mark.asyncio
async def test_expected_disconnect_quietly_removes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    failure_metric = Mock()
    test_logger = Mock()
    monkeypatch.setattr(broadcast_module, "ws_sender_failures_total", failure_metric)
    monkeypatch.setattr(broadcast_module, "logger", test_logger)
    broadcaster = LiveBroadcaster()
    healthy, dead = FakeWebSocket(), FakeWebSocket(send_error=WebSocketDisconnect())
    await broadcaster.connect(healthy)
    await broadcaster.connect(dead)
    dead_sender = broadcaster._clients[dead].sender_task
    assert dead_sender is not None
    await broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"x": 1}))
    await dead_sender
    # Dead client must be removed from the pool.
    assert dead not in broadcaster._clients
    assert healthy in broadcaster._clients
    failure_metric.inc.assert_not_called()
    test_logger.error.assert_not_called()
    # A second broadcast does not raise even though dead is gone.
    await broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"x": 2}))
    await asyncio.sleep(0)
    assert len(healthy.sent) == 2
    await broadcaster.disconnect(healthy)


@pytest.mark.asyncio
async def test_closed_websockets_connection_is_an_expected_departure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_metric = Mock()
    test_logger = Mock()
    monkeypatch.setattr(broadcast_module, "ws_sender_failures_total", failure_metric)
    monkeypatch.setattr(broadcast_module, "logger", test_logger)
    broadcaster = LiveBroadcaster()
    socket = FakeWebSocket(send_error=ConnectionClosed(None, None))
    await broadcaster.connect(socket)
    sender = broadcaster._clients[socket].sender_task
    assert sender is not None

    await broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"x": 1}))
    await sender

    assert socket not in broadcaster._clients
    failure_metric.inc.assert_not_called()
    test_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_serialization_failure_is_counted_and_logged_without_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SerializingSocket(FakeWebSocket):
        async def send_json(self, payload: dict[str, Any]) -> None:
            json.dumps(payload)

    failure_metric = Mock()
    test_logger = Mock()
    monkeypatch.setattr(broadcast_module, "ws_sender_failures_total", failure_metric)
    monkeypatch.setattr(broadcast_module, "logger", test_logger)
    broadcaster = LiveBroadcaster()
    socket = SerializingSocket()
    socket.client = SimpleNamespace(host="127.0.0.1", port=54321)
    await broadcaster.connect(socket)
    sender = broadcaster._clients[socket].sender_task
    assert sender is not None

    await broadcaster.broadcast(LiveMessage(type="opportunity", payload={"not_json": Decimal("1")}))
    await sender

    assert socket not in broadcaster._clients
    failure_metric.inc.assert_called_once_with()
    test_logger.error.assert_called_once_with(
        "websocket_sender_failed",
        exception_type="TypeError",
        client_host="127.0.0.1",
        client_port=54321,
        message_type="opportunity",
        stream_sequence=1,
    )


@pytest.mark.asyncio
async def test_sender_cancellation_cleans_up_without_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_metric = Mock()
    test_logger = Mock()
    monkeypatch.setattr(broadcast_module, "ws_sender_failures_total", failure_metric)
    monkeypatch.setattr(broadcast_module, "logger", test_logger)
    broadcaster = LiveBroadcaster()
    socket = FakeWebSocket()
    await broadcaster.connect(socket)
    sender = broadcaster._clients[socket].sender_task
    assert sender is not None

    await broadcaster.disconnect(socket)

    assert socket not in broadcaster._clients
    assert sender.cancelled()
    failure_metric.inc.assert_not_called()
    test_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_broadcaster_with_no_clients_is_noop() -> None:
    broadcaster = LiveBroadcaster()
    # Must not raise.
    await broadcaster.broadcast(LiveMessage(type="opportunity", payload={}))


@pytest.mark.asyncio
async def test_initial_state_is_ordered_before_live_updates() -> None:
    broadcaster = LiveBroadcaster()
    websocket = FakeWebSocket()
    await broadcaster.connect(
        websocket,
        lambda: LiveMessage(type="state_snapshot", payload={"books": []}),
    )
    await broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"sequence": 2}))
    await asyncio.sleep(0)

    assert [message["type"] for message in websocket.sent] == [
        "state_snapshot",
        "top_of_book",
    ]
    assert [message["stream_sequence"] for message in websocket.sent] == [1, 2]


@pytest.mark.asyncio
async def test_pending_update_before_connect_is_cut_off_by_snapshot() -> None:
    broadcaster = LiveBroadcaster(coalesce_interval=60)
    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"sequence": 1})
    )
    websocket = FakeWebSocket()

    await broadcaster.connect(
        websocket,
        lambda: LiveMessage("state_snapshot", {"books": [{"sequence": 2}]}),
    )
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert [(message["type"], message["payload"]) for message in websocket.sent] == [
        ("state_snapshot", {"books": [{"sequence": 2}]})
    ]
    await broadcaster.disconnect(websocket)
    await broadcaster.aclose()


@pytest.mark.asyncio
async def test_update_queued_during_accept_is_cut_off_by_later_snapshot() -> None:
    accepting = asyncio.Event()
    continue_accept = asyncio.Event()

    class PausedAcceptSocket(FakeWebSocket):
        async def accept(self) -> None:
            accepting.set()
            await continue_accept.wait()
            await super().accept()

    broadcaster = LiveBroadcaster(coalesce_interval=60)
    websocket = PausedAcceptSocket()
    connecting = asyncio.create_task(
        broadcaster.connect(
            websocket,
            lambda: LiveMessage("state_snapshot", {"books": [{"sequence": 2}]}),
        )
    )
    await accepting.wait()
    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"sequence": 1})
    )
    continue_accept.set()
    await connecting
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert [message["type"] for message in websocket.sent] == ["state_snapshot"]
    await broadcaster.disconnect(websocket)
    await broadcaster.aclose()


@pytest.mark.asyncio
async def test_update_immediately_after_snapshot_is_delivered() -> None:
    broadcaster = LiveBroadcaster(coalesce_interval=60)
    websocket = FakeWebSocket()
    await broadcaster.connect(
        websocket,
        lambda: LiveMessage("state_snapshot", {"books": [{"sequence": 1}]}),
    )

    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"sequence": 2})
    )
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert [message["type"] for message in websocket.sent] == [
        "state_snapshot",
        "top_of_book",
    ]
    assert [message["payload"].get("sequence") for message in websocket.sent] == [None, 2]
    await broadcaster.disconnect(websocket)
    await broadcaster.aclose()


@pytest.mark.asyncio
async def test_slow_new_client_keeps_snapshot_cutoff_and_post_snapshot_update() -> None:
    release_send = asyncio.Event()

    class SlowSocket(FakeWebSocket):
        async def send_json(self, payload: dict[str, Any]) -> None:
            await release_send.wait()
            await super().send_json(payload)

    broadcaster = LiveBroadcaster(queue_maxsize=2, coalesce_interval=60)
    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"sequence": 1})
    )
    websocket = SlowSocket()
    await broadcaster.connect(
        websocket,
        lambda: LiveMessage("state_snapshot", {"books": [{"sequence": 2}]}),
    )
    await asyncio.sleep(0)
    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"sequence": 3})
    )
    await broadcaster.flush()
    release_send.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [message["type"] for message in websocket.sent] == [
        "state_snapshot",
        "top_of_book",
    ]
    assert websocket.sent[-1]["payload"]["sequence"] == 3
    assert websocket.closed is False
    await broadcaster.disconnect(websocket)
    await broadcaster.aclose()


@pytest.mark.asyncio
async def test_clients_connecting_at_different_times_get_distinct_pending_cutoffs() -> None:
    broadcaster = LiveBroadcaster(coalesce_interval=60)
    first, second = FakeWebSocket(), FakeWebSocket()
    await broadcaster.connect(
        first,
        lambda: LiveMessage("state_snapshot", {"books": []}),
    )
    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"sequence": 1})
    )
    await broadcaster.connect(
        second,
        lambda: LiveMessage("state_snapshot", {"books": [{"sequence": 1}]}),
    )
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert [message["type"] for message in first.sent] == [
        "state_snapshot",
        "top_of_book",
    ]
    assert [message["type"] for message in second.sent] == ["state_snapshot"]
    await broadcaster.disconnect(first)
    await broadcaster.disconnect(second)
    await broadcaster.aclose()


@pytest.mark.asyncio
async def test_slow_client_queue_overflow_does_not_block_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_metric = Mock()
    monkeypatch.setattr(broadcast_module, "ws_sender_failures_total", failure_metric)
    release_send = asyncio.Event()

    class SlowWebSocket(FakeWebSocket):
        async def send_json(self, payload: dict[str, Any]) -> None:
            await release_send.wait()
            await super().send_json(payload)

    broadcaster = LiveBroadcaster(queue_maxsize=1)
    slow = SlowWebSocket()
    await broadcaster.connect(slow)
    await broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"sequence": 1}))
    await asyncio.sleep(0)
    await broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"sequence": 2}))

    await asyncio.wait_for(
        broadcaster.broadcast(LiveMessage(type="top_of_book", payload={"sequence": 3})),
        timeout=0.1,
    )
    await asyncio.sleep(0)

    assert slow not in broadcaster._clients
    assert slow.closed is True
    failure_metric.inc.assert_not_called()
    release_send.set()


@pytest.mark.asyncio
async def test_coalescing_keeps_only_the_newest_update_per_book() -> None:
    broadcaster = LiveBroadcaster()
    socket = _RecordingSocket()
    await broadcaster.connect(socket)

    def quote(price: int, sequence: int) -> LiveMessage:
        return LiveMessage(
            "top_of_book",
            {
                "best_bid_price": str(price),
                "best_bid_size": "1",
                "best_ask_price": str(price + 1),
                "best_ask_size": "1",
                "sequence": sequence,
            },
        )

    for sequence in range(5):
        await broadcaster.broadcast_book("gemini", "BTC-USD", quote(100 + sequence, sequence))
    await broadcaster.broadcast_book("coinbase", "BTC-USD", quote(200, 99))
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert [payload["payload"]["sequence"] for payload in socket.sent] == [4, 99]
    await broadcaster.aclose()
    await broadcaster.disconnect(socket)


@pytest.mark.asyncio
async def test_immediate_book_update_discards_superseded_queued_update() -> None:
    """A disconnect must not be followed by a stale update showing the book live."""
    broadcaster = LiveBroadcaster()
    socket = _RecordingSocket()
    await broadcaster.connect(socket)

    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("book_status", {"eligible": True})
    )
    await broadcaster.broadcast_book_now(
        "gemini", "BTC-USD", LiveMessage("book_status", {"eligible": False})
    )
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert [payload["payload"]["eligible"] for payload in socket.sent] == [False]
    await broadcaster.aclose()
    await broadcaster.disconnect(socket)


@pytest.mark.asyncio
async def test_pipeline_gap_invalidates_before_a_pending_quote_can_flush() -> None:
    from arb.detector import ArbitrageDetector
    from arb.main import process_market_event

    broadcaster = LiveBroadcaster(coalesce_interval=60)
    socket = _RecordingSocket()
    await broadcaster.connect(socket)
    manager = OrderBookManager()
    detector = ArbitrageDetector(Decimal("0.1"))
    store = OpportunityStore(":memory:")
    initial = MarketEvent(
        "gemini",
        "BTC-USD",
        EventKind.SNAPSHOT,
        1,
        1,
        bids=(PriceLevel(Decimal("100"), Decimal("1")),),
        asks=(PriceLevel(Decimal("101"), Decimal("1")),),
    )
    await process_market_event(
        initial, book_manager=manager, detector=detector, store=store, broadcaster=broadcaster
    )
    gap = MarketEvent("gemini", "BTC-USD", EventKind.DELTA, 3, 3)
    await process_market_event(
        gap, book_manager=manager, detector=detector, store=store, broadcaster=broadcaster
    )
    await broadcaster.flush()
    await asyncio.sleep(0)
    assert len(socket.sent) == 1
    assert socket.sent[0]["type"] == "book_status"
    assert socket.sent[0]["payload"]["eligible"] is False
    assert manager.top_of_book("gemini", "BTC-USD") is None
    await broadcaster.aclose()
    await broadcaster.disconnect(socket)


@pytest.mark.asyncio
async def test_gap_storm_sends_one_invalidation_not_one_per_refused_event() -> None:
    """After a gap every delta is refused until a snapshot; that must not flood."""
    from arb.detector import ArbitrageDetector
    from arb.main import process_market_event

    broadcaster = LiveBroadcaster()
    socket = _RecordingSocket()
    await broadcaster.connect(socket)
    manager = OrderBookManager()
    detector = ArbitrageDetector(Decimal("0.1"))
    store = OpportunityStore(":memory:")

    def event(kind: EventKind, sequence: int, price: str = "100") -> MarketEvent:
        return MarketEvent(
            "gemini",
            "BTC-USD",
            kind,
            sequence,
            sequence,
            bids=(PriceLevel(Decimal(price), Decimal("1")),),
            asks=(PriceLevel(Decimal(str(int(price) + 1)), Decimal("1")),),
        )

    async def feed(market_event: MarketEvent) -> None:
        await process_market_event(
            market_event,
            book_manager=manager,
            detector=detector,
            store=store,
            broadcaster=broadcaster,
        )

    await feed(event(EventKind.SNAPSHOT, 1))
    await broadcaster.flush()
    await asyncio.sleep(0)
    socket.sent.clear()

    await feed(event(EventKind.DELTA, 5))  # gap: clears the book
    for sequence in range(6, 206):  # every one of these is refused as book_stale
        await feed(event(EventKind.DELTA, sequence))
    await broadcaster.flush()
    await asyncio.sleep(0)

    assert len(socket.sent) == 1
    assert socket.sent[0]["payload"]["eligible"] is False
    assert not socket.closed

    # Recovery must still get through: the status genuinely changed.
    await feed(event(EventKind.SNAPSHOT, 300, "150"))
    await broadcaster.flush()
    await asyncio.sleep(0)
    types = [payload["type"] for payload in socket.sent[1:]]
    assert "book_status" in types and "top_of_book" in types
    book = manager.top_of_book("gemini", "BTC-USD")
    assert book is not None and book.best_bid_price == Decimal("150")

    await broadcaster.aclose()
    await broadcaster.disconnect(socket)


def test_status_display_signature_matches_payload_based_signature() -> None:
    """The typed and payload-based suppression signatures must not drift apart.

    `broadcast_status` compares `BookEligibility.display_signature()` so an
    unchanged status never builds a payload, while the generic book path still
    derives its signature from payload fields. Two sources of truth for one
    decision only stay correct while they agree.
    """
    for eligible, reason in ((True, None), (False, "too_old"), (False, "disconnected")):
        status = BookEligibility(
            exchange="gemini",
            pair="BTC-USD",
            initialized=True,
            continuous=True,
            connected=eligible,
            age_ns=1_000_000,
            max_age_ns=30_000_000_000,
            eligible=eligible,
            reason=reason,
        )
        message = LiveMessage(type="book_status", payload=status.as_payload())
        assert status.display_signature() == broadcast_module._display_signature(message)


@pytest.mark.asyncio
async def test_aclose_is_terminal_for_the_flush_loop() -> None:
    """A coalesced message after aclose must not respawn the flush loop."""
    broadcaster = LiveBroadcaster(coalesce_interval=60)
    socket = _RecordingSocket()
    await broadcaster.connect(socket)
    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"best_bid_price": "1"})
    )
    assert broadcaster._flush_task is not None

    await broadcaster.aclose()
    await asyncio.sleep(0)
    assert [payload["payload"]["best_bid_price"] for payload in socket.sent] == ["1"]

    await broadcaster.broadcast_book(
        "gemini", "BTC-USD", LiveMessage("top_of_book", {"best_bid_price": "2"})
    )
    assert broadcaster._flush_task is None
    await broadcaster.flush()
    await asyncio.sleep(0)
    assert [payload["payload"]["best_bid_price"] for payload in socket.sent] == ["1", "2"]
    await broadcaster.disconnect(socket)
