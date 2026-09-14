from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from aiosqlite.core import _connection_worker_thread
from arb.metrics import persistence_queue_drops_total, persistence_unflushed_rows
from arb.persistence import OpportunityStore
from arb.types import ArbitrageOpportunity


def make_opp(
    timestamp_ns: int, spread: str = "1", profit: str = "0.5", pair: str = "BTC-USD"
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        timestamp_ns=timestamp_ns,
        pair=pair,
        quote_asset=pair.rsplit("-", maxsplit=1)[1],
        buy_exchange="gemini",
        sell_exchange="coinbase",
        buy_price=Decimal("100.123456789"),
        sell_price=Decimal("101.987654321"),
        spread_pct=Decimal(spread),
        max_size=Decimal("0.5"),
        theoretical_profit=Decimal(profit),
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
async def test_initialize_creates_database_parent_directories(tmp_path: Path) -> None:
    database_path = tmp_path / "deployment" / "var" / "db.sqlite3"
    store = OpportunityStore(str(database_path))

    await store.initialize()

    assert database_path.is_file()


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

    assert store.state == "closed"
    assert store.failure is None
    assert store.unflushed_count == 0
    rows = await store.recent(limit=10)
    assert [row["timestamp_ns"] for row in rows] == [5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_enqueue_rejects_new_work_after_close(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    runner = asyncio.create_task(store.run())

    await store.close()
    await runner

    before = persistence_queue_drops_total.labels(reason="store_closed")._value.get()
    assert await store.enqueue(make_opp(1)) is False
    after = persistence_queue_drops_total.labels(reason="store_closed")._value.get()
    assert after - before == 1


@pytest.mark.asyncio
async def test_queue_full_returns_false_and_increments_drop_metric(tmp_path: Path) -> None:
    before = persistence_queue_drops_total.labels(reason="queue_full")._value.get()
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), queue_maxsize=1)
    # No runner — queue stays full.
    assert await store.enqueue(make_opp(1)) is True
    assert await store.enqueue(make_opp(2)) is False
    after = persistence_queue_drops_total.labels(reason="queue_full")._value.get()
    assert after - before == 1


@pytest.mark.asyncio
async def test_initialize_failure_closes_store_and_reports_dropped_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError("database unavailable")
    logged = Mock()

    def fail_connect(_path: str) -> aiosqlite.Connection:
        raise failure

    monkeypatch.setattr("arb.persistence.aiosqlite.connect", fail_connect)
    monkeypatch.setattr("arb.persistence.logger", Mock(error=logged))
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))

    with pytest.raises(OSError, match="database unavailable"):
        await store.initialize()

    assert store.state == "failed"
    assert store.failure is failure
    assert store.failure_reason == "initialize_failed"
    before = persistence_queue_drops_total.labels(reason="initialize_failed")._value.get()
    assert await store.enqueue(make_opp(1)) is False
    after = persistence_queue_drops_total.labels(reason="initialize_failed")._value.get()
    assert after - before == 1
    logged.assert_called_once_with(
        "persistence_store_failed",
        reason="initialize_failed",
        error="OSError('database unavailable')",
        unflushed_rows=0,
    )


@pytest.mark.asyncio
async def test_flush_failure_is_terminal_and_rejects_new_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), batch_size=1)
    await store.initialize()
    failure = RuntimeError("flush failed")
    monkeypatch.setattr(store, "_flush", AsyncMock(side_effect=failure))
    runner = asyncio.create_task(store.run())

    assert await store.enqueue(make_opp(1)) is True
    with pytest.raises(RuntimeError, match="flush failed"):
        await runner

    assert store.state == "failed"
    assert store.failure is failure
    assert store.failure_reason == "worker_failed"
    assert store.unflushed_count == 1
    assert persistence_unflushed_rows._value.get() == 1
    before = persistence_queue_drops_total.labels(reason="worker_failed")._value.get()
    assert await store.enqueue(make_opp(2)) is False
    after = persistence_queue_drops_total.labels(reason="worker_failed")._value.get()
    assert after - before == 1


@pytest.mark.asyncio
async def test_commit_failure_marks_worker_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), batch_size=1)
    await store.initialize()

    async def fail_commit(_connection: aiosqlite.Connection) -> None:
        raise OSError("commit failed")

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_commit)
    runner = asyncio.create_task(store.run())
    assert await store.enqueue(make_opp(1)) is True

    with pytest.raises(OSError, match="commit failed"):
        await runner

    assert store.state == "failed"
    assert store.failure_reason == "worker_failed"
    assert store.unflushed_count == 1


@pytest.mark.asyncio
async def test_close_is_non_blocking_after_worker_failure_with_full_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), batch_size=1, queue_maxsize=2)
    await store.initialize()
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    logged = Mock()

    async def fail_flush(_batch: object) -> None:
        flush_started.set()
        await release_flush.wait()
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_flush", fail_flush)
    monkeypatch.setattr("arb.persistence.logger", Mock(error=logged))
    runner = asyncio.create_task(store.run())
    assert await store.enqueue(make_opp(1)) is True
    await asyncio.wait_for(flush_started.wait(), timeout=1.0)
    assert await store.enqueue(make_opp(2)) is True
    assert await store.enqueue(make_opp(3)) is True
    closing = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release_flush.set()
    with pytest.raises(RuntimeError, match="disk full"):
        await runner

    assert store.unflushed_count == 3
    await asyncio.wait_for(closing, timeout=0.1)
    logged.assert_called_with(
        "persistence_shutdown_incomplete",
        reason="worker_failed",
        error="RuntimeError('disk full')",
        unflushed_rows=3,
    )


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
    assert {row["theoretical_profit"] for row in rows} == {"0.1", "0.2"}

    stats = await store.extended_stats(window_ns=None)
    approximate_profit = Decimal(str(stats["theoretical_profit_by_quote"]["USD"]))
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
    assert Decimal(stats["theoretical_profit_by_quote"]["USD"]) == Decimal("1")


@pytest.mark.asyncio
async def test_stats_group_profits_by_quote_asset(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"), batch_size=2)
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now_ns = time.time_ns()
    await store.enqueue(make_opp(now_ns, profit="1.5", pair="BTC-USD"))
    await store.enqueue(make_opp(now_ns + 1, profit="2.25", pair="BTC-USDT"))
    await store.close()
    await runner

    stats = await store.stats(window_ns=3_600_000_000_000)

    assert stats["theoretical_profit_by_quote"] == {"USD": "1.5", "USDT": "2.25"}


@pytest.mark.asyncio
async def test_stats_with_no_rows_returns_zeros(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "db.sqlite3"))
    await store.initialize()
    stats = await store.stats(window_ns=3_600_000_000_000)
    assert stats["count"] == 0
    assert Decimal(stats["max_spread_pct"]) == Decimal("0")
    assert stats["theoretical_profit_by_quote"] == {}


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
    assert Decimal(str(stats["theoretical_profit_by_quote"]["USD"])) == Decimal("13.5")
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
    assert stats["theoretical_profit_by_quote"] == {}
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
async def test_legacy_usd_profit_schema_is_discarded(tmp_path: Path) -> None:
    """USDT amounts stored as USD cannot be migrated safely."""
    import sqlite3

    path = str(tmp_path / "legacy.sqlite3")
    now_ns = time.time_ns()
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY,
            timestamp_ns INTEGER NOT NULL,
            pair TEXT NOT NULL,
            buy_exchange TEXT NOT NULL,
            sell_exchange TEXT NOT NULL,
            buy_price TEXT NOT NULL,
            sell_price TEXT NOT NULL,
            spread_pct TEXT NOT NULL,
            max_size TEXT NOT NULL,
            theoretical_profit_usd TEXT NOT NULL
        );
        """
    )
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
    assert stats["count"] == 0
    assert stats["theoretical_profit_by_quote"] == {}

    # Migration is one-time and later startup preserves v2 data.
    runner = asyncio.create_task(store.run())
    await store.enqueue(make_opp(now_ns, profit="2", pair="BTC-USDT"))
    await store.close()
    await runner
    await store.initialize()
    assert (await store.extended_stats(window_ns=None))["theoretical_profit_by_quote"] == {
        "USDT": "2.0"
    }


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
    assert Decimal(str(stats["theoretical_profit_by_quote"]["USD"])) == Decimal("5")
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
    assert Decimal(str(stats["theoretical_profit_by_quote"]["USD"])) == Decimal("1")
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


def live_sqlite_worker_threads() -> set[threading.Thread]:
    """Every aiosqlite worker thread still running.

    aiosqlite runs each connection on a plain `Thread` that is not a daemon, so
    one left alive after the event loop closes blocks interpreter exit instead
    of raising anything a test would otherwise notice. The threads carry no
    distinguishing name, so they are identified by the function they run.

    `threading.enumerate` also reports threads that have finished but have not
    been reaped yet, so callers comparing before and after would otherwise see
    an earlier test's expiring thread as a change of their own making.
    """
    return {
        thread
        for thread in threading.enumerate()
        if getattr(thread, "_target", None) is _connection_worker_thread
        if thread.is_alive()
    }


async def wait_for_no_new_sqlite_workers(before: set[threading.Thread]) -> None:
    """Fail unless every worker started during the test has stopped.

    Closing a connection returns once the worker has processed the close, which
    is a moment before the thread itself finishes, so asserting immediately is a
    race that fails roughly one run in five. A thread that is genuinely stranded
    never leaves, so waiting costs nothing when the assertion should fail.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_TIMEOUT_SECONDS
    while True:
        extra = live_sqlite_worker_threads() - before
        if not extra:
            return
        if loop.time() >= deadline:
            raise AssertionError(
                f"aiosqlite worker threads still running after {WAIT_TIMEOUT_SECONDS}s: {extra}"
            )
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_graceful_shutdown_stops_the_reused_writer_thread(tmp_path: Path) -> None:
    """The reused writer connection must not outlive the worker that owns it.

    The per-flush connection this replaced was closed by its own context manager
    every interval, so nothing previously depended on shutdown ordering to
    release it. One connection now spans the whole run.
    """
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=1, flush_interval_seconds=0.05
    )
    await store.initialize()
    before = live_sqlite_worker_threads()
    worker = asyncio.create_task(store.run())
    assert await store.enqueue(make_opp(1))
    await store.close()
    await asyncio.wait_for(worker, timeout=WAIT_TIMEOUT_SECONDS)

    assert store._db is None
    # Threads other tests left running are in both snapshots and cancel out.
    await wait_for_no_new_sqlite_workers(before)


@pytest.mark.asyncio
async def test_cancelled_worker_releases_its_writer_thread(tmp_path: Path) -> None:
    """Cancellation must release the writer too, not just the sentinel path.

    Shutdown normally drains a sentinel, but a worker can also be cancelled. The
    `finally` in `run` is what closes the connection in that case, and it clears
    the attribute before awaiting the close, so an interrupted close would strand
    a thread no one holds a reference to.
    """
    store = OpportunityStore(
        str(tmp_path / "db.sqlite3"), batch_size=1, flush_interval_seconds=0.05
    )
    await store.initialize()
    before = live_sqlite_worker_threads()
    worker = asyncio.create_task(store.run())
    assert await store.enqueue(make_opp(1))
    await wait_for_rows(store, 1)
    assert store._db is not None
    assert live_sqlite_worker_threads() - before != set()

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert store._db is None
    await wait_for_no_new_sqlite_workers(before)
