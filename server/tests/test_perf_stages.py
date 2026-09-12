import pstats
from decimal import Decimal

import pytest
from perf_stages import decimal_value, stage_for, summarize_stages


def test_profile_preserves_decimal_values_and_partitions_self_time() -> None:
    assert decimal_value("0.10000000000000000001") == Decimal("0.10000000000000000001")
    stats = pstats.Stats()
    stats.stats = {
        ("/repo/tools/perf_stages.py", 1, "decimal_value"): (1, 1, 0.2, 0.8, {}),
        ("/repo/server/arb/orderbook.py", 1, "set_level"): (1, 1, 0.3, 0.5, {}),
        ("/repo/server/arb/main.py", 1, "process_market_event"): (1, 1, 0.5, 1.0, {}),
    }
    result = summarize_stages(stats)
    stages = result["stages"]
    assert stages["decimal_conversion"]["self_seconds"] == 0.2
    assert result["total_self_seconds"] == 1.0
    assert abs(sum(stage["percent"] for stage in stages.values()) - 100) < 1e-9
    assert stage_for(r"C:\repo\server\arb\orderbook.py", "set_level") == "sorted_level_mutation"
    assert stage_for("/repo/server/arb/persistence.py", "_flush") == "persistence_main_thread"
    assert stage_for("/repo/server/arb/broadcast.py", "_send_messages") == "websocket_delivery"


def test_negative_profiler_times_are_rejected() -> None:
    stats = pstats.Stats()
    stats.stats = {("test.py", 1, "bad_timer"): (1, 1, -1.0, 1.0, {})}
    with pytest.raises(ValueError, match="Negative profiler"):
        summarize_stages(stats)
