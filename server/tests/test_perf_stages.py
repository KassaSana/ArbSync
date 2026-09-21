import pstats
import threading
from decimal import Decimal
from typing import Any

import pytest
from perf_stages import ThreadCpuAccumulator, decimal_value, stage_for, summarize_stages


def test_profile_preserves_decimal_values_and_partitions_self_time() -> None:
    assert decimal_value("0.10000000000000000001") == Decimal("0.10000000000000000001")
    stats: Any = pstats.Stats()
    stats.stats = {
        ("/repo/tools/perf_stages.py", 1, "decimal_value"): (1, 1, 0.2, 0.8, {}),
        ("/repo/server/arb/orderbook.py", 1, "set_level"): (1, 1, 0.3, 0.5, {}),
        ("/repo/server/arb/main.py", 1, "process_market_event"): (1, 1, 0.5, 1.0, {}),
    }
    result = summarize_stages(stats)
    stages: dict[str, dict[str, Any]] = result["stages"]  # type: ignore[assignment]
    assert stages["decimal_conversion"]["self_seconds"] == 0.2
    assert result["total_self_seconds"] == 1.0
    assert abs(sum(stage["percent"] for stage in stages.values()) - 100) < 1e-9
    assert stage_for(r"C:\repo\server\arb\orderbook.py", "set_level") == "sorted_level_mutation"
    assert stage_for("/repo/server/arb/persistence.py", "_flush") == "persistence_main_thread"
    assert stage_for("/repo/server/arb/broadcast.py", "_send_messages") == "websocket_delivery"


def test_negative_profiler_times_are_rejected() -> None:
    stats: Any = pstats.Stats()
    stats.stats = {("test.py", 1, "bad_timer"): (1, 1, -1.0, 1.0, {})}
    with pytest.raises(ValueError, match="Negative profiler"):
        summarize_stages(stats)


def test_thread_cpu_accumulator_only_measures_active_worker_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = iter((1_000, 4_000))
    monkeypatch.setattr("perf_stages.time.thread_time_ns", lambda: next(readings))
    accumulator = ThreadCpuAccumulator()
    calls: list[int] = []

    def work(value: int) -> int:
        calls.append(value)
        return value * 2

    measured = accumulator.measure(work)

    assert measured(1) == 2
    accumulator.start()
    worker = threading.Thread(target=lambda: measured(2))
    worker.start()
    worker.join()

    assert accumulator.stop() == 0.000003
    assert calls == [1, 2]
