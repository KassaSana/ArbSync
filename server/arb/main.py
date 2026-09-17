from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import json
import logging
import os
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
from fastapi import FastAPI

from arb.adapters import ADAPTER_TYPES
from arb.adapters.base import ExchangeAdapter, request_scoped_resync
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.capture import CaptureWriter
from arb.config import AppConfig, ConfigError, load_config
from arb.detector import ArbitrageDetector
from arb.metrics import (
    background_task_failures_total,
    book_metrics,
    detection_latency_seconds,
    opportunity_counter,
)
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.reconcile import SnapshotReconciler
from arb.types import (
    BookEligibility,
    BookUpdateResult,
    LiveMessage,
    MarketEvent,
    OpportunityEpisode,
)

if TYPE_CHECKING:
    from arb.replay import ReplayReport

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
    detected_at_ns: int | None = None,
    now_monotonic_ns: int | None = None,
) -> BookUpdateResult:
    """Apply one event, publish its book, then detect and deliver opportunities.

    `detected_at_ns` and `now_monotonic_ns` default to the live clocks and
    exist so offline replay can run the identical path on the recorded
    timeline instead of wall-clock time.
    """
    received_monotonic_ns = (
        event.received_monotonic_ns
        if event.received_monotonic_ns is not None
        else now_monotonic_ns
        if now_monotonic_ns is not None
        else time.monotonic_ns()
    )
    metrics = book_metrics(event.exchange, event.pair)
    metrics.ingested.inc()
    result = book_manager.apply(event, received_monotonic_ns=received_monotonic_ns)
    eligibility_checked_ns = (
        now_monotonic_ns if now_monotonic_ns is not None else time.monotonic_ns()
    )
    status = book_manager.eligibility(event.exchange, event.pair, eligibility_checked_ns)
    detected_ns = detected_at_ns if detected_at_ns is not None else time.time_ns()
    if not result.accepted or result.top_of_book is None:
        metrics.eligible.set(0)
        await broadcaster.broadcast_status(status, immediate=True)
        # This leg just left the eligible set, and detection below is skipped,
        # so any episode standing on it must be closed here.
        await deliver_episodes(
            detector.close_for_book(
                event.exchange, event.pair, detected_ns, eligibility_checked_ns
            ),
            store=store,
            broadcaster=broadcaster,
        )
        return result

    metrics.updates.inc()
    if status.age_ns is not None:
        metrics.staleness.set(status.age_ns / 1_000_000_000)
    metrics.eligible.set(1 if status.eligible else 0)
    if not status.eligible:
        await broadcaster.broadcast_status(status, immediate=True)
        await deliver_episodes(
            detector.close_for_book(
                event.exchange, event.pair, detected_ns, eligibility_checked_ns
            ),
            store=store,
            broadcaster=broadcaster,
        )
        return result

    await broadcaster.broadcast_book(
        event.exchange,
        event.pair,
        LiveMessage(type="top_of_book", payload=result.top_of_book.as_payload()),
    )
    await broadcaster.broadcast_status(status, immediate=False)
    pair_books = book_manager.eligible_books(
        event.pair, eligibility_checked_ns, known=result.top_of_book
    )
    detect_started = time.perf_counter()
    episodes = detector.detect_for_pair(event.pair, pair_books, detected_ns, eligibility_checked_ns)
    detection_latency_seconds.observe(time.perf_counter() - detect_started)
    await deliver_episodes(episodes, store=store, broadcaster=broadcaster)
    # Buffered socket reads and put_nowait-based delivery may otherwise run a
    # whole burst without yielding. Let senders and persistence drain their
    # bounded queues before consuming another update.
    await asyncio.sleep(0)
    return result


async def deliver_episodes(
    episodes: list[OpportunityEpisode], *, store: OpportunityStore, broadcaster: LiveBroadcaster
) -> None:
    """Persist and publish episode open and close events in order."""
    for episode in episodes:
        if episode.is_open:
            opportunity_counter(episode.pair).inc()
        await store.enqueue(episode)
        await broadcaster.broadcast(LiveMessage(type="opportunity", payload=episode.as_payload()))


async def consume_adapter(
    adapter: ExchangeAdapter,
    *,
    book_manager: OrderBookManager,
    detector: ArbitrageDetector,
    store: OpportunityStore,
    broadcaster: LiveBroadcaster,
    on_book_update: Callable[[str, str], None] | None = None,
) -> None:
    """Process each normalized event from an adapter in sequence."""
    async for event in adapter.connect():
        result = await process_market_event(
            event,
            book_manager=book_manager,
            detector=detector,
            store=store,
            broadcaster=broadcaster,
        )
        if result.requires_resync:
            logger.warning(
                "book_resync_requested",
                exchange=event.exchange,
                pair=event.pair,
                reason=result.reason,
            )
            if not request_scoped_resync(adapter, event.pair):
                adapter.request_reconnect()
        if on_book_update is not None:
            on_book_update(event.exchange, event.pair)


@dataclass(frozen=True)
class Pipeline:
    """Every long-lived component, wired but not yet running."""

    config: AppConfig
    started_at_ns: int
    book_manager: OrderBookManager
    detector: ArbitrageDetector
    store: OpportunityStore
    adapters: list[ExchangeAdapter]
    expected_pairs: list[tuple[str, str]]
    broadcaster: LiveBroadcaster
    supervisor: BackgroundTaskSupervisor
    reconciler: SnapshotReconciler
    app: FastAPI


@dataclass(frozen=True)
class PipelineTasks:
    persistence: asyncio.Task[object]
    reconciler: asyncio.Task[object]
    adapters: list[asyncio.Task[object]]


def build_pipeline(
    config: AppConfig,
    *,
    adapter_types: Sequence[type[ExchangeAdapter]] = ADAPTER_TYPES,
    started_at_ns: int | None = None,
) -> Pipeline:
    """Construct and connect every component without starting any task."""
    started_at_ns = time.time_ns() if started_at_ns is None else started_at_ns
    book_manager = OrderBookManager(max_age_seconds=config.order_books.max_age_seconds)
    detector = ArbitrageDetector(threshold_pct=Decimal(str(config.detector.threshold_pct)))
    store = OpportunityStore(
        config.server.database_path,
        batch_size=config.persistence.batch_size,
        flush_interval_seconds=config.persistence.flush_interval_seconds,
        queue_maxsize=config.persistence.queue_maxsize,
    )
    adapters = [
        adapter_type(config.exchanges.get(adapter_type.name, [])) for adapter_type in adapter_types
    ]
    broadcaster = LiveBroadcaster()
    supervisor = BackgroundTaskSupervisor()

    async def publish_book_status(status: BookEligibility) -> None:
        book_metrics(status.exchange, status.pair).eligible.set(1 if status.eligible else 0)
        await broadcaster.broadcast_status(status, immediate=True)

    async def report_connection_state(exchange: str, connected: bool) -> None:
        for status in book_manager.set_exchange_connected(exchange, connected):
            await publish_book_status(status)
            if not connected:
                # The venue's books were just cleared without any market event,
                # so nothing else would close the episodes resting on them.
                await deliver_episodes(
                    detector.close_for_book(exchange, status.pair, time.time_ns()),
                    store=store,
                    broadcaster=broadcaster,
                )

    for adapter in adapters:
        adapter.set_connection_state_callback(report_connection_state)
    expected_pairs = [
        (adapter.name, pair) for adapter in adapters for pair in adapter.expected_pairs()
    ]
    reconciler = SnapshotReconciler(
        adapters,
        book_manager,
        expected_pairs,
        cycle_seconds=config.reconciliation.cycle_seconds,
        confirmation_count=config.reconciliation.confirmation_count,
        size_confirmation_count=config.reconciliation.size_confirmation_count,
        cooldown_seconds=config.reconciliation.cooldown_seconds,
        on_book_invalidated=publish_book_status,
    )
    app = create_app(
        store,
        book_manager,
        broadcaster,
        adapters=adapters,
        expected_pairs=expected_pairs,
        started_at_ns=started_at_ns,
        background_failures=supervisor.failures,
        cors_allowed_origins=config.server.cors_allowed_origins,
    )
    return Pipeline(
        config=config,
        started_at_ns=started_at_ns,
        book_manager=book_manager,
        detector=detector,
        store=store,
        adapters=adapters,
        expected_pairs=expected_pairs,
        broadcaster=broadcaster,
        supervisor=supervisor,
        reconciler=reconciler,
        app=app,
    )


async def start_pipeline(pipeline: Pipeline) -> PipelineTasks:
    """Open the store, then start persistence, reconciliation, and every adapter."""
    await pipeline.store.initialize()
    persistence_task = pipeline.supervisor.create("persistence", pipeline.store.run())
    reconcile_task = pipeline.supervisor.create("snapshot_reconciler", pipeline.reconciler.run())
    adapter_tasks = [
        pipeline.supervisor.create(
            f"adapter:{adapter.name}",
            consume_adapter(
                adapter,
                book_manager=pipeline.book_manager,
                detector=pipeline.detector,
                store=pipeline.store,
                broadcaster=pipeline.broadcaster,
                on_book_update=pipeline.reconciler.observe_book,
            ),
        )
        for adapter in pipeline.adapters
    ]
    return PipelineTasks(
        persistence=persistence_task, reconciler=reconcile_task, adapters=adapter_tasks
    )


async def shutdown_pipeline(pipeline: Pipeline, tasks: PipelineTasks) -> None:
    """Stop producers before consumers so nothing enqueues into a closed component.

    Adapters and the reconciler are cancelled and awaited first: cancellation only
    lands at their next await, and their teardown publishes final disconnected
    statuses. The broadcaster then closes and flushes those, then the adapters'
    shared REST pool closes, then the store. The persistence task is awaited last
    so it can drain what the adapters enqueued before cancellation.
    """
    pipeline.supervisor.stop()
    for task in tasks.adapters:
        task.cancel()
    tasks.reconciler.cancel()
    await asyncio.gather(*tasks.adapters, tasks.reconciler, return_exceptions=True)
    # Nothing can open an episode once the adapters are gone; close the ones
    # still standing so storage never holds an episode with no end.
    await deliver_episodes(
        pipeline.detector.close_all(time.time_ns()),
        store=pipeline.store,
        broadcaster=pipeline.broadcaster,
    )
    await pipeline.broadcaster.aclose()
    # Adapters hold a reused REST pool; close it only once nothing can fetch.
    await asyncio.gather(
        *(adapter.aclose() for adapter in pipeline.adapters), return_exceptions=True
    )
    await pipeline.store.close()
    await tasks.persistence


async def run_capture(
    config_path: str | Path,
    duration_seconds: float,
    output: Path,
    *,
    adapter_types: Sequence[type[ExchangeAdapter]] = ADAPTER_TYPES,
) -> None:
    """Run ingestion only for a fixed duration, recording traffic to disk.

    The HTTP server is not started: the pipeline's adapters, reconciler, and
    store run exactly as in serving mode while every adapter also taps its
    inbound traffic into the capture file. The duration uses asyncio.sleep,
    which follows the monotonic clock.
    """
    configure_logging()
    config = load_config(config_path)
    pipeline = build_pipeline(config, adapter_types=adapter_types)
    writer = CaptureWriter(output, config.exchanges, queue_maxsize=config.capture.queue_maxsize)
    for adapter in pipeline.adapters:
        adapter.set_capture_sink(writer)
    tasks = await start_pipeline(pipeline)
    capture_task = pipeline.supervisor.create("capture", writer.run())
    try:
        await asyncio.sleep(duration_seconds)
    finally:
        await shutdown_pipeline(pipeline, tasks)
        await writer.close()
        await asyncio.gather(capture_task, return_exceptions=True)
    for failure in pipeline.supervisor.failures():
        logger.error("capture_background_failure", **failure)
    logger.info(
        "capture_finished",
        path=str(output),
        frames=writer.frame_count,
        duration_seconds=duration_seconds,
    )


async def run_replay_offline(
    config_path: str | Path, capture_path: Path, speed: float | None
) -> ReplayReport:
    """Replay a capture on the recorded timeline and report the outcome."""
    from arb.replay import replay_file

    config = load_config(config_path)
    return await replay_file(
        capture_path,
        threshold_pct=Decimal(str(config.detector.threshold_pct)),
        max_age_seconds=config.order_books.max_age_seconds,
        speed=speed,
    )


async def feed_replay_into_pipeline(
    pipeline: Pipeline, capture_path: Path, speed: float | None
) -> ReplayReport:
    """Stream a capture through a pipeline's own books, store, and broadcaster.

    The `live` timeline stamps replayed events with the replay machine's
    clocks, so books age and expire like a live venue while paced by
    `--speed`. No adapter task runs, so nothing touches the network.
    """
    from arb.capture import read_capture
    from arb.replay import replay_frames

    header, frames = read_capture(capture_path)
    return await replay_frames(
        header,
        frames,
        speed=speed,
        timeline="live",
        book_manager=pipeline.book_manager,
        detector=pipeline.detector,
        store=pipeline.store,
        broadcaster=pipeline.broadcaster,
    )


async def run_replay_serve(
    config_path: str | Path, capture_path: Path, speed: float | None
) -> None:
    """Serve the dashboard API while streaming a capture with no live backend."""
    configure_logging()
    config = load_config(config_path)
    pipeline = build_pipeline(config)
    await pipeline.store.initialize()
    persistence_task = pipeline.supervisor.create("persistence", pipeline.store.run())
    replay_task = pipeline.supervisor.create(
        "replay", feed_replay_into_pipeline(pipeline, capture_path, speed)
    )
    try:
        await _serve_app(pipeline)
    finally:
        pipeline.supervisor.stop()
        replay_task.cancel()
        await asyncio.gather(replay_task, return_exceptions=True)
        await deliver_episodes(
            pipeline.detector.close_all(time.time_ns()),
            store=pipeline.store,
            broadcaster=pipeline.broadcaster,
        )
        await pipeline.broadcaster.aclose()
        await asyncio.gather(
            *(adapter.aclose() for adapter in pipeline.adapters), return_exceptions=True
        )
        await pipeline.store.close()
        await persistence_task
    for failure in pipeline.supervisor.failures():
        logger.error("replay_background_failure", **failure)


async def run_pipeline(config_path: str | Path = "config.toml") -> None:
    configure_logging()
    config = load_config(config_path)
    pipeline = build_pipeline(config)
    tasks = await start_pipeline(pipeline)
    try:
        await _serve_app(pipeline)
    finally:
        await shutdown_pipeline(pipeline, tasks)


async def _serve_app(pipeline: Pipeline) -> None:
    config_uvicorn = uvicorn.Config(
        app=pipeline.app,
        host=pipeline.config.server.host,
        port=pipeline.config.server.port,
        log_level="info",
    )
    server = uvicorn.Server(config_uvicorn)
    await server.serve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbsync",
        description="Stream public exchange books and report theoretical arbitrage opportunities.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="TOML configuration path (default: ARB_CONFIG, then ./config.toml)",
    )
    parser.add_argument(
        "--init-config",
        type=Path,
        metavar="PATH",
        help="write a safe example configuration to PATH and exit",
    )
    subparsers = parser.add_subparsers(dest="command")
    capture_parser = subparsers.add_parser(
        "capture",
        help="record real exchange traffic to a file without serving the dashboard",
    )
    capture_parser.add_argument(
        "--duration",
        required=True,
        help="how long to record, e.g. 90s, 10m, 1h (plain numbers are seconds)",
    )
    capture_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="capture file path (.jsonl, or .jsonl.gz for gzip compression)",
    )
    replay_parser = subparsers.add_parser(
        "replay",
        help="replay a capture through the production pipeline, optionally serving it",
    )
    replay_parser.add_argument("capture", type=Path, help="capture file to replay")
    replay_parser.add_argument(
        "--speed",
        default=None,
        help="pacing multiplier for recorded gaps, e.g. 2 for twice as fast "
        "(default: as fast as possible, or 1.0 with --serve)",
    )
    replay_parser.add_argument(
        "--serve",
        action="store_true",
        help="serve the HTTP and WebSocket API while streaming the capture, "
        "so the dashboard runs with no live backend",
    )
    return parser


def parse_duration(value: str) -> float:
    """Parse a capture duration like 90s, 10m, or 1h into seconds."""
    text = value.strip().lower()
    multiplier = 1.0
    if text.endswith(("s", "m", "h")):
        suffix = text[-1]
        multiplier = {"s": 1.0, "m": 60.0, "h": 3_600.0}[suffix]
        text = text[:-1]
    try:
        seconds = float(text) * multiplier
    except ValueError as exc:
        raise ConfigError(f"invalid duration {value!r}; expected like 90s, 10m, or 1h") from exc
    if seconds <= 0:
        raise ConfigError(f"duration must be greater than zero; got {value!r}")
    return seconds


def parse_speed(value: str) -> float:
    """Parse a replay pacing multiplier, which must be finite and positive."""
    try:
        speed = float(value)
    except ValueError as exc:
        raise ConfigError(f"invalid speed {value!r}; expected a positive number") from exc
    if not isfinite(speed) or speed <= 0:
        raise ConfigError(f"speed must be finite and greater than zero; got {value!r}")
    return speed


def _write_example_config(path: Path, parser: argparse.ArgumentParser) -> None:
    destination = path.expanduser().resolve()
    if destination.exists():
        parser.error(f"refusing to overwrite existing file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    example = importlib.resources.files("arb").joinpath("config.example.toml").read_text()
    destination.write_text(example)
    print(f"Wrote example configuration to {destination}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.init_config is not None:
        if args.config is not None:
            parser.error("--config and --init-config cannot be used together")
        _write_example_config(args.init_config, parser)
        return

    config_path = args.config or Path(os.getenv("ARB_CONFIG", "config.toml"))
    if not config_path.expanduser().is_file():
        parser.error(
            f"configuration file not found: {config_path}. "
            "Pass --config PATH, set ARB_CONFIG, or create one with --init-config PATH."
        )
    try:
        if args.command == "capture":
            duration_seconds = parse_duration(args.duration)
            asyncio.run(run_capture(config_path, duration_seconds, args.output))
        elif args.command == "replay":
            speed = (
                parse_speed(args.speed) if args.speed is not None else (1.0 if args.serve else None)
            )
            if args.serve:
                asyncio.run(run_replay_serve(config_path, args.capture, speed))
            else:
                report = asyncio.run(run_replay_offline(config_path, args.capture, speed))
                print(
                    json.dumps(
                        {
                            "transitions": len(report.transitions),
                            "opportunities": len(report.opportunities),
                            "snapshots_consumed": report.snapshots_consumed,
                            "digest": report.digest,
                        }
                    )
                )
        else:
            asyncio.run(run_pipeline(config_path))
    except ConfigError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
