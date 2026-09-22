"""Explicit, bounded history pruning. Never run on the ingestion event loop."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from arb.persistence import MINUTE_NS, ROLLUP_REBUILD_SELECT, SCHEMA_VERSION


def prune_batch(
    database: Path,
    cutoff_ns: int,
    *,
    batch_size: int = 1000,
    timeout_seconds: float = 0.5,
) -> int:
    """Delete at most batch_size rows of each kind before cutoff, atomically repairing rollups.

    Episodes that started before the cutoff are deleted with their minute rollups
    rebuilt. Fill-rate buckets are deleted only for whole minutes that ended at
    or before the cutoff, then tick rows and sessions left with no buckets. The
    return value counts deleted episodes plus deleted fill-rate bucket rows.

    A SQLite progress deadline bounds query work (including dense-minute rebuilds).
    Lock contention or expiry raises OperationalError and rolls back the whole batch.
    Filesystem stalls and rollback I/O are not covered by the VM progress deadline.
    """
    if not 0 <= cutoff_ns <= 2**63 - 1:
        raise ValueError("cutoff_ns must be a non-negative SQLite integer")
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 2:
        raise ValueError("timeout_seconds must be finite and in (0, 2]")
    # mode=rw refuses to create a new database after a mistyped path.
    uri = database.resolve(strict=True).as_uri() + "?mode=rw"
    db = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError(
                f"Expected schema version {SCHEMA_VERSION}; start the current application first"
            )
        db.execute("BEGIN IMMEDIATE")
        # Episodes are pruned by start time, open or not: an open episode older
        # than the cutoff is one a previous process never closed.
        rows = db.execute(
            "SELECT id, start_ns, pair FROM opportunity_episodes "
            "WHERE start_ns < ? ORDER BY start_ns, id LIMIT ?",
            (cutoff_ns, batch_size),
        ).fetchall()
        affected = {(start // MINUTE_NS * MINUTE_NS, pair) for _, start, pair in rows}
        db.executemany("DELETE FROM opportunity_episodes WHERE id = ?", [(row[0],) for row in rows])
        for minute, pair in affected:
            db.execute(
                "DELETE FROM opportunity_minutes WHERE minute_ns = ? AND pair = ?",
                (minute, pair),
            )
            # Recompute MAX as well as sums: subtracting a deleted maximum is incorrect.
            db.execute(
                "INSERT INTO opportunity_minutes "
                "(minute_ns, pair, count, max_spread_pct, sum_spread_pct, quote_asset, sum_profit) "
                + ROLLUP_REBUILD_SELECT,
                (minute, pair, minute, minute + MINUTE_NS),
            )
        # A bucket covers [minute_ns, minute_ns + 1 minute); only whole minutes
        # before the cutoff go, so a window after the cutoff keeps its counts.
        last_minute_ns = cutoff_ns - MINUTE_NS
        fill_rows = db.execute(
            "DELETE FROM fill_rate_minutes WHERE (minute_ns, session_id, exchange, pair, "
            "notional, side) IN (SELECT minute_ns, session_id, exchange, pair, notional, side "
            "FROM fill_rate_minutes WHERE minute_ns <= ? ORDER BY minute_ns LIMIT ?)",
            (last_minute_ns, batch_size),
        ).rowcount
        db.execute(
            "DELETE FROM fill_rate_ticks WHERE minute_ns <= ? AND NOT EXISTS ("
            "SELECT 1 FROM fill_rate_minutes m WHERE m.minute_ns = fill_rate_ticks.minute_ns "
            "AND m.session_id = fill_rate_ticks.session_id)",
            (last_minute_ns,),
        )
        db.execute(
            "DELETE FROM fill_rate_sessions WHERE started_wall_ns < ? AND NOT EXISTS ("
            "SELECT 1 FROM fill_rate_ticks t WHERE t.session_id = fill_rate_sessions.session_id)",
            (cutoff_ns,),
        )
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("Pruning time budget expired")
        db.commit()
        return len(rows) + max(0, fill_rows)
    finally:
        db.set_progress_handler(None, 0)
        try:
            db.rollback()
        finally:
            db.close()


def parse_cutoff(value: str) -> int:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("--before must include a timezone, for example 2026-09-01T00:00:00Z")
    delta = timestamp.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--before", required=True, help="Exclusive ISO-8601 timestamp with timezone"
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)
    if not 1 <= args.max_batches <= 1000:
        parser.error("--max-batches must be between 1 and 1000")
    deleted = 0
    try:
        cutoff = parse_cutoff(args.before)
        for index in range(args.max_batches):
            count = prune_batch(
                args.database,
                cutoff,
                batch_size=args.batch_size,
                timeout_seconds=args.timeout_seconds,
            )
            deleted += count
            print(json.dumps({"batch": index + 1, "deleted": count, "total_deleted": deleted}))
            if count < args.batch_size:
                break
            if index + 1 < args.max_batches:
                time.sleep(0.1)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Pruning stopped after {deleted} committed deletions: {exc}\n")


if __name__ == "__main__":
    main()
