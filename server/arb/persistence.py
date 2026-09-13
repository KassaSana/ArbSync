from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import structlog

from arb.metrics import persistence_queue_drops_total, persistence_unflushed_rows
from arb.types import ArbitrageOpportunity

logger = structlog.get_logger(__name__)

PersistenceFailureReason = Literal["initialize_failed", "worker_failed"]
PersistenceState = Literal["open", "failed", "closed"]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY,
    timestamp_ns INTEGER NOT NULL,
    pair TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    buy_price TEXT NOT NULL,
    sell_price TEXT NOT NULL,
    spread_pct TEXT NOT NULL,
    max_size TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    theoretical_profit TEXT NOT NULL
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_opps_timestamp ON opportunities(timestamp_ns);
CREATE INDEX IF NOT EXISTS idx_opps_pair_ts ON opportunities(pair, timestamp_ns);
"""

MINUTE_NS = 60_000_000_000

# Per-minute, per-pair totals maintained as opportunities are written, so the
# statistics endpoints read one row per minute and pair instead of every stored
# opportunity. Aggregates are REAL because the queries they replace already
# computed MAX/AVG/SUM through CAST(... AS REAL); exact decimal values stay in
# `opportunities`, which is unchanged and remains the source of truth.
CREATE_ROLLUP_SQL = """
CREATE TABLE IF NOT EXISTS opportunity_minutes (
    minute_ns INTEGER NOT NULL,
    pair TEXT NOT NULL,
    count INTEGER NOT NULL,
    max_spread_pct REAL NOT NULL,
    sum_spread_pct REAL NOT NULL,
    quote_asset TEXT NOT NULL,
    sum_profit REAL NOT NULL,
    PRIMARY KEY (minute_ns, pair)
);
CREATE INDEX IF NOT EXISTS idx_minutes_ns ON opportunity_minutes(minute_ns);
"""

UPSERT_ROLLUP_SQL = """
INSERT INTO opportunity_minutes (
    minute_ns, pair, count, max_spread_pct, sum_spread_pct, quote_asset, sum_profit
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(minute_ns, pair) DO UPDATE SET
    count = count + excluded.count,
    max_spread_pct = MAX(max_spread_pct, excluded.max_spread_pct),
    sum_spread_pct = sum_spread_pct + excluded.sum_spread_pct,
    sum_profit = sum_profit + excluded.sum_profit
"""


def _rollup_rows(batch: Iterable[ArbitrageOpportunity]) -> list[tuple[object, ...]]:
    """Fold a write batch into one row per minute and pair."""
    totals: dict[tuple[int, str], tuple[int, float, float, str, float]] = {}
    for opp in batch:
        key = ((opp.timestamp_ns // MINUTE_NS) * MINUTE_NS, opp.pair)
        spread = float(opp.spread_pct)
        profit = float(opp.theoretical_profit)
        entry = totals.get(key)
        if entry is None:
            totals[key] = (1, spread, spread, opp.quote_asset, profit)
        else:
            count, max_spread, sum_spread, quote_asset, sum_profit = entry
            totals[key] = (
                count + 1,
                max(max_spread, spread),
                sum_spread + spread,
                quote_asset,
                sum_profit + profit,
            )
    return [
        (minute_ns, pair, int(count), max_spread, sum_spread, quote_asset, sum_profit)
        for (minute_ns, pair), (
            count,
            max_spread,
            sum_spread,
            quote_asset,
            sum_profit,
        ) in totals.items()
    ]


def _minute_boundary(cutoff_ns: int) -> int:
    """Return the first whole minute at or after `cutoff_ns`.

    A window rarely starts exactly on a minute. Rows between the cutoff and this
    boundary belong to a minute the rollup only holds in full, so they are read
    from `opportunities` directly and the rollup supplies everything after it.
    That keeps cutoff membership exact rather than rounding the window out to the minute.
    """
    if cutoff_ns <= 0:
        return 0
    remainder = cutoff_ns % MINUTE_NS
    return cutoff_ns if remainder == 0 else cutoff_ns - remainder + MINUTE_NS


class OpportunityStore:
    def __init__(
        self,
        db_path: str,
        batch_size: int = 500,
        flush_interval_seconds: float = 1.0,
        queue_maxsize: int = 10_000,
    ) -> None:
        self.db_path = db_path
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self._queue: asyncio.Queue[ArbitrageOpportunity | None] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._closed = False
        self._failure: Exception | None = None
        self._failure_reason: PersistenceFailureReason | None = None
        self._failure_event = asyncio.Event()
        self._accepted_count = 0
        self._flushed_count = 0
        # Opened on the first flush and held until the worker stops, so writing
        # no longer pays connection setup and statement recompilation every
        # interval. Owned by the single `run` worker, which is also the only
        # writer: `initialize` deliberately does not open it, so a store that is
        # never run leaks no connection and no aiosqlite thread. Readers below
        # still open their own short-lived connections, because WAL allows
        # concurrent readers and sharing this one would serialize API reads onto
        # the writer's thread and interleave them with an open write transaction.
        self._db: aiosqlite.Connection | None = None
        self._worker_started = False
        persistence_unflushed_rows.set(0)

    @property
    def state(self) -> PersistenceState:
        if self._failure is not None:
            return "failed"
        return "closed" if self._closed else "open"

    @property
    def failure(self) -> Exception | None:
        return self._failure

    @property
    def failure_reason(self) -> PersistenceFailureReason | None:
        return self._failure_reason

    @property
    def unflushed_count(self) -> int:
        return self._accepted_count - self._flushed_count

    async def initialize(self) -> None:
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                cursor = await db.execute("PRAGMA table_info(opportunities)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if columns and {"quote_asset", "theoretical_profit"} - columns:
                    # Earlier releases recorded Binance.US USDT amounts as USD. Their
                    # historical opportunities and rollups cannot be relabelled safely.
                    await db.executescript(
                        "DROP TABLE IF EXISTS opportunity_minutes; DROP TABLE opportunities;"
                    )
                await db.executescript(CREATE_TABLE_SQL + CREATE_INDEX_SQL + CREATE_ROLLUP_SQL)
                await db.execute("PRAGMA user_version = 2")
                await db.commit()
        except Exception as exc:
            self._mark_failed("initialize_failed", exc)
            raise

    async def enqueue(self, opportunity: ArbitrageOpportunity) -> bool:
        if self._closed:
            persistence_queue_drops_total.labels(
                reason=self._failure_reason or "store_closed"
            ).inc()
            return False
        try:
            self._queue.put_nowait(opportunity)
        except asyncio.QueueFull:
            persistence_queue_drops_total.labels(reason="queue_full").inc()
            return False
        self._accepted_count += 1
        persistence_unflushed_rows.set(self.unflushed_count)
        return True

    async def run(self) -> None:
        batch: list[ArbitrageOpportunity] = []
        self._worker_started = True
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=self.flush_interval_seconds
                    )
                    if item is None:
                        break
                    batch.append(item)
                    if len(batch) >= self.batch_size:
                        await self._flush(batch)
                        batch.clear()
                except TimeoutError:
                    if batch:
                        await self._flush(batch)
                        batch.clear()

            if batch:
                await self._flush(batch)
        except Exception as exc:
            self._mark_failed("worker_failed", exc)
            raise
        finally:
            # The worker owns the writer connection, so it outlives `close`,
            # which only queues the sentinel this loop is still draining.
            await self._close_db()

    def _mark_failed(self, reason: PersistenceFailureReason, exception: Exception) -> None:
        if self._failure is not None:
            return
        self._failure = exception
        self._failure_reason = reason
        self._closed = True
        self._failure_event.set()
        persistence_unflushed_rows.set(self.unflushed_count)
        logger.error(
            "persistence_store_failed",
            reason=reason,
            error=repr(exception),
            unflushed_rows=self.unflushed_count,
        )

    async def _close_db(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            await db.close()

    async def close(self) -> None:
        if self._failure is not None:
            self._report_incomplete_shutdown()
            await self._close_db()
            return
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            put_sentinel = asyncio.create_task(self._queue.put(None))
            wait_for_failure = asyncio.create_task(self._failure_event.wait())
            try:
                await asyncio.wait(
                    {put_sentinel, wait_for_failure}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (put_sentinel, wait_for_failure):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(put_sentinel, wait_for_failure, return_exceptions=True)
            if self._failure is not None:
                self._report_incomplete_shutdown()
        if not self._worker_started:
            # No worker will reach the sentinel and run the `finally` that
            # closes the connection, so release it here. A worker starting
            # later still exits on the queued sentinel without touching it.
            await self._close_db()

    def _report_incomplete_shutdown(self) -> None:
        logger.error(
            "persistence_shutdown_incomplete",
            reason=self._failure_reason,
            error=repr(self._failure),
            unflushed_rows=self.unflushed_count,
        )

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
        SELECT timestamp_ns, pair, quote_asset, buy_exchange, sell_exchange, buy_price, sell_price,
               spread_pct, max_size, theoretical_profit
        FROM opportunities
        ORDER BY timestamp_ns DESC
        LIMIT ?
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, (limit,))
            rows = await cursor.fetchall()
        return [
            {
                "timestamp_ns": row[0],
                "pair": row[1],
                "quote_asset": row[2],
                "buy_exchange": row[3],
                "sell_exchange": row[4],
                "buy_price": row[5],
                "sell_price": row[6],
                "spread_pct": row[7],
                "max_size": row[8],
                "theoretical_profit": row[9],
            }
            for row in rows
        ]

    async def _windowed_totals(
        self, db: aiosqlite.Connection, cutoff_ns: int
    ) -> tuple[int, float | None, float, dict[str, float]]:
        """Return count, max spread, summed spread and summed profit since the cutoff."""
        boundary_ns = _minute_boundary(cutoff_ns)
        cursor = await db.execute(
            "SELECT COALESCE(SUM(count), 0), MAX(max_spread_pct), "
            "COALESCE(SUM(sum_spread_pct), 0) "
            "FROM opportunity_minutes WHERE minute_ns >= ?",
            (boundary_ns,),
        )
        rolled = await cursor.fetchone() or (0, None, 0.0)
        cursor = await db.execute(
            "SELECT COUNT(*), MAX(CAST(spread_pct AS REAL)), "
            "COALESCE(SUM(CAST(spread_pct AS REAL)), 0) "
            "FROM opportunities WHERE timestamp_ns >= ? AND timestamp_ns < ?",
            (cutoff_ns, boundary_ns),
        )
        partial = await cursor.fetchone() or (0, None, 0.0)
        profits: dict[str, float] = {}
        for query, params in (
            (
                "SELECT quote_asset, SUM(sum_profit) FROM opportunity_minutes "
                "WHERE minute_ns >= ? GROUP BY quote_asset",
                (boundary_ns,),
            ),
            (
                "SELECT quote_asset, SUM(CAST(theoretical_profit AS REAL)) FROM opportunities "
                "WHERE timestamp_ns >= ? AND timestamp_ns < ? GROUP BY quote_asset",
                (cutoff_ns, boundary_ns),
            ),
        ):
            cursor = await db.execute(query, params)
            for quote_asset, profit in await cursor.fetchall():
                profits[str(quote_asset)] = profits.get(str(quote_asset), 0.0) + float(profit)
        maxima = [value for value in (rolled[1], partial[1]) if value is not None]
        return (
            int(rolled[0]) + int(partial[0]),
            max(maxima) if maxima else None,
            float(rolled[2]) + float(partial[2]),
            profits,
        )

    async def _windowed_pair_counts(
        self, db: aiosqlite.Connection, cutoff_ns: int
    ) -> dict[str, int]:
        boundary_ns = _minute_boundary(cutoff_ns)
        counts: dict[str, int] = {}
        cursor = await db.execute(
            "SELECT pair, SUM(count) FROM opportunity_minutes WHERE minute_ns >= ? GROUP BY pair",
            (boundary_ns,),
        )
        for pair, count in await cursor.fetchall():
            counts[pair] = counts.get(pair, 0) + int(count)
        cursor = await db.execute(
            "SELECT pair, COUNT(*) FROM opportunities "
            "WHERE timestamp_ns >= ? AND timestamp_ns < ? GROUP BY pair",
            (cutoff_ns, boundary_ns),
        )
        for pair, count in await cursor.fetchall():
            counts[pair] = counts.get(pair, 0) + int(count)
        return counts

    async def stats(self, window_ns: int) -> dict[str, object]:
        cutoff_ns = time.time_ns() - window_ns
        async with aiosqlite.connect(self.db_path) as db:
            count, max_spread, _spread, profits = await self._windowed_totals(db, cutoff_ns)
        return {
            "count": count,
            "max_spread_pct": str(Decimal(str(max_spread if max_spread is not None else 0))),
            "theoretical_profit_by_quote": {
                quote_asset: str(Decimal(str(profit)))
                for quote_asset, profit in sorted(profits.items())
            },
        }

    async def extended_stats(self, window_ns: int | None) -> dict[str, object]:
        cutoff_ns = 0 if window_ns is None else time.time_ns() - window_ns
        async with aiosqlite.connect(self.db_path) as db:
            count, max_spread, sum_spread, profits = await self._windowed_totals(db, cutoff_ns)
            pair_counts = await self._windowed_pair_counts(db, cutoff_ns)
        mean_spread = sum_spread / count if count else 0
        top_pair = (
            max(sorted(pair_counts), key=lambda pair: pair_counts[pair]) if pair_counts else None
        )
        return {
            "count": count,
            "max_spread_pct": str(Decimal(str(max_spread if max_spread is not None else 0))),
            "mean_spread_pct": str(Decimal(str(mean_spread))),
            "theoretical_profit_by_quote": {
                quote_asset: str(Decimal(str(profit)))
                for quote_asset, profit in sorted(profits.items())
            },
            "top_pair": top_pair,
        }

    async def peak_minute(self, window_ns: int | None) -> dict[str, int] | None:
        cutoff_ns = 0 if window_ns is None else time.time_ns() - window_ns
        boundary_ns = _minute_boundary(cutoff_ns)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT minute_ns, SUM(count) AS c FROM opportunity_minutes "
                "WHERE minute_ns >= ? GROUP BY minute_ns ORDER BY c DESC, minute_ns DESC LIMIT 1",
                (boundary_ns,),
            )
            rolled = await cursor.fetchone()
            # The cutoff can fall inside a minute the rollup only holds whole, so
            # that minute is counted from the rows actually inside the window.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM opportunities WHERE timestamp_ns >= ? AND timestamp_ns < ?",
                (cutoff_ns, boundary_ns),
            )
            partial = await cursor.fetchone()
        candidates: list[tuple[int, int]] = []
        if rolled is not None and rolled[1]:
            candidates.append((int(rolled[1]), int(rolled[0])))
        if partial is not None and partial[0]:
            candidates.append((int(partial[0]), boundary_ns - MINUTE_NS))
        if not candidates:
            return None
        count, minute_start_ns = max(candidates)
        return {"minute_start_ns": minute_start_ns, "count": count}

    async def timeseries(self, window_ns: int, bucket_seconds: int) -> list[dict[str, int | str]]:
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        bucket_ns = bucket_seconds * 1_000_000_000
        cutoff_ns = time.time_ns() - window_ns
        buckets: dict[int, tuple[int, float]] = {}

        def add(bucket: int, count: int, max_spread: float) -> None:
            existing = buckets.get(bucket)
            if existing is None:
                buckets[bucket] = (count, max_spread)
            else:
                buckets[bucket] = (existing[0] + count, max(existing[1], max_spread))

        raw_query = (
            "SELECT timestamp_ns / ?, COUNT(*), MAX(CAST(spread_pct AS REAL)) "
            "FROM opportunities WHERE timestamp_ns >= ?"
        )
        async with aiosqlite.connect(self.db_path) as db:
            if bucket_ns % MINUTE_NS == 0:
                # Whole-minute buckets align with the rollup, so read it rather
                # than every stored opportunity in the window.
                boundary_ns = _minute_boundary(cutoff_ns)
                cursor = await db.execute(
                    "SELECT minute_ns / ?, SUM(count), MAX(max_spread_pct) "
                    "FROM opportunity_minutes WHERE minute_ns >= ? GROUP BY 1",
                    (bucket_ns, boundary_ns),
                )
                for bucket, count, max_spread in await cursor.fetchall():
                    add(int(bucket), int(count), float(max_spread))
                cursor = await db.execute(
                    raw_query + " AND timestamp_ns < ? GROUP BY 1",
                    (bucket_ns, cutoff_ns, boundary_ns),
                )
            else:
                cursor = await db.execute(raw_query + " GROUP BY 1", (bucket_ns, cutoff_ns))
            for bucket, count, max_spread in await cursor.fetchall():
                add(int(bucket), int(count), float(max_spread))
        return [
            {
                "bucket_start_ns": bucket * bucket_ns,
                "count": buckets[bucket][0],
                "max_spread_pct": str(Decimal(str(buckets[bucket][1]))),
            }
            for bucket in sorted(buckets)
        ]

    async def _flush(self, batch: Iterable[ArbitrageOpportunity]) -> None:
        batch = list(batch)
        rows = [
            (
                opp.timestamp_ns,
                opp.pair,
                opp.buy_exchange,
                opp.sell_exchange,
                str(opp.buy_price),
                str(opp.sell_price),
                str(opp.spread_pct),
                str(opp.max_size),
                opp.quote_asset,
                str(opp.theoretical_profit),
            )
            for opp in batch
        ]
        db = self._db
        if db is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            db = self._db = await aiosqlite.connect(self.db_path)
        await db.executemany(
            """
            INSERT INTO opportunities (
                timestamp_ns, pair, buy_exchange, sell_exchange, buy_price, sell_price,
                spread_pct, max_size, quote_asset, theoretical_profit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.executemany(UPSERT_ROLLUP_SQL, _rollup_rows(batch))
        # One transaction, so the rollup can never record opportunities the
        # table does not hold, or miss ones it does.
        await db.commit()
        self._flushed_count += len(batch)
        persistence_unflushed_rows.set(self.unflushed_count)
