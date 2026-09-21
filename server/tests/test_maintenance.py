from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from arb import maintenance
from arb.maintenance import parse_cutoff, prune_batch
from arb.persistence import MINUTE_NS, OpportunityStore
from episodes import close_episode
from test_persistence import make_opp


def counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as db:
        return (
            db.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0],
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
    await store._close_db()
    assert prune_batch(path, cutoff, batch_size=1) == 1
    assert counts(path) == (3, 3)
    assert prune_batch(path, cutoff, batch_size=1) == 1
    assert prune_batch(path, cutoff) == 0
    restarted = OpportunityStore(str(path))
    await restarted.initialize()
    assert counts(path) == (2, 2)
    rows = await restarted.recent()
    assert {row["start_ns"] for row in rows} == {cutoff, cutoff + 1}
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
async def test_rebuilt_minute_uses_peak_values_of_closed_episodes(tmp_path: Path) -> None:
    # ARB-031: the rollup carries each episode at its peak, so a rebuild after
    # pruning must read the peak columns, not the open-time ones.
    from decimal import Decimal

    path = tmp_path / "peaks.sqlite3"
    store = OpportunityStore(str(path))
    await store.initialize()
    survivor = make_opp(MINUTE_NS + 1, spread="1", profit="0.5")
    pruned = make_opp(MINUTE_NS - 1, spread="1", profit="0.5")
    await store._flush(
        [
            pruned,
            survivor,
            close_episode(
                survivor,
                duration_ns=5,
                peak_spread_pct=Decimal("7"),
                peak_profit=Decimal("3.5"),
            ),
        ]
    )
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT count, max_spread_pct, sum_spread_pct, sum_profit "
            "FROM opportunity_minutes WHERE minute_ns = ?",
            (MINUTE_NS,),
        ).fetchone()
    assert row == (1, 7.0, 7.0, 3.5)

    # Prune the older minute; the surviving minute's rebuild is not triggered,
    # and pruning inside the surviving minute rebuilds it from peak columns.
    assert prune_batch(path, MINUTE_NS) == 1
    await store._flush([make_opp(MINUTE_NS + 2, spread="2", profit="1")])
    assert prune_batch(path, MINUTE_NS + 2) == 1
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT count, max_spread_pct, sum_spread_pct, sum_profit "
            "FROM opportunity_minutes WHERE minute_ns = ?",
            (MINUTE_NS,),
        ).fetchone()
    assert row == (1, 2.0, 2.0, 1.0)
    assert counts(path) == (1, 1)
    await store._close_db()


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
        # This test is about coexisting with the writer, not about the budget:
        # the default 0.5 s expired under lock contention on a slow CI runner.
        # Budget expiry has its own deterministic test below.
        assert await asyncio.to_thread(prune_batch, path, MINUTE_NS, timeout_seconds=2.0) == 30
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
    await store._close_db()
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
    await store._close_db()
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


@pytest.fixture
def seeded_database(tmp_path: Path) -> Path:
    """Three rows before minute one, one row after it."""
    path = tmp_path / "history.sqlite3"

    async def seed() -> None:
        store = OpportunityStore(str(path))
        await store.initialize()
        await store._flush([make_opp(1), make_opp(2), make_opp(3), make_opp(MINUTE_NS + 1)])
        await store.close()

    asyncio.run(seed())
    return path


def test_cli_prunes_in_batches_and_reports_each_one(
    seeded_database: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("arb.maintenance.time.sleep", pauses.append)

    maintenance.main(
        [
            "--database",
            str(seeded_database),
            "--before",
            "1970-01-01T00:01:00Z",
            "--batch-size",
            "2",
            "--max-batches",
            "5",
        ]
    )

    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports == [
        {"batch": 1, "deleted": 2, "total_deleted": 2},
        {"batch": 2, "deleted": 1, "total_deleted": 3},
    ]
    # A short batch ends the run before the third one is attempted or waited for.
    assert pauses == [0.1]
    assert counts(seeded_database) == (1, 1)


def test_cli_stops_at_max_batches(
    seeded_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    maintenance.main(
        [
            "--database",
            str(seeded_database),
            "--before",
            "1970-01-01T00:01:00Z",
            "--batch-size",
            "1",
        ]
    )

    assert json.loads(capsys.readouterr().out) == {"batch": 1, "deleted": 1, "total_deleted": 1}
    assert counts(seeded_database) == (3, 3)


def test_cli_rejects_max_batches_outside_range(
    seeded_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(
            [
                "--database",
                str(seeded_database),
                "--before",
                "1970-01-01T00:01:00Z",
                "--max-batches",
                "0",
            ]
        )

    assert excinfo.value.code == 2
    assert "--max-batches must be between 1 and 1000" in capsys.readouterr().err
    assert counts(seeded_database) == (4, 4)


@pytest.mark.parametrize(
    ("argv_tail", "message"),
    [
        (["--before", "2026-09-01"], "--before must include a timezone"),
        (["--before", "1970-01-01T00:01:00Z", "--batch-size", "0"], "batch_size must be"),
    ],
)
def test_cli_reports_validation_errors_without_deleting(
    seeded_database: Path,
    capsys: pytest.CaptureFixture[str],
    argv_tail: list[str],
    message: str,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(["--database", str(seeded_database), *argv_tail])

    assert excinfo.value.code == 1
    assert f"Pruning stopped after 0 committed deletions: {message}" in capsys.readouterr().err
    assert counts(seeded_database) == (4, 4)


def test_cli_reports_committed_deletions_when_a_later_batch_fails(
    seeded_database: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_prune_batch = maintenance.prune_batch
    calls = 0

    def flaky_prune_batch(*args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_prune_batch(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(maintenance, "prune_batch", flaky_prune_batch)
    monkeypatch.setattr("arb.maintenance.time.sleep", lambda _seconds: None)

    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(
            [
                "--database",
                str(seeded_database),
                "--before",
                "1970-01-01T00:01:00Z",
                "--batch-size",
                "1",
                "--max-batches",
                "3",
            ]
        )

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert json.loads(captured.out) == {"batch": 1, "deleted": 1, "total_deleted": 1}
    assert "Pruning stopped after 1 committed deletions: database is locked" in captured.err
    # The first batch committed; the failed one rolled back nothing that was kept.
    assert counts(seeded_database) == (3, 3)
