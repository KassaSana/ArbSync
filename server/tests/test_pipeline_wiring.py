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
    OrderBookConfig,
    PersistenceConfig,
    ReconciliationConfig,
    ServerConfig,
)
from arb.types import BookEligibility, EventKind, MarketEvent, PriceLevel


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
    """Stands in for LiveBroadcaster where only status delivery matters."""

    def __init__(self) -> None:
        self.statuses: list[BookEligibility] = []

    async def broadcast_status(self, status: BookEligibility, *, immediate: bool) -> None:
        assert immediate is True
        self.statuses.append(status)


def test_build_pipeline_wires_configuration_into_each_component(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    pipeline = main.build_pipeline(config, adapter_types=(StubAdapter,), started_at_ns=42)

    assert pipeline.started_at_ns == 42
    assert pipeline.detector.threshold_pct == Decimal("0.25")
    assert pipeline.book_manager._max_age_ns == 12_500_000_000
    assert pipeline.store.batch_size == 7
    assert pipeline.store.flush_interval_seconds == 0.01
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
async def test_reconciler_invalidation_republishes_book_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "LiveBroadcaster", RecordingBroadcaster)
    pipeline = main.build_pipeline(make_config(tmp_path), adapter_types=(StubAdapter,))
    pipeline.book_manager.apply(snapshot("stub", "BTC-USD"))

    status = pipeline.book_manager.invalidate("stub", "BTC-USD")
    assert pipeline.reconciler._on_book_invalidated is not None
    await pipeline.reconciler._on_book_invalidated(status)

    broadcaster: Any = pipeline.broadcaster
    assert [(s.pair, s.eligible) for s in broadcaster.statuses] == [("BTC-USD", False)]


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
        detector=None,  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
        adapters=[FakeAdapter()],  # type: ignore[list-item]
        expected_pairs=[],
        broadcaster=FakeBroadcaster(),  # type: ignore[arg-type]
        supervisor=supervisor,
        reconciler=None,  # type: ignore[arg-type]
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
    assert [task.get_name() for task in (tasks.persistence, tasks.reconciler, *tasks.adapters)] == [
        "persistence",
        "snapshot_reconciler",
        "adapter:stub",
    ]
    await main.shutdown_pipeline(pipeline, tasks)
    assert pipeline.supervisor.failures() == []
