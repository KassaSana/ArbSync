"""Construction and shutdown of the pipeline, without serving HTTP."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from arb import main
from arb.adapters.base import ExchangeAdapter
from arb.config import (
    AppConfig,
    CaptureConfig,
    DetectorConfig,
    FeeSchedule,
    OrderBookConfig,
    PersistenceConfig,
    PricingConfig,
    ReconciliationConfig,
    ServerConfig,
)
from arb.detector import ArbitrageDetector
from arb.types import BookEligibility, EventKind, LiveMessage, MarketEvent, PriceLevel


class StubAdapter(ExchangeAdapter):
    name = "stub"
    ws_url = "wss://example.test"
    snapshot_url = "https://example.test/snapshot"

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return symbol.upper()

    async def subscribe(self, websocket: Any) -> None:
        return None

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return []

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        raise AssertionError("not fetched in these tests")


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        detector=DetectorConfig(threshold_pct=0.25),
        exchanges={"stub": ["btc-usd", "eth-usd"], "gemini": ["btcusd"]},
        server=ServerConfig(
            host="127.0.0.1",
            port=8000,
            database_path=str(tmp_path / "arb.sqlite3"),
            cors_allowed_origins=("https://dashboard.example.test",),
        ),
        persistence=PersistenceConfig(batch_size=7, flush_interval_seconds=0.01, queue_maxsize=11),
        order_books=OrderBookConfig(max_age_seconds=12.5),
        reconciliation=ReconciliationConfig(
            cycle_seconds=90.0,
            confirmation_count=2,
            size_confirmation_count=4,
            cooldown_seconds=30.0,
        ),
        capture=CaptureConfig(queue_maxsize=11),
        pricing=PricingConfig(
            notionals=(Decimal("100"), Decimal("1000")), sample_interval_seconds=0.5
        ),
        fees=FeeSchedule(
            taker_pct={"stub": Decimal("0.1"), "gemini": Decimal("0.4")}, maker_pct={}
        ),
    )


def snapshot(exchange: str, pair: str) -> MarketEvent:
    return MarketEvent(
        exchange=exchange,
        pair=pair,
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=1,
        bids=(PriceLevel(Decimal("100"), Decimal("1")),),
        asks=(PriceLevel(Decimal("101"), Decimal("1")),),
    )


class RecordingBroadcaster:
    """Stands in for LiveBroadcaster where only status and episode delivery matter."""

    def __init__(self) -> None:
        self.statuses: list[BookEligibility] = []
        self.messages: list[LiveMessage] = []

    async def broadcast_status(self, status: BookEligibility, *, immediate: bool) -> None:
        assert immediate is True
        self.statuses.append(status)

    async def broadcast(self, message: LiveMessage) -> None:
        self.messages.append(message)


def test_build_pipeline_wires_configuration_into_each_component(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    pipeline = main.build_pipeline(config, adapter_types=(StubAdapter,), started_at_ns=42)

    assert pipeline.started_at_ns == 42
    assert pipeline.detector.threshold_pct == Decimal("0.25")
    assert pipeline.book_manager._max_age_ns == 12_500_000_000
    assert pipeline.store.batch_size == 7
    assert pipeline.store.flush_interval_seconds == 0.01
    assert pipeline.depth_sampler.notionals == (Decimal("100"), Decimal("1000"))
    assert pipeline.depth_sampler.interval_seconds == 0.5
    assert pipeline.depth_sampler.depth_levels == {"stub": None}
    # Only the registered adapter types are built; other configured exchanges are ignored.
    assert [adapter.name for adapter in pipeline.adapters] == ["stub"]
    assert pipeline.expected_pairs == [("stub", "BTC-USD"), ("stub", "ETH-USD")]
    assert [(t.exchange, t.pair) for t in pipeline.reconciler._states] == pipeline.expected_pairs
    assert pipeline.reconciler.cycle_seconds == 90.0
    assert pipeline.reconciler.confirmation_count == 2
    assert pipeline.reconciler.size_confirmation_count == 4
    assert pipeline.reconciler.cooldown_seconds == 30.0
    assert pipeline.app.title == "Cross-Exchange Arbitrage Detector"


@pytest.mark.asyncio
async def test_adapter_disconnect_republishes_book_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "LiveBroadcaster", RecordingBroadcaster)
    pipeline = main.build_pipeline(make_config(tmp_path), adapter_types=(StubAdapter,))
    pipeline.book_manager.apply(snapshot("stub", "BTC-USD"))
    (adapter,) = pipeline.adapters

    await adapter._report_connection_state(False)

    broadcaster: Any = pipeline.broadcaster
    assert [(s.exchange, s.pair, s.eligible) for s in broadcaster.statuses] == [
        ("stub", "BTC-USD", False)
    ]
    assert pipeline.book_manager.eligibility("stub", "BTC-USD").eligible is False


@pytest.mark.asyncio
async def test_adapter_disconnect_closes_episodes_resting_on_its_books(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ARB-031: a disconnect clears the venue's books without any market event,
    # so the episodes standing on them must be closed here or their lifetime
    # would silently span the outage.
    monkeypatch.setattr(main, "LiveBroadcaster", RecordingBroadcaster)
    pipeline = main.build_pipeline(make_config(tmp_path), adapter_types=(StubAdapter,))
    for exchange in ("stub", "gemini"):
        pipeline.book_manager.apply(snapshot(exchange, "BTC-USD"))
    wide = MarketEvent(
        exchange="gemini",
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=2,
        timestamp_ns=2,
        bids=(PriceLevel(Decimal("103"), Decimal("1")),),
        asks=(PriceLevel(Decimal("104"), Decimal("1")),),
    )
    pipeline.book_manager.apply(wide)
    books = pipeline.book_manager.eligible_books("BTC-USD", 0)
    [opened] = pipeline.detector.detect_for_pair("BTC-USD", books, 1)
    assert opened.route == ("BTC-USD", "stub", "gemini")
    (adapter,) = pipeline.adapters

    await adapter._report_connection_state(False)

    assert pipeline.detector.open_episodes() == []
    broadcaster: Any = pipeline.broadcaster
    [closed] = [m for m in broadcaster.messages if m.type == "opportunity"]
    assert closed.payload["close_reason"] == "book_ineligible"
    assert closed.payload["start_ns"] == "1"
    assert pipeline.store.unflushed_count == 1


@pytest.mark.asyncio
async def test_reconciler_invalidation_republishes_book_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "LiveBroadcaster", RecordingBroadcaster)
    pipeline = main.build_pipeline(make_config(tmp_path), adapter_types=(StubAdapter,))
    pipeline.book_manager.apply(snapshot("stub", "BTC-USD"))
    pipeline.book_manager.apply(snapshot("gemini", "BTC-USD"))
    wide = MarketEvent(
        exchange="gemini",
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=2,
        timestamp_ns=2,
        bids=(PriceLevel(Decimal("103"), Decimal("1")),),
        asks=(PriceLevel(Decimal("104"), Decimal("1")),),
    )
    pipeline.book_manager.apply(wide)
    books = pipeline.book_manager.eligible_books("BTC-USD", 0)
    assert len(pipeline.detector.detect_for_pair("BTC-USD", books, 1)) == 1

    status = pipeline.book_manager.invalidate("stub", "BTC-USD")
    assert pipeline.reconciler._on_book_invalidated is not None
    await pipeline.reconciler._on_book_invalidated(status)

    broadcaster: Any = pipeline.broadcaster
    assert [(s.pair, s.eligible) for s in broadcaster.statuses] == [("BTC-USD", False)]
    assert pipeline.detector.open_episodes() == []
    [closed] = [message for message in broadcaster.messages if message.type == "opportunity"]
    assert closed.payload["close_reason"] == "book_ineligible"
    assert pipeline.store.unflushed_count == 1


@pytest.mark.asyncio
async def test_age_expiry_without_market_event_propagates_only_affected_transition(
    tmp_path: Path,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, payload: dict[str, object]) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            return None

    pipeline = main.build_pipeline(make_config(tmp_path), adapter_types=(StubAdapter,))
    client = Client()
    await pipeline.broadcaster.connect(client)  # type: ignore[arg-type]
    now = 0
    pipeline.book_manager._clock = lambda: now
    pipeline.book_manager.apply(snapshot("stub", "BTC-USD"), received_monotonic_ns=0)
    pipeline.book_manager.apply(
        MarketEvent(
            exchange="gemini",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=1,
            timestamp_ns=1,
            bids=(PriceLevel(Decimal("103"), Decimal("1")),),
            asks=(PriceLevel(Decimal("104"), Decimal("1")),),
        ),
        received_monotonic_ns=0,
    )
    now = 12_000_000_000
    pipeline.book_manager.apply(
        MarketEvent(
            exchange="gemini",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=2,
            timestamp_ns=2,
            bids=(PriceLevel(Decimal("103"), Decimal("1")),),
            asks=(PriceLevel(Decimal("104"), Decimal("1")),),
        ),
        received_monotonic_ns=now,
    )
    pipeline.book_manager.apply(snapshot("stub", "ETH-USD"), received_monotonic_ns=now)
    books = pipeline.book_manager.eligible_books("BTC-USD", now)
    [opened] = pipeline.detector.detect_for_pair("BTC-USD", books, 1, now)
    assert opened.route == ("BTC-USD", "stub", "gemini")
    await pipeline.eligibility_publisher.scan_once(now_monotonic_ns=now, detected_at_ns=1)
    await asyncio.sleep(0)

    client.sent.clear()
    now = 13_000_000_000
    await pipeline.eligibility_publisher.scan_once(now_monotonic_ns=now, detected_at_ns=2)
    await asyncio.sleep(0)

    assert pipeline.book_manager.eligibility("stub", "BTC-USD", now).eligible is False
    assert pipeline.book_manager.eligibility("stub", "ETH-USD", now).eligible is True
    [status] = [message for message in client.sent if message["type"] == "book_status"]
    assert status["payload"]["pair"] == "BTC-USD"
    assert status["payload"]["eligible"] is False
    assert status["payload"]["reason"] == "too_old"
    assert pipeline.detector.open_episodes() == []
    [closed] = [message for message in client.sent if message["type"] == "opportunity"]
    assert closed["payload"]["close_reason"] == "book_ineligible"
    assert pipeline.store.unflushed_count == 1
    await pipeline.broadcaster.disconnect(client)  # type: ignore[arg-type]
    await pipeline.broadcaster.aclose()


@pytest.mark.asyncio
async def test_shutdown_stops_producers_before_consumers_and_drains_persistence() -> None:
    log: list[str] = []
    store_closed = asyncio.Event()

    class FakeStore:
        async def close(self) -> None:
            log.append("store.close")
            store_closed.set()

    class FakeAdapter:
        async def aclose(self) -> None:
            log.append("adapter.aclose")

    class FakeBroadcaster:
        async def aclose(self) -> None:
            log.append("broadcaster.aclose")

    async def until_cancelled(name: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            log.append(f"{name}.cancelled")
            raise

    async def persistence_worker() -> None:
        await store_closed.wait()
        log.append("persistence.drained")

    supervisor = main.BackgroundTaskSupervisor()
    tasks = main.PipelineTasks(
        persistence=supervisor.create("persistence", persistence_worker()),
        reconciler=supervisor.create("snapshot_reconciler", until_cancelled("reconciler")),
        adapters=[supervisor.create("adapter:stub", until_cancelled("adapter"))],
    )
    await asyncio.sleep(0)
    pipeline: Any = main.Pipeline(
        config=None,  # type: ignore[arg-type]
        started_at_ns=0,
        book_manager=None,  # type: ignore[arg-type]
        detector=ArbitrageDetector(Decimal("0.1")),
        store=FakeStore(),  # type: ignore[arg-type]
        adapters=[FakeAdapter()],  # type: ignore[list-item]
        expected_pairs=[],
        broadcaster=FakeBroadcaster(),  # type: ignore[arg-type]
        eligibility_publisher=None,  # type: ignore[arg-type]
        supervisor=supervisor,
        reconciler=None,  # type: ignore[arg-type]
        depth_sampler=None,  # type: ignore[arg-type]
        app=None,  # type: ignore[arg-type]
    )

    await main.shutdown_pipeline(pipeline, tasks)

    assert log == [
        "adapter.cancelled",
        "reconciler.cancelled",
        "broadcaster.aclose",
        "adapter.aclose",
        "store.close",
        "persistence.drained",
    ]
    assert all(task.done() for task in (tasks.persistence, tasks.reconciler, *tasks.adapters))
    # Cancellation during an orderly shutdown is not a background failure.
    assert supervisor.failures() == []


@pytest.mark.asyncio
async def test_start_pipeline_initializes_store_before_any_task_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []

    async def fake_consume_adapter(adapter: ExchangeAdapter, **kwargs: object) -> None:
        log.append(f"consume:{adapter.name}")

    async def fake_reconciler_run() -> None:
        log.append("reconciler.run")

    monkeypatch.setattr(main, "consume_adapter", fake_consume_adapter)
    pipeline = main.build_pipeline(make_config(tmp_path), adapter_types=(StubAdapter,))
    original_initialize = pipeline.store.initialize

    async def logged_initialize() -> None:
        log.append("store.initialize")
        await original_initialize()

    monkeypatch.setattr(pipeline.store, "initialize", logged_initialize)
    monkeypatch.setattr(pipeline.reconciler, "run", fake_reconciler_run)

    tasks = await main.start_pipeline(pipeline)
    await asyncio.sleep(0)

    assert log[0] == "store.initialize"
    assert set(log[1:]) == {"consume:stub", "reconciler.run"}
    assert tasks.depth_sampler is not None
    assert tasks.eligibility_monitor is not None
    assert [
        task.get_name()
        for task in (
            tasks.persistence,
            tasks.reconciler,
            *tasks.adapters,
            tasks.depth_sampler,
            tasks.eligibility_monitor,
        )
    ] == [
        "persistence",
        "snapshot_reconciler",
        "adapter:stub",
        "depth_sampler",
        "eligibility_monitor",
    ]
    await main.shutdown_pipeline(pipeline, tasks)
    assert pipeline.supervisor.failures() == []
