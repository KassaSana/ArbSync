"""Exclusive function-time attribution for isolated diagnostic runs."""

from __future__ import annotations

import pstats
import threading
import time
from decimal import Decimal
from typing import Any


class ThreadCpuAccumulator:
    """Measure CPU used by functions executed on background worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._nanoseconds = 0

    def start(self) -> None:
        with self._lock:
            self._nanoseconds = 0
            self._active = True

    def stop(self) -> float:
        with self._lock:
            self._active = False
            return self._nanoseconds / 1_000_000_000

    def measure(self, function: Any) -> Any:
        """Wrap a callable so its executing thread's CPU time is accumulated."""

        def measured(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                active = self._active
            if not active:
                return function(*args, **kwargs)
            started = time.thread_time_ns()
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = time.thread_time_ns() - started
                with self._lock:
                    self._nanoseconds += elapsed

        return measured


def instrument_aiosqlite_worker_cpu(accumulator: ThreadCpuAccumulator) -> None:
    """Instrument aiosqlite calls inside this disposable benchmark process."""
    from aiosqlite.core import Connection

    original_execute = Connection._execute

    async def measured_execute(self: Any, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await original_execute(self, accumulator.measure(function), *args, **kwargs)

    Connection._execute = measured_execute  # type: ignore[assignment]


def decimal_value(value: Any) -> Decimal:
    # cProfile doesn't expose the Decimal type constructor as a separate frame.
    # A diagnostic-only frame makes that conversion cost visible in self time.
    return Decimal(value)


def stage_for(filename: str, function: str) -> str:
    path = filename.replace("\\", "/")
    if path.endswith("/perf_stages.py") and function == "decimal_value":
        return "decimal_conversion"
    if "/json/" in path or "_json" in function:
        return "json_decode_encode"
    if path.endswith("/arb/orderbook.py") and function in {"set_level", "remove"}:
        return "sorted_level_mutation"
    if path.endswith("/arb/detector.py"):
        return "detection"
    if path.endswith("/arb/persistence.py") or "/aiosqlite/" in path:
        return "persistence_main_thread"
    if path.endswith("/arb/broadcast.py") or "/starlette/websockets.py" in path:
        return "websocket_delivery"
    if "GetQueuedCompletionStatus" in function or "select" in function or "epoll" in function:
        return "event_loop_wait"
    return "other"


def summarize_stages(
    stats: pstats.Stats, *, sqlite_worker_cpu_seconds: float | None = None
) -> dict[str, object]:
    seconds = dict.fromkeys(
        [
            "json_decode_encode",
            "decimal_conversion",
            "sorted_level_mutation",
            "detection",
            "persistence_main_thread",
            "websocket_delivery",
            "event_loop_wait",
            "other",
        ],
        0.0,
    )
    # Exclusive/self times partition measured time; cumulative times overlap and cannot
    # be added to establish the share attributable to each stage.
    for (filename, _line, function), values in stats.stats.items():  # type: ignore[attr-defined]
        if values[2] < 0:
            raise ValueError("Negative profiler self time; this run cannot support attribution")
        seconds[stage_for(filename, function)] += values[2]
    total = sum(seconds.values())
    result: dict[str, object] = {
        "timer": "cProfile default high-resolution elapsed timer; self time, not exact CPU",
        "total_self_seconds": total,
        "stages": {
            name: {"self_seconds": value, "percent": value / total * 100 if total else 0}
            for name, value in seconds.items()
        },
        "limits": "Diagnostic overhead and scheduling included; event-loop wait is separate. "
        "Scheduling lag is measured "
        "separately and cannot be causally assigned to a stage from this profile.",
    }
    if sqlite_worker_cpu_seconds is not None:
        result["sqlite_worker"] = {
            "thread_cpu_seconds": sqlite_worker_cpu_seconds,
            "timer": "time.thread_time_ns around functions executed by aiosqlite workers",
            "scope": "All aiosqlite worker calls during the measured interval, including writes and API reads",
        }
    return result
