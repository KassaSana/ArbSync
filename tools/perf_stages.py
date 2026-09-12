"""Exclusive function-time attribution for isolated diagnostic runs."""

from __future__ import annotations

import pstats
from decimal import Decimal
from typing import Any


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


def summarize_stages(stats: pstats.Stats) -> dict[str, object]:
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
    return {
        "timer": "cProfile default high-resolution elapsed timer; self time, not exact CPU",
        "total_self_seconds": total,
        "stages": {
            name: {"self_seconds": value, "percent": value / total * 100 if total else 0}
            for name, value in seconds.items()
        },
        "limits": "Diagnostic overhead and scheduling included; event-loop wait is separate. SQLite worker CPU is excluded; "
        "process CPU in unprofiled runs includes all threads. Scheduling lag is measured "
        "separately and cannot be causally assigned to a stage from this profile.",
    }
