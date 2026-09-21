import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from arb import main
from arb.broadcast import LiveBroadcaster
from arb.detector import ArbitrageDetector
from arb.orderbook import OrderBookManager
from arb.types import BookEligibility, EventKind, LiveMessage, MarketEvent, PriceLevel, TopOfBook


def snapshot(exchange: str, bid: str = "100", ask: str = "101") -> MarketEvent:
    return MarketEvent(
        exchange=exchange,
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=1,
        bids=(PriceLevel(Decimal(bid), Decimal("1")),),
        asks=(PriceLevel(Decimal(ask), Decimal("1")),),
    )


def top_of_book(manager: OrderBookManager, exchange: str, pair: str = "BTC-USD") -> TopOfBook:
    book = manager.top_of_book(exchange, pair)
    assert book is not None
    return book


def delta(exchange: str, sequence: int, bid: str = "100") -> MarketEvent:
    return MarketEvent(
        exchange=exchange,
        pair="BTC-USD",
        kind=EventKind.DELTA,
        sequence=sequence,
        timestamp_ns=sequence,
        bids=(PriceLevel(Decimal(bid), Decimal("1")),),
    )


class RecordingBroadcaster:
    """Records deliveries in order across the immediate and coalesced paths.

    Book updates and opportunities reach the dashboard through different
    broadcaster methods, so a single ordered log is what the pipeline's
    delivery contract is actually about.
    """

    def __init__(self) -> None:
        self.messages: list[LiveMessage] = []
        self.books: list[tuple[str, str, LiveMessage]] = []

    async def broadcast(self, message: LiveMessage) -> None:
        self.messages.append(message)

    async def broadcast_book(self, exchange: str, pair: str, message: LiveMessage) -> None:
        self.books.append((exchange, pair, message))
        self.messages.append(message)

    async def broadcast_book_now(self, exchange: str, pair: str, message: LiveMessage) -> None:
        self.books.append((exchange, pair, message))
        self.messages.append(message)

    async def broadcast_status(self, status: BookEligibility, *, immediate: bool) -> None:
        message = LiveMessage(type="book_status", payload=status.as_payload())
        self.books.append((status.exchange, status.pair, message))
        self.messages.append(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("enqueue_accepted", [True, False])
async def test_processing_order_and_payloads(
    monkeypatch: pytest.MonkeyPatch, enqueue_accepted: bool
) -> None:
    manager = OrderBookManager()
    manager.apply(snapshot("coinbase", "103", "104"))
    manager.apply(snapshot("binance", "105", "106"))
    event = snapshot("gemini")
    detector = ArbitrageDetector(Decimal("0.1"))
    books = Mock(wraps=manager)
    detection = Mock(wraps=detector)
    store = Mock(enqueue=AsyncMock(return_value=enqueue_accepted))
    broadcaster = RecordingBroadcaster()
    for name in (
        "book_metrics",
        "detection_latency_seconds",
        "opportunity_counter",
    ):
        metric = Mock()
        monkeypatch.setattr(main, name, metric)
    clock = Mock()
    clock.perf_counter.side_effect = [10.0, 10.25]
    clock.time_ns.return_value = 123
    clock.monotonic_ns.side_effect = [123, 123]
    monkeypatch.setattr(main, "time", clock)

    await main.process_market_event(
        event,
        book_manager=books,
        detector=detection,
        store=store,
        broadcaster=broadcaster,
    )

    # eligible_books orders venues by exchange name, so the detector sees the
    # same list regardless of which venue happened to update last.
    pair_books = [
        top_of_book(manager, exchange, event.pair) for exchange in ("binance", "coinbase", "gemini")
    ]
    event_book = top_of_book(manager, event.exchange, event.pair)
    # The wrapped detector already holds these episodes open; a fresh one
    # shows what the pipeline should have been handed.
    opportunities = ArbitrageDetector(Decimal("0.1")).detect_for_pair(event.pair, pair_books, 123)
    assert len(opportunities) == 3
    books.apply.assert_called_once_with(event, received_monotonic_ns=123)
    detection.detect_for_pair.assert_called_once_with("BTC-USD", pair_books, 123, 123)
    assert store.enqueue.await_count == 3
    assert len(broadcaster.messages) == 5
    assert broadcaster.messages[0] == LiveMessage("top_of_book", event_book.as_payload())
    assert broadcaster.messages[1].type == "book_status"
    assert [message.type for message in broadcaster.messages[2:]] == ["opportunity"] * 3
    assert [(exchange, pair) for exchange, pair, _ in broadcaster.books] == [
        ("gemini", "BTC-USD")
    ] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [EventKind.DELTA, EventKind.SNAPSHOT])
async def test_rejected_or_incomplete_event_stops_before_delivery(kind: EventKind) -> None:
    event = MarketEvent("gemini", "BTC-USD", kind, 1, 1)
    detector = Mock()
    detector.close_for_book.return_value = []
    store = Mock(enqueue=AsyncMock())
    broadcaster = RecordingBroadcaster()

    await main.process_market_event(
        event,
        book_manager=OrderBookManager(),
        detector=detector,
        store=store,
        broadcaster=broadcaster,
    )

    detector.detect_for_pair.assert_not_called()
    # The leg is not usable, so anything resting on it is closed instead.
    detector.close_for_book.assert_called_once()
    assert detector.close_for_book.call_args.args[:2] == ("gemini", "BTC-USD")
    store.enqueue.assert_not_awaited()
    assert len(broadcaster.messages) == 1
    status_message = broadcaster.messages[0]
    assert status_message.type == "book_status"
    assert status_message.payload["eligible"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("second_exchange", [False, True])
async def test_no_opportunity_still_broadcasts_book(second_exchange: bool) -> None:
    manager = OrderBookManager()
    if second_exchange:
        manager.apply(snapshot("coinbase"))
    store = Mock(enqueue=AsyncMock())
    broadcaster = RecordingBroadcaster()

    await main.process_market_event(
        snapshot("gemini"),
        book_manager=manager,
        detector=ArbitrageDetector(Decimal("0.1")),
        store=store,
        broadcaster=broadcaster,
    )

    store.enqueue.assert_not_awaited()
    assert (
        LiveMessage("top_of_book", top_of_book(manager, "gemini", "BTC-USD").as_payload())
        in broadcaster.messages
    )
    assert len(broadcaster.messages) == 2


@pytest.mark.asyncio
async def test_slow_browser_does_not_block_detection() -> None:
    release_send = asyncio.Event()

    class SlowWebSocket:
        async def accept(self) -> None:
            return None

        async def send_json(self, payload: dict[str, object]) -> None:
            await release_send.wait()

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            return None

    manager = OrderBookManager()
    manager.apply(snapshot("coinbase", "103", "104"))
    manager.apply(snapshot("binance", "105", "106"))
    store = Mock(enqueue=AsyncMock())
    broadcaster = LiveBroadcaster(queue_maxsize=1)
    await broadcaster.connect(SlowWebSocket())

    await asyncio.wait_for(
        main.process_market_event(
            snapshot("gemini"),
            book_manager=manager,
            detector=ArbitrageDetector(Decimal("0.1")),
            store=store,
            broadcaster=broadcaster,
        ),
        timeout=0.1,
    )

    assert store.enqueue.await_count == 3
    release_send.set()


@pytest.mark.asyncio
async def test_consumer_continues_after_rejected_event() -> None:
    rejected = MarketEvent("gemini", "BTC-USD", EventKind.DELTA, 1, 1)
    accepted = snapshot("gemini")

    async def events() -> AsyncIterator[MarketEvent]:
        yield rejected
        yield accepted

    adapter = Mock()
    adapter.connect.return_value = events()
    adapter.request_reconnect = Mock()
    broadcaster = RecordingBroadcaster()
    manager = OrderBookManager()
    await main.consume_adapter(
        adapter,
        book_manager=manager,
        detector=ArbitrageDetector(Decimal("0.1")),
        store=Mock(enqueue=AsyncMock()),
        broadcaster=broadcaster,
    )

    assert (
        LiveMessage("top_of_book", top_of_book(manager, "gemini", "BTC-USD").as_payload())
        in broadcaster.messages
    )
    assert len(broadcaster.messages) == 3
    adapter.request_reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_consumer_requests_adapter_resync_after_invalid_snapshot() -> None:
    invalid = MarketEvent(
        "gemini",
        "BTC-USD",
        EventKind.SNAPSHOT,
        1,
        1,
        bids=(PriceLevel(Decimal("100"), Decimal("1")),),
    )

    async def events() -> AsyncIterator[MarketEvent]:
        yield invalid

    adapter = Mock()
    adapter.name = "gemini"
    adapter.connect.return_value = events()
    adapter.request_reconnect = Mock()
    broadcaster = RecordingBroadcaster()

    await main.consume_adapter(
        adapter,
        book_manager=OrderBookManager(),
        detector=ArbitrageDetector(Decimal("0.1")),
        store=Mock(enqueue=AsyncMock()),
        broadcaster=broadcaster,
    )

    adapter.request_reconnect.assert_called_once_with()
    assert len(broadcaster.messages) == 1
    assert broadcaster.messages[0].payload["eligible"] is False


@pytest.mark.asyncio
async def test_consumer_prefers_single_pair_resync() -> None:
    # ARB-028: consume_adapter routes book-level resync through the
    # single-pair hook when the adapter supports it, leaving the shared
    # connection alone.
    from arb.adapters.base import ExchangeAdapter

    invalid = MarketEvent(
        "stub",
        "BTC-USD",
        EventKind.SNAPSHOT,
        1,
        1,
        bids=(PriceLevel(Decimal("100"), Decimal("1")),),
    )

    class PairCapableAdapter(ExchangeAdapter):
        name = "stub"
        ws_url = "wss://example.test"
        snapshot_url = "https://example.test/snapshot"

        def __init__(self) -> None:
            super().__init__(["BTC-USD"])
            self.pair_resync_calls: list[str] = []
            self.reconnect_calls = 0

        @staticmethod
        def normalize_symbol(symbol: str) -> str:
            return symbol

        async def subscribe(self, websocket: object) -> None:
            return None

        async def parse_message(self, message: str) -> list[MarketEvent]:
            return []

        async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
            raise NotImplementedError

        async def connect(self) -> AsyncGenerator[MarketEvent, None]:
            yield invalid

        def request_pair_resync(self, pair: str) -> bool:
            self.pair_resync_calls.append(pair)
            return True

        def request_reconnect(self) -> None:
            self.reconnect_calls += 1

    adapter = PairCapableAdapter()
    broadcaster = RecordingBroadcaster()

    await main.consume_adapter(
        adapter,
        book_manager=OrderBookManager(),
        detector=ArbitrageDetector(Decimal("0.1")),
        store=Mock(enqueue=AsyncMock()),
        broadcaster=broadcaster,
    )

    assert adapter.pair_resync_calls == ["BTC-USD"]
    assert adapter.reconnect_calls == 0
    assert len(broadcaster.messages) == 1
    assert broadcaster.messages[0].payload["eligible"] is False


@pytest.mark.asyncio
async def test_event_receipt_time_drives_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    event = MarketEvent(
        **{
            **snapshot("gemini").__dict__,
            "received_monotonic_ns": 1_000,
        }
    )
    clock = Mock()
    clock.monotonic_ns.return_value = 9_000
    clock.perf_counter.side_effect = [1.0, 1.1]
    clock.time_ns.return_value = 123
    monkeypatch.setattr(main, "time", clock)
    for name in (
        "book_metrics",
        "detection_latency_seconds",
        "opportunity_counter",
    ):
        monkeypatch.setattr(main, name, Mock())
    manager = OrderBookManager(clock=lambda: 9_000)

    await main.process_market_event(
        event,
        book_manager=manager,
        detector=ArbitrageDetector(Decimal("0.1")),
        store=Mock(enqueue=AsyncMock()),
        broadcaster=RecordingBroadcaster(),
    )

    assert manager.eligibility("gemini", "BTC-USD", 9_000).age_ns == 8_000
    clock.monotonic_ns.assert_called_once_with()


@pytest.mark.asyncio
async def test_processing_delay_can_make_received_event_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = MarketEvent(
        **{
            **snapshot("gemini").__dict__,
            "received_monotonic_ns": 1_000,
        }
    )
    clock = Mock()
    clock.monotonic_ns.return_value = 2_001
    monkeypatch.setattr(main, "time", clock)
    for name in (
        "book_metrics",
        "detection_latency_seconds",
        "opportunity_counter",
    ):
        monkeypatch.setattr(main, name, Mock())
    broadcaster = RecordingBroadcaster()
    detector = Mock()
    detector.close_for_book.return_value = []

    await main.process_market_event(
        event,
        book_manager=OrderBookManager(max_age_seconds=0.000001),
        detector=detector,
        store=Mock(enqueue=AsyncMock()),
        broadcaster=broadcaster,
    )

    detector.detect_for_pair.assert_not_called()
    assert len(broadcaster.messages) == 1
    message = broadcaster.messages[0]
    assert message.type == "book_status"
    assert message.payload["reason"] == "too_old"


@pytest.mark.asyncio
async def test_background_task_supervisor_records_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged = Mock()
    metric = Mock()
    monkeypatch.setattr(main, "logger", Mock(error=logged))
    monkeypatch.setattr(main, "background_task_failures_total", metric)
    supervisor = main.BackgroundTaskSupervisor()

    async def fail() -> object:
        raise RuntimeError("boom")

    task = supervisor.create("adapter:gemini", fail())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert supervisor.failures() == [{"task": "adapter:gemini", "error": "RuntimeError('boom')"}]
    metric.labels.assert_called_once_with(task="adapter:gemini")
    metric.labels.return_value.inc.assert_called_once_with()
    logged.assert_called_once_with(
        "background_task_failed",
        task="adapter:gemini",
        error="RuntimeError('boom')",
    )


@pytest.mark.asyncio
async def test_background_task_supervisor_ignores_shutdown_cancellation() -> None:
    supervisor = main.BackgroundTaskSupervisor()

    async def wait_forever() -> object:
        await asyncio.Event().wait()
        return None

    task = supervisor.create("persistence", wait_forever())
    supervisor.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert supervisor.failures() == []


@pytest.mark.asyncio
async def test_buffered_burst_allows_dashboard_sender_to_drain() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.closed = False

        async def accept(self) -> None:
            pass

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.closed = True

    socket = Socket()
    broadcaster = LiveBroadcaster(queue_maxsize=8)
    await broadcaster.connect(socket)
    manager = OrderBookManager()
    detector = ArbitrageDetector(Decimal("0.1"))
    for _ in range(100):
        await main.process_market_event(
            snapshot("gemini"),
            book_manager=manager,
            detector=detector,
            store=Mock(enqueue=AsyncMock()),
            broadcaster=broadcaster,
        )
    await broadcaster.flush()
    await asyncio.sleep(0)

    # Coalescing bounds a burst by book count, not event count, so the queue
    # cannot overflow and the client survives with the newest state.
    assert not socket.closed
    assert len(socket.sent) <= 8
    types = [payload["type"] for payload in socket.sent]
    assert "top_of_book" in types and "book_status" in types
    top = [payload for payload in socket.sent if payload["type"] == "top_of_book"][-1]
    assert top["payload"] == top_of_book(manager, "gemini", "BTC-USD").as_payload()
    await broadcaster.aclose()
    await broadcaster.disconnect(socket)


@pytest.mark.asyncio
async def test_leg_turning_ineligible_closes_its_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    # ARB-031: an episode's lifetime ends when a leg stops being trusted, not
    # only when the spread narrows. The adapter's RESET (ARB-028) is one such
    # path: the pipeline skips detection for it, so the close must come from
    # the ineligible branch.
    from arb.types import EventKind as Kind

    metrics = {
        name: Mock()
        for name in ("book_metrics", "detection_latency_seconds", "opportunity_counter")
    }
    for name, mock in metrics.items():
        monkeypatch.setattr(main, name, mock)
    manager = OrderBookManager()
    detector = ArbitrageDetector(Decimal("0.1"))
    store = Mock(enqueue=AsyncMock(return_value=True))
    broadcaster = RecordingBroadcaster()
    process = partial(
        main.process_market_event,
        book_manager=manager,
        detector=detector,
        store=store,
        broadcaster=broadcaster,
    )

    await process(snapshot("coinbase", "103", "104"))
    await process(snapshot("gemini", "100", "101"))
    [opened] = [m for m in broadcaster.messages if m.type == "opportunity"]
    assert opened.payload["buy_exchange"] == "gemini"
    assert opened.payload["end_ns"] is None
    assert len(detector.open_episodes()) == 1

    reset = MarketEvent("gemini", "BTC-USD", Kind.RESET, 1, 2)
    await process(reset)

    assert detector.open_episodes() == []
    closes = [m for m in broadcaster.messages if m.type == "opportunity"][1:]
    assert len(closes) == 1
    assert closes[0].payload["close_reason"] == "book_ineligible"
    assert closes[0].payload["start_ns"] == opened.payload["start_ns"]
    assert store.enqueue.await_count == 2
    # Only the open counted as a new opportunity.
    assert metrics["opportunity_counter"].call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sequence", [1, 0], ids=["duplicate", "older"])
async def test_rejected_old_delta_keeps_healthy_episode_open(
    monkeypatch: pytest.MonkeyPatch, sequence: int
) -> None:
    metrics = {
        name: Mock()
        for name in ("book_metrics", "detection_latency_seconds", "opportunity_counter")
    }
    for name, mock in metrics.items():
        monkeypatch.setattr(main, name, mock)
    manager = OrderBookManager()
    detector = ArbitrageDetector(Decimal("0.1"))
    store = Mock(enqueue=AsyncMock(return_value=True))
    broadcaster = RecordingBroadcaster()
    publisher = main.BookEligibilityPublisher(manager, detector, store, broadcaster, ())
    process = partial(
        main.process_market_event,
        book_manager=manager,
        detector=detector,
        store=store,
        broadcaster=broadcaster,
        eligibility_publisher=publisher,
    )

    await process(snapshot("coinbase", "103", "104"))
    await process(snapshot("gemini", "100", "101"))
    [opened] = detector.open_episodes()

    result = await process(delta("gemini", sequence))

    assert result.accepted is False
    assert result.reason == "out_of_order"
    assert manager.eligibility("gemini", "BTC-USD").eligible is True
    assert detector.open_episodes() == [opened]
    assert [
        message.payload["end_ns"]
        for message in broadcaster.messages
        if message.type == "opportunity"
    ] == [None]
    assert metrics["book_metrics"].return_value.eligible.set.call_args.args == (1,)


@pytest.mark.asyncio
async def test_sequence_gap_closes_episode_and_snapshot_recovery_reopens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = {
        name: Mock()
        for name in ("book_metrics", "detection_latency_seconds", "opportunity_counter")
    }
    for name, mock in metrics.items():
        monkeypatch.setattr(main, name, mock)
    manager = OrderBookManager()
    detector = ArbitrageDetector(Decimal("0.1"))
    store = Mock(enqueue=AsyncMock(return_value=True))
    broadcaster = RecordingBroadcaster()
    publisher = main.BookEligibilityPublisher(manager, detector, store, broadcaster, ())
    process = partial(
        main.process_market_event,
        book_manager=manager,
        detector=detector,
        store=store,
        broadcaster=broadcaster,
        eligibility_publisher=publisher,
    )

    await process(snapshot("coinbase", "103", "104"))
    await process(snapshot("gemini", "100", "101"))

    gap = await process(delta("gemini", 3))

    assert gap.accepted is False
    assert gap.reason == "sequence_gap"
    assert manager.eligibility("gemini", "BTC-USD").eligible is False
    assert detector.open_episodes() == []
    closed = [message for message in broadcaster.messages if message.type == "opportunity"][-1]
    assert closed.payload["close_reason"] == "book_ineligible"
    assert metrics["book_metrics"].return_value.eligible.set.call_args.args == (0,)

    recovered = await process(snapshot("gemini", "100", "101"))

    assert recovered.accepted is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True
    assert len(detector.open_episodes()) == 1
    reopened = [message for message in broadcaster.messages if message.type == "opportunity"][-1]
    assert reopened.payload["end_ns"] is None
    assert metrics["book_metrics"].return_value.eligible.set.call_args.args == (1,)


@pytest.mark.asyncio
async def test_shutdown_closes_open_episodes_before_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = {
        name: Mock()
        for name in ("book_metrics", "detection_latency_seconds", "opportunity_counter")
    }
    for name, mock in metrics.items():
        monkeypatch.setattr(main, name, mock)
    detector = ArbitrageDetector(Decimal("0.1"))
    manager = OrderBookManager()
    store = Mock(enqueue=AsyncMock(return_value=True))
    broadcaster = RecordingBroadcaster()
    process = partial(
        main.process_market_event,
        book_manager=manager,
        detector=detector,
        store=store,
        broadcaster=broadcaster,
    )
    await process(snapshot("coinbase", "103", "104"))
    await process(snapshot("gemini", "100", "101"))

    await main.deliver_episodes(detector.close_all(7), store=store, broadcaster=broadcaster)

    [closed] = [m for m in broadcaster.messages if m.type == "opportunity"][1:]
    assert closed.payload["close_reason"] == "shutdown"
    assert detector.open_episodes() == []
