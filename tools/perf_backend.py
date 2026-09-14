"""Isolated profiling worker using production adapters, handler, API and storage.

Only this localhost benchmark app has control routes. Normal arb.main is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import cProfile
import faulthandler
import json
import os
import pstats
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import uvicorn
from arb.adapters.binance import BinanceAdapter
from arb.adapters.coinbase import CoinbaseAdapter
from arb.adapters.gemini import GeminiAdapter
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.detector import ArbitrageDetector
from arb.main import BackgroundTaskSupervisor, process_market_event
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.types import LiveMessage
from fastapi.staticfiles import StaticFiles
from perf_feed import ASSETS
from perf_stages import (
    ThreadCpuAccumulator,
    decimal_value,
    instrument_aiosqlite_worker_cpu,
    summarize_stages,
)
from prometheus_client import REGISTRY


@dataclass
class EventSample:
    key: list
    receipt_ns: int
    detected_ns: int | None = None


current_sample: contextvars.ContextVar[EventSample] = contextvars.ContextVar("event_sample")


class TimedDetector(ArbitrageDetector):
    def detect_for_pair(self, pair, books, timestamp_ns):
        result = super().detect_for_pair(pair, books, timestamp_ns)
        current_sample.get().detected_ns = time.monotonic_ns()
        return result


def counters() -> dict:
    names = (
        "arb_persistence_queue_drops_total",
        "arb_ws_client_queue_overflows_total",
        "arb_opportunities_total",
        "arb_events_ingested_total",
    )
    return {
        name: sum(
            s.value for metric in REGISTRY.collect() for s in metric.samples if s.name == name
        )
        for name in names
    }


async def run(args) -> None:
    # Python 3.12's Windows monotonic_ns can tick in ~15.6 ms increments.
    # All receipt/freshness/completion timestamps in this isolated worker use
    # perf_counter_ns instead, including the manager's explicitly supplied clock.
    # Normal arb.main clocks are unchanged.
    time.monotonic_ns = time.perf_counter_ns
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = output / "opportunities.sqlite3"
    if database.exists():
        raise RuntimeError(
            "Use a fresh output directory; benchmark databases are never overwritten"
        )
    sqlite_worker_cpu = ThreadCpuAccumulator()
    if args.profile:
        instrument_aiosqlite_worker_cpu(sqlite_worker_cpu)
    store = OpportunityStore(str(database))
    await store.initialize()
    manager = OrderBookManager(max_age_seconds=60, clock=time.perf_counter_ns)
    detector = TimedDetector(Decimal("0.1"))
    broadcaster = LiveBroadcaster()
    adapters = [
        GeminiAdapter([f"{a.lower()}usd" for a in ASSETS]),
        CoinbaseAdapter([f"{a}-USD" for a in ASSETS]),
        BinanceAdapter([f"{a}USDT" for a in ASSETS]),
    ]
    for adapter in adapters:
        adapter.ws_url = f"ws://127.0.0.1:{args.feed_port}/{adapter.name}"
    adapters[2].snapshot_url = f"http://127.0.0.1:{args.feed_port}/snapshot"
    expected = [(adapter.name, pair) for adapter in adapters for pair in adapter.expected_pairs()]
    supervisor = BackgroundTaskSupervisor()
    rows: list = []
    queue_samples: list = []
    lag_ms: list = []
    active = False
    initial_counters: dict = {}
    profiler = cProfile.Profile()
    if args.profile:
        # Keep conversion instrumentation isolated to this disposable worker.
        from arb.adapters import binance, coinbase, gemini

        for module in (binance, coinbase, gemini):
            module.Decimal = decimal_value

    async def connection_state(exchange, connected):
        for status in manager.set_exchange_connected(exchange, connected):
            await broadcaster.broadcast_book_now(
                exchange, status.pair, LiveMessage("book_status", status.as_payload())
            )

    async def consume(adapter):
        async for event in adapter.connect():
            sample = EventSample(
                [event.exchange, event.pair, event.exchange_last_sequence],
                event.received_monotonic_ns or time.monotonic_ns(),
            )
            token = current_sample.set(sample)
            measured = active
            try:
                await process_market_event(
                    event,
                    book_manager=manager,
                    detector=detector,
                    store=store,
                    broadcaster=broadcaster,
                )
                completed = time.monotonic_ns()
                if measured:
                    rows.append([*sample.key, sample.receipt_ns, sample.detected_ns, completed])
            finally:
                current_sample.reset(token)

    async def sample_queues():
        while True:
            target = time.perf_counter() + 0.05
            await asyncio.sleep(0.05)
            if active:
                lag_ms.append(max(0, time.perf_counter() - target) * 1000)
                queue_samples.append(
                    [
                        store._queue.qsize(),
                        max((c.queue.qsize() for c in broadcaster._clients.values()), default=0),
                        len(broadcaster._clients),
                    ]
                )

    app = create_app(
        store,
        manager,
        broadcaster,
        adapters=adapters,
        expected_pairs=expected,
        background_failures=supervisor.failures,
    )

    @app.post("/__bench/start")
    async def start():
        nonlocal active, initial_counters
        if active:
            raise RuntimeError("Already measuring")
        rows.clear()
        queue_samples.clear()
        lag_ms.clear()
        initial_counters = counters()
        active = True
        if args.profile:
            sqlite_worker_cpu.start()
            profiler.enable()
        return {"started": True}

    @app.get("/__bench/progress")
    async def progress():
        return {
            "pid": os.getpid(),
            "processed": len(rows),
            "clients": len(broadcaster._clients),
            "persistence_queue": store._queue.qsize(),
        }

    @app.post("/__bench/stop")
    async def stop():
        nonlocal active
        active = False
        if args.profile:
            profiler.disable()
            worker_cpu_seconds = sqlite_worker_cpu.stop()
            profiler.dump_stats(str(output / "backend.pstats"))
            (output / "stages.json").write_text(
                json.dumps(
                    summarize_stages(
                        pstats.Stats(profiler),
                        sqlite_worker_cpu_seconds=worker_cpu_seconds,
                    ),
                    indent=2,
                )
            )
            with (output / "backend-profile.txt").open("w") as stream:
                stats = pstats.Stats(profiler, stream=stream).strip_dirs()
                stats.sort_stats("tottime").print_stats(50)
                stats.sort_stats("cumulative").print_stats(50)
        counts = counters()
        result = {
            "rows": rows,
            "queue_samples": queue_samples,
            "event_loop_lag_ms": lag_ms,
            "counters": {k: counts[k] - v for k, v in initial_counters.items()},
            "failures": supervisor.failures(),
            "adapters": [a.status_snapshot().as_payload() for a in adapters],
        }
        (output / "backend.json").write_text(json.dumps(result))
        return {"processed": len(rows), "counters": result["counters"]}

    @app.post("/__bench/shutdown")
    async def shutdown():
        server.should_exit = True
        return {"stopping": True}

    # The production API serves service metadata at GET /. This isolated worker
    # serves the built dashboard there so browser measurements exercise React.
    app.router.routes[:] = [
        route for route in app.router.routes if getattr(route, "path", None) != "/"
    ]
    app.mount("/", StaticFiles(directory="dashboard/dist-perf", html=True), name="dashboard")
    for adapter in adapters:
        adapter.set_connection_state_callback(connection_state)
    persistence = supervisor.create("persistence", store.run())
    tasks = [supervisor.create(a.name, consume(a)) for a in adapters]
    tasks.append(supervisor.create("queue_sampler", sample_queues()))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    )
    try:
        await server.serve()
    finally:
        # A backend that stops responding during shutdown is otherwise invisible:
        # the harness only sees a process that never exits and has to be repeated
        # until it happens again. Timing each phase names the one that stalled,
        # and the watchdog dumps every thread's stack while it is still stuck.
        phase_seconds: dict[str, float] = {}
        stacks = (output / "shutdown-stacks.txt").open("w")
        faulthandler.dump_traceback_later(15, repeat=True, file=stacks, exit=False)

        async def phase(name: str, awaitable) -> None:
            started = time.perf_counter()
            try:
                await awaitable
            finally:
                # A phase that never returns records no duration, so the missing
                # key is the one that hung.
                phase_seconds[name] = time.perf_counter() - started

        try:
            supervisor.stop()
            for task in tasks:
                task.cancel()
            await phase("cancel_tasks", asyncio.gather(*tasks, return_exceptions=True))
            await phase("broadcaster_aclose", broadcaster.aclose())
            await phase("store_close", store.close())
            await phase("persistence_worker", persistence)
        finally:
            faulthandler.cancel_dump_traceback_later()
            (output / "shutdown.json").write_text(
                json.dumps({"phase_seconds": phase_seconds}, indent=2)
            )
            stacks.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--feed-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(run(arguments))

    # The interpreter joins non-daemon threads before exiting, so a survivor here
    # is exactly what a process that hangs after finishing its work looks like.
    # aiosqlite worker threads are not daemons. Record them while recording is
    # still possible, because the join happens after this and never returns.
    output_directory = arguments.output.resolve()
    lingering = sorted(
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.main_thread() and not thread.daemon
    )
    diagnostics = output_directory / "shutdown.json"
    if diagnostics.exists():
        recorded = json.loads(diagnostics.read_text())
        recorded["lingering_non_daemon_threads"] = lingering
        diagnostics.write_text(json.dumps(recorded, indent=2))
    if lingering:
        # Dump stacks from inside the hanging join rather than leaving the
        # harness to kill a silent process. The harness still times and records
        # the forced exit, so this only adds evidence.
        faulthandler.dump_traceback_later(
            20, file=(output_directory / "join-stacks.txt").open("w"), exit=False
        )
