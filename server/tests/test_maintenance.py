from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from arb.maintenance import parse_cutoff, prune_batch
from arb.persistence import MINUTE_NS, OpportunityStore
from test_persistence import make_opp


def counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as db:
        return (
            db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
            db.execute("SELECT COALESCE(SUM(count), 0) FROM opportunity_minutes").fetchone()[0],
        )


@pytest.mark.asyncio
async def test_partial_minute_boundary_batches_restart_and_statistics(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = OpportunityStore(str(path))
    await store.initialize()
    cutoff = MINUTE_NS + 50
    await store._flush(
        [
            make_opp(1, spread="9"),
            make_opp(cutoff - 1, spread="8"),
            make_opp(cutoff, spread="2", profit="0.123456789"),
            make_opp(cutoff + 1, spread="3", pair="BTC-USDT"),
        ]
    )
    assert prune_batch(path, cutoff, batch_size=1) == 1
    assert counts(path) == (3, 3)
    assert prune_batch(path, cutoff, batch_size=1) == 1
    assert prune_batch(path, cutoff) == 0
    restarted = OpportunityStore(str(path))
    await restarted.initialize()
    assert counts(path) == (2, 2)
    rows = await restarted.recent()
    assert {row["timestamp_ns"] for row in rows} == {cutoff, cutoff + 1}
    assert (
        next(row for row in rows if row["pair"] == "BTC-USD")["theoretical_profit"] == "0.123456789"
    )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(max_spread_pct) FROM opportunity_minutes").fetchone()[0] == 3
        assert (
            db.execute("SELECT COUNT(*) FROM opportunity_minutes WHERE minute_ns = 0").fetchone()[0]
            == 0
        )
    # Public query paths must agree with retained rows, even for a partial minute.
    import aiosqlite

    async with aiosqlite.connect(path) as db:
        total, maximum, spread, profits = await restarted._windowed_totals(db, cutoff)
    assert (total, maximum, spread) == (2, 3, 5)
    assert profits == pytest.approx({"USD": 0.123456789, "USDT": 0.5})


@pytest.mark.asyncio
async def test_active_writer_and_pruner_preserve_rollups(tmp_path: Path) -> None:
    path = tmp_path / "active.sqlite3"
    store = OpportunityStore(str(path), batch_size=2, flush_interval_seconds=0.01)
    await store.initialize()
    await store._flush([make_opp(index) for index in range(30)])
    runner = asyncio.create_task(store.run())
    try:
        for index in range(20):
            assert await store.enqueue(make_opp(MINUTE_NS + index))
        assert await asyncio.to_thread(prune_batch, path, MINUTE_NS) == 30
    finally:
        await store.close()
        await runner
    assert counts(path) == (20, 20)
    assert store.state == "closed"


@pytest.mark.asyncio
async def test_expired_budget_rolls_back_and_lock_contention_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    store = OpportunityStore(str(path))
    await store.initialize()
    await store._flush([make_opp(index) for index in range(20)])
    with sqlite3.connect(path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            prune_batch(path, 100, timeout_seconds=0.01)
    assert counts(path) == (20, 20)
    # A deterministic progress deadline must abort without changing either table.
    ticks = iter([0.0, 0.0, 0.0])
    monkeypatch.setattr("arb.maintenance.time.monotonic", lambda: next(ticks, 10.0))
    with pytest.raises(sqlite3.OperationalError):
        prune_batch(path, 100, timeout_seconds=0.01)
    assert counts(path) == (20, 20)


@pytest.mark.asyncio
async def test_rollup_rebuild_failure_rolls_back_deleted_rows(tmp_path: Path) -> None:
    path = tmp_path / "atomic.sqlite3"
    store = OpportunityStore(str(path))
    await store.initialize()
    await store._flush([make_opp(1), make_opp(2)])
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER fail_rebuild BEFORE INSERT ON opportunity_minutes "
            "BEGIN SELECT RAISE(ABORT, 'injected rebuild failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected rebuild failure"):
        prune_batch(path, 2)
    assert counts(path) == (2, 2)


def test_cutoff_and_invalid_arguments_do_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    assert parse_cutoff("1970-01-01T00:00:00.000001Z") == 1000
    assert parse_cutoff("1970-01-01T01:00:00+01:00") == 0
    with pytest.raises(ValueError, match="timezone"):
        parse_cutoff("2026-09-01")
    with pytest.raises(ValueError, match="batch_size"):
        prune_batch(path, 1, batch_size=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        prune_batch(path, 1, timeout_seconds=float("nan"))
    with pytest.raises(FileNotFoundError):
        prune_batch(path, 1)
    assert not path.exists()
