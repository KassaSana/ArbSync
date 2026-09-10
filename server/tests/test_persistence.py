from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from arb.persistence import OpportunityStore
from arb.types import ArbitrageOpportunity


def make_opp(
    timestamp_ns: int, spread: str = "1", profit: str = "0.5", pair: str = "BTC-USD"
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        timestamp_ns=timestamp_ns,
        pair=pair,
        buy_exchange="gemini",
        sell_exchange="coinbase",
        buy_price=Decimal("100.123456789"),
        sell_price=Decimal("101.987654321"),
        spread_pct=Decimal(spread),
        max_size=Decimal("0.5"),
        theoretical_profit_usd=Decimal(profit),
    )


WAIT_TIMEOUT_SECONDS = 5.0


async def wait_for_rows(store: OpportunityStore, expected: int) -> list[dict[str, Any]]:
    """Poll until `expected` opportunities are readable, then return them.

    Only for the two tests whose subject is the flush trigger itself; they cannot
    use the deterministic drain in `close()` without hiding what they assert. The
    writer flushes on its own interval and aiosqlite commits on a worker thread,
    so a fixed sleep races the flush — it passes locally and fails on a loaded
    CI runner.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_TIMEOUT_SECONDS
    while True:
        rows = await store.recent(limit=expected + 1)
        if len(rows) >= expected:
            return rows
        if loop.time() >= deadline:
            raise AssertionError(
                f"{len(rows)} of {expected} opportunities persisted within {WAIT_TIMEOUT_SECONDS}s"
            )
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_initialize_is_idempotent(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    # Calling twice must not raise — uses CREATE IF NOT EXISTS.
    await store.initialize()


@pytest.mark.asyncio
async def test_batched_flush_by_size_writes_all_rows(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), batch_size=3, flush_interval_seconds=5.0)
    await store.initialize()
    runner = asyncio.create_task(store.run())
    for index in range(3):
        await store.enqueue(make_opp(timestamp_ns=index + 1))
    # Allow batch-by-size flush to complete (no need to wait the 5s interval).
    rows = await wait_for_rows(store, 3)
    assert len(rows) == 3
    await store.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_flush_interval_drains_partial_batch(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=500, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    await store.enqueue(make_opp(timestamp_ns=1))
    # Far below batch_size, but interval should fire.
    rows = await wait_for_rows(store, 1)
    assert len(rows) == 1
    await store.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_close_drains_every_accepted_opportunity(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"),
        batch_size=500,
        flush_interval_seconds=60.0,
        queue_maxsize=10,
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())

    for timestamp_ns in range(1, 6):
        assert await store.enqueue(make_opp(timestamp_ns)) is True

    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    rows = await store.recent(limit=10)
    assert [row["timestamp_ns"] for row in rows] == [5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_enqueue_rejects_new_work_after_close(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    runner = asyncio.create_task(store.run())

    await store.close()
    await runner

    assert await store.enqueue(make_opp(1)) is False


@pytest.mark.asyncio
async def test_queue_full_returns_false_and_increments_drop_metric(tmp_path: Path) -> None:
    from arb.metrics import persistence_queue_drops_total

    before = persistence_queue_drops_total._value.get()
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), queue_maxsize=1)
    # No runner — queue stays full.
    assert await store.enqueue(make_opp(1)) is True
    assert await store.enqueue(make_opp(2)) is False
    after = persistence_queue_drops_total._value.get()
    assert after - before == 1


@pytest.mark.asyncio
async def test_decimal_round_trip_preserves_string_form(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), batch_size=1, flush_interval_seconds=5.0)
    await store.initialize()
    runner = asyncio.create_task(store.run())
    await store.enqueue(make_opp(timestamp_ns=10))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)
    [row] = await store.recent(limit=10)
    # Stored as TEXT — exact round trip with no float drift.
    assert row["buy_price"] == "100.123456789"
    assert row["sell_price"] == "101.987654321"


@pytest.mark.asyncio
async def test_canonical_decimals_remain_exact_when_rollups_are_approximate(
    tmp_path: Path,
) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    timestamp_ns = time.time_ns()
    await store.enqueue(make_opp(timestamp_ns, spread="0.1", profit="0.1"))
    await store.enqueue(make_opp(timestamp_ns + 1, spread="0.2", profit="0.2"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    rows = await store.recent(limit=2)
    assert {row["spread_pct"] for row in rows} == {"0.1", "0.2"}
    assert {row["theoretical_profit_usd"] for row in rows} == {"0.1", "0.2"}

    stats = await store.extended_stats(window_ns=None)
    approximate_profit = Decimal(str(stats["total_theoretical_profit_usd"]))
    assert approximate_profit != Decimal("0.3")
    assert float(approximate_profit) == pytest.approx(0.3, rel=0, abs=1e-15)


@pytest.mark.asyncio
async def test_recent_orders_descending_by_timestamp(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    for ts in (10, 30, 20):
        await store.enqueue(make_opp(timestamp_ns=ts))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)
    rows = await store.recent(limit=10)
    assert [row["timestamp_ns"] for row in rows] == [30, 20, 10]


@pytest.mark.asyncio
async def test_stats_window_filters_out_old_rows(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now_ns = time.time_ns()
    await store.enqueue(make_opp(timestamp_ns=now_ns, spread="2", profit="1"))
    await store.enqueue(
        make_opp(timestamp_ns=now_ns - 10_000_000_000_000, spread="9", profit="100")
    )  # ~3h old
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    one_hour_ns = 3_600_000_000_000
    stats = await store.stats(window_ns=one_hour_ns)
    assert stats["count"] == 1
    assert Decimal(stats["max_spread_pct"]) == Decimal("2")
    assert Decimal(stats["total_theoretical_profit_usd"]) == Decimal("1")


@pytest.mark.asyncio
async def test_stats_with_no_rows_returns_zeros(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    stats = await store.stats(window_ns=3_600_000_000_000)
    assert stats["count"] == 0
    assert Decimal(stats["max_spread_pct"]) == Decimal("0")
    assert Decimal(stats["total_theoretical_profit_usd"]) == Decimal("0")


@pytest.mark.asyncio
async def test_recent_on_empty_store_returns_empty_list(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    assert await store.recent(limit=5) == []


@pytest.mark.asyncio
async def test_extended_stats_aggregates_within_window(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now_ns = time.time_ns()
    # 3 BTC opps, 1 ETH opp inside window; one stale ETH outside.
    await store.enqueue(make_opp(now_ns, spread="2", profit="1", pair="BTC-USD"))
    await store.enqueue(make_opp(now_ns - 1, spread="3", profit="2", pair="BTC-USD"))
    await store.enqueue(make_opp(now_ns - 2, spread="1", profit="0.5", pair="BTC-USD"))
    await store.enqueue(make_opp(now_ns - 3, spread="5", profit="10", pair="ETH-USD"))
    await store.enqueue(
        make_opp(now_ns - 10_000_000_000_000, spread="99", profit="999", pair="ETH-USD")
    )
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    stats = await store.extended_stats(window_ns=3_600_000_000_000)
    assert stats["count"] == 4
    assert Decimal(str(stats["max_spread_pct"])) == Decimal("5")
    assert Decimal(str(stats["mean_spread_pct"])) == Decimal("2.75")
    assert Decimal(str(stats["total_theoretical_profit_usd"])) == Decimal("13.5")
    assert stats["top_pair"] == "BTC-USD"


@pytest.mark.asyncio
async def test_extended_stats_all_time_with_window_none(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    await store.enqueue(make_opp(timestamp_ns=1, spread="2", pair="BTC-USD"))
    await store.enqueue(make_opp(timestamp_ns=2, spread="4", pair="BTC-USD"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    stats = await store.extended_stats(window_ns=None)
    assert stats["count"] == 2
    assert Decimal(str(stats["max_spread_pct"])) == Decimal("4")
    assert stats["top_pair"] == "BTC-USD"


@pytest.mark.asyncio
async def test_extended_stats_empty_returns_safe_defaults(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    stats = await store.extended_stats(window_ns=3_600_000_000_000)
    assert stats["count"] == 0
    assert Decimal(str(stats["max_spread_pct"])) == Decimal("0")
    assert Decimal(str(stats["mean_spread_pct"])) == Decimal("0")
    assert Decimal(str(stats["total_theoretical_profit_usd"])) == Decimal("0")
    assert stats["top_pair"] is None


@pytest.mark.asyncio
async def test_peak_minute_returns_busiest_bucket(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=20, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    minute_ns = 60_000_000_000
    base = (time.time_ns() // minute_ns) * minute_ns
    # Minute A: 2 opps. Minute B: 5 opps (the peak).
    for offset in range(2):
        await store.enqueue(make_opp(timestamp_ns=base + offset))
    for offset in range(5):
        await store.enqueue(make_opp(timestamp_ns=base + minute_ns + offset))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    peak = await store.peak_minute(window_ns=3_600_000_000_000)
    assert peak is not None
    assert peak["count"] == 5
    assert peak["minute_start_ns"] == base + minute_ns


@pytest.mark.asyncio
async def test_peak_minute_returns_none_when_empty(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    assert await store.peak_minute(window_ns=3_600_000_000_000) is None
    assert await store.peak_minute(window_ns=None) is None


@pytest.mark.asyncio
async def test_timeseries_buckets_and_orders_ascending(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=20, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    bucket_seconds = 60
    bucket_ns = bucket_seconds * 1_000_000_000
    base = (time.time_ns() // bucket_ns) * bucket_ns
    await store.enqueue(make_opp(timestamp_ns=base, spread="1"))
    await store.enqueue(make_opp(timestamp_ns=base + 1, spread="3"))
    await store.enqueue(make_opp(timestamp_ns=base + bucket_ns, spread="2"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    points = await store.timeseries(window_ns=3_600_000_000_000, bucket_seconds=bucket_seconds)
    assert len(points) == 2
    assert points[0]["bucket_start_ns"] == base
    assert points[0]["count"] == 2
    assert Decimal(str(points[0]["max_spread_pct"])) == Decimal("3")
    assert points[1]["bucket_start_ns"] == base + bucket_ns
    assert points[1]["count"] == 1


@pytest.mark.asyncio
async def test_timeseries_rejects_non_positive_bucket(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    with pytest.raises(ValueError):
        await store.timeseries(window_ns=3_600_000_000_000, bucket_seconds=0)


@pytest.mark.asyncio
async def test_rollup_backfills_a_database_written_before_it_existed(tmp_path: Path) -> None:
    """History predating the rollup must still be reported by the statistics."""
    import sqlite3

    from arb.persistence import CREATE_INDEX_SQL, CREATE_TABLE_SQL

    path = str(tmp_path / "legacy.sqlite3")
    now_ns = time.time_ns()
    legacy = sqlite3.connect(path)
    legacy.executescript(CREATE_TABLE_SQL)
    legacy.executescript(CREATE_INDEX_SQL)
    legacy.executemany(
        "INSERT INTO opportunities (timestamp_ns, pair, buy_exchange, sell_exchange,"
        " buy_price, sell_price, spread_pct, max_size, theoretical_profit_usd)"
        " VALUES (?, ?, 'gemini', 'coinbase', '100', '101', ?, '1', ?)",
        [
            (now_ns, "BTC-USD", "2", "1"),
            (now_ns - 1, "BTC-USD", "4", "2"),
            (now_ns - 2, "ETH-USD", "6", "3"),
        ],
    )
    legacy.commit()
    legacy.close()

    store = OpportunityStore(path)
    await store.initialize()

    stats = await store.extended_stats(window_ns=None)
    assert stats["count"] == 3
    assert Decimal(str(stats["max_spread_pct"])) == Decimal("6")
    assert Decimal(str(stats["mean_spread_pct"])) == Decimal("4")
    assert Decimal(str(stats["total_theoretical_profit_usd"])) == Decimal("6")
    assert stats["top_pair"] == "BTC-USD"

    # Backfilling twice would double every total.
    await store.initialize()
    assert (await store.extended_stats(window_ns=None))["count"] == 3


@pytest.mark.asyncio
async def test_rollup_stays_consistent_across_separate_write_batches(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=2, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now_ns = time.time_ns()
    # Five opportunities in one minute across three flushes of two.
    for index in range(5):
        await store.enqueue(make_opp(now_ns - index, spread=str(index + 1), profit="1"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    stats = await store.extended_stats(window_ns=None)
    assert stats["count"] == 5
    assert Decimal(str(stats["max_spread_pct"])) == Decimal("5")
    assert Decimal(str(stats["mean_spread_pct"])) == Decimal("3")
    assert Decimal(str(stats["total_theoretical_profit_usd"])) == Decimal("5")
    assert (await store.peak_minute(window_ns=None)) == {
        "minute_start_ns": (now_ns // 60_000_000_000) * 60_000_000_000,
        "count": 5,
    }


@pytest.mark.asyncio
async def test_window_starting_mid_minute_excludes_earlier_rows_in_that_minute(
    tmp_path: Path,
) -> None:
    """The rollup holds whole minutes; a window cutting one must stay exact."""
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now_ns = time.time_ns()
    minute_start = (now_ns // 60_000_000_000) * 60_000_000_000
    # Both land in the same minute, on opposite sides of a 30-second window.
    await store.enqueue(make_opp(minute_start + 55_000_000_000, spread="2", profit="1"))
    await store.enqueue(make_opp(minute_start + 5_000_000_000, spread="90", profit="500"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    cutoff_ns = minute_start + 30_000_000_000
    stats = await store.extended_stats(window_ns=time.time_ns() - cutoff_ns)
    assert stats["count"] == 1
    assert Decimal(str(stats["max_spread_pct"])) == Decimal("2")
    assert Decimal(str(stats["total_theoretical_profit_usd"])) == Decimal("1")
    assert (await store.peak_minute(window_ns=time.time_ns() - cutoff_ns)) == {
        "minute_start_ns": minute_start,
        "count": 1,
    }


@pytest.mark.asyncio
async def test_sub_minute_timeseries_buckets_do_not_use_the_rollup(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now_ns = time.time_ns()
    minute_start = (now_ns // 60_000_000_000) * 60_000_000_000
    await store.enqueue(make_opp(minute_start + 1_000_000_000, spread="2"))
    await store.enqueue(make_opp(minute_start + 40_000_000_000, spread="3"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    points = await store.timeseries(window_ns=3_600_000_000_000, bucket_seconds=30)
    assert [point["count"] for point in points] == [1, 1]
    # Floats via SQL MAX(CAST(... AS REAL)), matching the pre-rollup format.
    assert [str(point["max_spread_pct"]) for point in points] == ["2.0", "3.0"]
