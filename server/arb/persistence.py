from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import aiosqlite

from arb.metrics import persistence_queue_drops_total
from arb.types import ArbitrageOpportunity

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
    theoretical_profit_usd TEXT NOT NULL
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
    sum_profit_usd REAL NOT NULL,
    PRIMARY KEY (minute_ns, pair)
);
CREATE INDEX IF NOT EXISTS idx_minutes_ns ON opportunity_minutes(minute_ns);
"""

BACKFILL_ROLLUP_SQL = """
INSERT INTO opportunity_minutes (
    minute_ns, pair, count, max_spread_pct, sum_spread_pct, sum_profit_usd
)
SELECT (timestamp_ns / ?) * ?,
       pair,
       COUNT(*),
       MAX(CAST(spread_pct AS REAL)),
       SUM(CAST(spread_pct AS REAL)),
       SUM(CAST(theoretical_profit_usd AS REAL))
FROM opportunities
GROUP BY 1, 2
"""

UPSERT_ROLLUP_SQL = """
INSERT INTO opportunity_minutes (
    minute_ns, pair, count, max_spread_pct, sum_spread_pct, sum_profit_usd
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(minute_ns, pair) DO UPDATE SET
    count = count + excluded.count,
    max_spread_pct = MAX(max_spread_pct, excluded.max_spread_pct),
    sum_spread_pct = sum_spread_pct + excluded.sum_spread_pct,
    sum_profit_usd = sum_profit_usd + excluded.sum_profit_usd
"""


def _rollup_rows(batch: Iterable[ArbitrageOpportunity]) -> list[tuple[object, ...]]:
    """Fold a write batch into one row per minute and pair."""
    totals: dict[tuple[int, str], list[float]] = {}
    for opp in batch:
        key = ((opp.timestamp_ns // MINUTE_NS) * MINUTE_NS, opp.pair)
        spread = float(opp.spread_pct)
        profit = float(opp.theoretical_profit_usd)
        entry = totals.get(key)
        if entry is None:
            totals[key] = [1, spread, spread, profit]
        else:
            entry[0] += 1
            entry[1] = max(entry[1], spread)
            entry[2] += spread
            entry[3] += profit
    return [
        (minute_ns, pair, int(count), max_spread, sum_spread, sum_profit)
        for (minute_ns, pair), (count, max_spread, sum_spread, sum_profit) in totals.items()
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

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.executescript(CREATE_TABLE_SQL + CREATE_INDEX_SQL + CREATE_ROLLUP_SQL)
            # A database written before the rollup existed still holds history
            # the statistics endpoints must report, so derive it once here.
            cursor = await db.execute("SELECT EXISTS (SELECT 1 FROM opportunity_minutes)")
            row = await cursor.fetchone()
            if row is not None and not row[0]:
                await db.execute(BACKFILL_ROLLUP_SQL, (MINUTE_NS, MINUTE_NS))
            await db.commit()

    async def enqueue(self, opportunity: ArbitrageOpportunity) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait(opportunity)
        except asyncio.QueueFull:
            persistence_queue_drops_total.inc()
            return False
        return True

    async def run(self) -> None:
        batch: list[ArbitrageOpportunity] = []
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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
        SELECT timestamp_ns, pair, buy_exchange, sell_exchange, buy_price, sell_price,
               spread_pct, max_size, theoretical_profit_usd
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
                "buy_exchange": row[2],
                "sell_exchange": row[3],
                "buy_price": row[4],
                "sell_price": row[5],
                "spread_pct": row[6],
                "max_size": row[7],
                "theoretical_profit_usd": row[8],
            }
            for row in rows
        ]

    async def _windowed_totals(
        self, db: aiosqlite.Connection, cutoff_ns: int
    ) -> tuple[int, float | None, float, float]:
        """Return count, max spread, summed spread and summed profit since the cutoff."""
        boundary_ns = _minute_boundary(cutoff_ns)
        cursor = await db.execute(
            "SELECT COALESCE(SUM(count), 0), MAX(max_spread_pct), "
            "COALESCE(SUM(sum_spread_pct), 0), COALESCE(SUM(sum_profit_usd), 0) "
            "FROM opportunity_minutes WHERE minute_ns >= ?",
            (boundary_ns,),
        )
        rolled = await cursor.fetchone() or (0, None, 0.0, 0.0)
        cursor = await db.execute(
            "SELECT COUNT(*), MAX(CAST(spread_pct AS REAL)), "
            "COALESCE(SUM(CAST(spread_pct AS REAL)), 0), "
            "COALESCE(SUM(CAST(theoretical_profit_usd AS REAL)), 0) "
            "FROM opportunities WHERE timestamp_ns >= ? AND timestamp_ns < ?",
            (cutoff_ns, boundary_ns),
        )
        partial = await cursor.fetchone() or (0, None, 0.0, 0.0)
        maxima = [value for value in (rolled[1], partial[1]) if value is not None]
        return (
            int(rolled[0]) + int(partial[0]),
            max(maxima) if maxima else None,
            float(rolled[2]) + float(partial[2]),
            float(rolled[3]) + float(partial[3]),
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

    async def stats(self, window_ns: int) -> dict[str, str | int]:
        cutoff_ns = time.time_ns() - window_ns
        async with aiosqlite.connect(self.db_path) as db:
            count, max_spread, _spread, profit = await self._windowed_totals(db, cutoff_ns)
        return {
            "count": count,
            "max_spread_pct": str(Decimal(str(max_spread if max_spread is not None else 0))),
            "total_theoretical_profit_usd": str(Decimal(str(profit))),
        }

    async def extended_stats(self, window_ns: int | None) -> dict[str, str | int | None]:
        cutoff_ns = 0 if window_ns is None else time.time_ns() - window_ns
        async with aiosqlite.connect(self.db_path) as db:
            count, max_spread, sum_spread, profit = await self._windowed_totals(db, cutoff_ns)
            pair_counts = await self._windowed_pair_counts(db, cutoff_ns)
        mean_spread = sum_spread / count if count else 0
        top_pair = (
            max(sorted(pair_counts), key=lambda pair: pair_counts[pair]) if pair_counts else None
        )
        return {
            "count": count,
            "max_spread_pct": str(Decimal(str(max_spread if max_spread is not None else 0))),
            "mean_spread_pct": str(Decimal(str(mean_spread))),
            "total_theoretical_profit_usd": str(Decimal(str(profit))),
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
                str(opp.theoretical_profit_usd),
            )
            for opp in batch
        ]
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT INTO opportunities (
                    timestamp_ns, pair, buy_exchange, sell_exchange, buy_price, sell_price,
                    spread_pct, max_size, theoretical_profit_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await db.executemany(UPSERT_ROLLUP_SQL, _rollup_rows(batch))
            # One transaction, so the rollup can never record opportunities the
            # table does not hold, or miss ones it does.
            await db.commit()
