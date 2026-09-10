from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Coroutine
from decimal import Decimal
from typing import Any

import structlog
import uvicorn

from arb.adapters.base import ExchangeAdapter
from arb.adapters.binance import BinanceAdapter
from arb.adapters.coinbase import CoinbaseAdapter
from arb.adapters.gemini import GeminiAdapter
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.config import load_config
from arb.detector import ArbitrageDetector
from arb.metrics import (
    background_task_failures_total,
    book_eligible,
    book_staleness_seconds,
    book_updates_total,
    detection_latency_seconds,
    events_ingested_total,
    opportunities_total,
)
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.reconcile import SnapshotReconciler
from arb.types import LiveMessage, MarketEvent

logger = structlog.get_logger(__name__)


class BackgroundTaskSupervisor:
    def __init__(self) -> None:
        self._failures: dict[str, str] = {}
        self._stopped = False

    def create(self, name: str, coroutine: Coroutine[Any, Any, object]) -> asyncio.Task[object]:
        task = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(lambda completed: self._task_done(name, completed))
        return task

    def failures(self) -> list[dict[str, str]]:
        return [{"task": name, "error": error} for name, error in sorted(self._failures.items())]

    def stop(self) -> None:
        self._stopped = True

    def _task_done(self, name: str, task: asyncio.Task[object]) -> None:
        if self._stopped or task.cancelled():
            return
        exception = task.exception()
        error = "task exited unexpectedly" if exception is None else repr(exception)
        self._failures[name] = error
        background_task_failures_total.labels(task=name).inc()
        logger.error("background_task_failed", task=name, error=error)


def configure_logging() -> None:
    level_name = os.getenv("ARB_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def process_market_event(
    event: MarketEvent,
    *,
    book_manager: OrderBookManager,
    detector: ArbitrageDetector,
    store: OpportunityStore,
    broadcaster: LiveBroadcaster,
) -> None:
    """Apply one event, publish its book, then detect and deliver opportunities."""
    received_monotonic_ns = (
        event.received_monotonic_ns
        if event.received_monotonic_ns is not None
        else time.monotonic_ns()
    )
    events_ingested_total.labels(exchange=event.exchange).inc()
    result = book_manager.apply(event, received_monotonic_ns=received_monotonic_ns)
    eligibility_checked_ns = time.monotonic_ns()
    status = book_manager.eligibility(event.exchange, event.pair, eligibility_checked_ns)
    if not result.accepted or result.top_of_book is None:
        book_eligible.labels(exchange=event.exchange, pair=event.pair).set(0)
        await broadcaster.broadcast_book_now(
            event.exchange, event.pair, LiveMessage(type="book_status", payload=status.as_payload())
        )
        return

    book_updates_total.labels(exchange=event.exchange, pair=event.pair).inc()
    if status.age_ns is not None:
        book_staleness_seconds.labels(exchange=event.exchange, pair=event.pair).set(
            status.age_ns / 1_000_000_000
        )
    book_eligible.labels(exchange=event.exchange, pair=event.pair).set(1 if status.eligible else 0)
    if not status.eligible:
        await broadcaster.broadcast_book_now(
            event.exchange, event.pair, LiveMessage(type="book_status", payload=status.as_payload())
        )
        return

    await broadcaster.broadcast_book(
        event.exchange,
        event.pair,
        LiveMessage(type="top_of_book", payload=result.top_of_book.as_payload()),
    )
    await broadcaster.broadcast_book(
        event.exchange, event.pair, LiveMessage(type="book_status", payload=status.as_payload())
    )
    pair_books = book_manager.eligible_books(event.pair, eligibility_checked_ns)
    detect_started = time.perf_counter()
    opportunities = detector.detect_for_pair(event.pair, pair_books, time.time_ns())
    detection_latency_seconds.observe(time.perf_counter() - detect_started)
    for opportunity in opportunities:
        opportunities_total.labels(pair=opportunity.pair).inc()
        await store.enqueue(opportunity)
        await broadcaster.broadcast(
            LiveMessage(type="opportunity", payload=opportunity.as_payload())
        )
    # Buffered socket reads and put_nowait-based delivery may otherwise run a
    # whole burst without yielding. Let senders and persistence drain their
    # bounded queues before consuming another update.
    await asyncio.sleep(0)


async def consume_adapter(
    adapter: ExchangeAdapter,
    *,
    book_manager: OrderBookManager,
    detector: ArbitrageDetector,
    store: OpportunityStore,
    broadcaster: LiveBroadcaster,
) -> None:
    """Process each normalized event from an adapter in sequence."""
    async for event in adapter.connect():
        await process_market_event(
            event,
            book_manager=book_manager,
            detector=detector,
            store=store,
            broadcaster=broadcaster,
        )


async def run_pipeline() -> None:
    configure_logging()
    config = load_config()
    started_at_holder: list[int] = [time.time_ns()]
    book_manager = OrderBookManager(max_age_seconds=config.order_books.max_age_seconds)
    detector = ArbitrageDetector(threshold_pct=Decimal(str(config.detector.threshold_pct)))
    store = OpportunityStore(
        config.server.database_path,
        batch_size=config.persistence.batch_size,
        flush_interval_seconds=config.persistence.flush_interval_seconds,
        queue_maxsize=config.persistence.queue_maxsize,
    )
    adapters = [
        GeminiAdapter(config.exchanges.get("gemini", [])),
        CoinbaseAdapter(config.exchanges.get("coinbase", [])),
        BinanceAdapter(config.exchanges.get("binance", [])),
    ]
    broadcaster = LiveBroadcaster()
    supervisor = BackgroundTaskSupervisor()

    async def report_connection_state(exchange: str, connected: bool) -> None:
        statuses = book_manager.set_exchange_connected(exchange, connected)
        for status in statuses:
            book_eligible.labels(exchange=status.exchange, pair=status.pair).set(
                1 if status.eligible else 0
            )
            await broadcaster.broadcast_book_now(
                status.exchange,
                status.pair,
                LiveMessage(type="book_status", payload=status.as_payload()),
            )

    for adapter in adapters:
        adapter.set_connection_state_callback(report_connection_state)
    expected_pairs = [
        (adapter.name, pair) for adapter in adapters for pair in adapter.expected_pairs()
    ]
    app = create_app(
        store,
        book_manager,
        broadcaster,
        adapters=adapters,
        expected_pairs=expected_pairs,
        started_at_holder=started_at_holder,
        background_failures=supervisor.failures,
        cors_allowed_origins=config.server.cors_allowed_origins,
    )

    await store.initialize()
    persistence_task = supervisor.create("persistence", store.run())
    reconcile_task = supervisor.create(
        "snapshot_reconciler",
        SnapshotReconciler(adapters, book_manager, expected_pairs).run(),
    )

    adapter_tasks = [
        supervisor.create(
            f"adapter:{adapter.name}",
            consume_adapter(
                adapter,
                book_manager=book_manager,
                detector=detector,
                store=store,
                broadcaster=broadcaster,
            ),
        )
        for adapter in adapters
    ]

    config_uvicorn = uvicorn.Config(
        app=app, host=config.server.host, port=config.server.port, log_level="info"
    )
    server = uvicorn.Server(config_uvicorn)
    try:
        await server.serve()
    finally:
        supervisor.stop()
        for task in adapter_tasks:
            task.cancel()
        reconcile_task.cancel()
        await broadcaster.aclose()
        await asyncio.gather(
            *adapter_tasks,
            reconcile_task,
            return_exceptions=True,
        )
        await store.close()
        await persistence_task


if __name__ == "__main__":
    asyncio.run(run_pipeline())
