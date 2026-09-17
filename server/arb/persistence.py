from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import structlog

from arb.metrics import persistence_queue_drops_total, persistence_unflushed_rows
from arb.types import OpportunityEpisode

logger = structlog.get_logger(__name__)

PersistenceFailureReason = Literal["initialize_failed", "worker_failed"]
PersistenceState = Literal["open", "failed", "closed"]

SCHEMA_VERSION = 4

# One row per episode: a (pair, buy venue, sell venue) route from the moment
# its spread crossed the threshold to the moment it stopped. The `*_price`,
# `spread_pct`, `max_size` and `theoretical_profit` columns are the values at
# open; `peak_*` are the widest spread seen and the size and profit at that
# moment. `pricing_ledgers` holds exact decimal strings for the configured
# notionals at that open/peak observation and is replaced when the peak grows.
# `end_ns`, `close_spread_pct` and `close_reason` are NULL while the episode is
# open. The natural key is the episode's identity across the open and close
# events that write it.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS opportunity_episodes (
    id INTEGER PRIMARY KEY,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER,
    pair TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    buy_price TEXT NOT NULL,
    sell_price TEXT NOT NULL,
    spread_pct TEXT NOT NULL,
    max_size TEXT NOT NULL,
    theoretical_profit TEXT NOT NULL,
    peak_spread_pct TEXT NOT NULL,
    peak_size TEXT NOT NULL,
    peak_profit TEXT NOT NULL,
    pricing_ledgers TEXT NOT NULL,
    close_spread_pct TEXT,
    close_reason TEXT,
    UNIQUE (start_ns, pair, buy_exchange, sell_exchange)
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_episodes_start ON opportunity_episodes(start_ns);
CREATE INDEX IF NOT EXISTS idx_episodes_pair_start ON opportunity_episodes(pair, start_ns);
"""

# Open and close events share one statement. An open inserts the row; a close
# finds it by natural key and fills in the end and peak. Should the open have
# been dropped from the bounded queue, the close still inserts a complete row.
UPSERT_EPISODE_SQL = """
INSERT INTO opportunity_episodes (
    start_ns, end_ns, pair, quote_asset, buy_exchange, sell_exchange,
    buy_price, sell_price, spread_pct, max_size, theoretical_profit,
    peak_spread_pct, peak_size, peak_profit, pricing_ledgers, close_spread_pct, close_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(start_ns, pair, buy_exchange, sell_exchange) DO UPDATE SET
    end_ns = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.end_ns ELSE excluded.end_ns END,
    peak_spread_pct = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.peak_spread_pct ELSE excluded.peak_spread_pct END,
    peak_size = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.peak_size ELSE excluded.peak_size END,
    peak_profit = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.peak_profit ELSE excluded.peak_profit END,
    pricing_ledgers = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.pricing_ledgers ELSE excluded.pricing_ledgers END,
    close_spread_pct = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.close_spread_pct ELSE excluded.close_spread_pct END,
    close_reason = CASE WHEN opportunity_episodes.end_ns IS NOT NULL
        THEN opportunity_episodes.close_reason ELSE excluded.close_reason END
"""

MINUTE_NS = 60_000_000_000

# Per-minute, per-pair totals keyed by episode start, rebuilt for each affected
# minute as canonical episodes are written. This makes retries and a close
# whose bounded-queue open was dropped idempotent without blocking ingestion.
# Aggregates are REAL because the queries they replace already computed
# MAX/AVG/SUM through CAST(... AS REAL); exact decimal values stay in
# `opportunity_episodes`, which remains the source of truth.
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

# Column list the readers and the pruner's rebuild share with the writer.
ROLLUP_REBUILD_SELECT = (
    "SELECT ?, pair, COUNT(*), MAX(CAST(peak_spread_pct AS REAL)), "
    "SUM(CAST(peak_spread_pct AS REAL)), quote_asset, SUM(CAST(peak_profit AS REAL)) "
    "FROM opportunity_episodes WHERE pair = ? AND start_ns >= ? AND start_ns < ? "
    "GROUP BY pair, quote_asset"
)


def _episode_row(episode: OpportunityEpisode) -> tuple[object, ...]:
    return (
        episode.start_ns,
        episode.end_ns,
        episode.pair,
        episode.quote_asset,
        episode.buy_exchange,
        episode.sell_exchange,
        str(episode.buy_price),
        str(episode.sell_price),
        str(episode.spread_pct),
        str(episode.max_size),
        str(episode.theoretical_profit),
        str(episode.peak_spread_pct),
        str(episode.peak_size),
        str(episode.peak_profit),
        json.dumps(
            [ledger.as_payload() for ledger in episode.pricing_ledgers], separators=(",", ":")
        ),
        None if episode.close_spread_pct is None else str(episode.close_spread_pct),
        episode.close_reason,
    )


def lifetime_summary(durations_ns: list[int]) -> dict[str, object] | None:
    """Nearest-rank p50/p90 and max of closed-episode lifetimes, in seconds."""
    if not durations_ns:
        return None
    ordered = sorted(durations_ns)

    def rank(fraction: float) -> float:
        index = min(len(ordered) - 1, int(fraction * len(ordered)))
        return ordered[index] / 1_000_000_000

    return {
        "closed_count": len(ordered),
        "p50_seconds": rank(0.5),
        "p90_seconds": rank(0.9),
        "max_seconds": ordered[-1] / 1_000_000_000,
    }


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
        self._queue: asyncio.Queue[OpportunityEpisode | None] = asyncio.Queue(maxsize=queue_maxsize)
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
                cursor = await db.execute("PRAGMA user_version")
                version = int((await cursor.fetchone() or (0,))[0])
                if version < 3:
                    # Schema 2 and earlier stored one row per book update while a
                    # spread persisted. Those samples cannot be folded into
                    # episodes after the fact (the book stream between them is
                    # gone), so they and their rollup are dropped, as the
                    # quote-currency migration dropped mislabelled amounts.
                    await db.executescript(
                        "DROP TABLE IF EXISTS opportunity_minutes; "
                        "DROP TABLE IF EXISTS opportunities;"
                    )
                await db.executescript(CREATE_TABLE_SQL + CREATE_INDEX_SQL + CREATE_ROLLUP_SQL)
                if version == 3:
                    await db.execute(
                        "ALTER TABLE opportunity_episodes "
                        "ADD COLUMN pricing_ledgers TEXT NOT NULL DEFAULT '[]'"
                    )
                # Episodes a previous process left open never got a close event.
                # They keep their count but no lifetime; the marker keeps them
                # out of the open set.
                await db.execute(
                    "UPDATE opportunity_episodes SET close_reason = 'orphaned' "
                    "WHERE end_ns IS NULL AND close_reason IS NULL"
                )
                await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                await db.commit()
        except Exception as exc:
            self._mark_failed("initialize_failed", exc)
            raise

    async def enqueue(self, episode: OpportunityEpisode) -> bool:
        if self._closed:
            persistence_queue_drops_total.labels(
                reason=self._failure_reason or "store_closed"
            ).inc()
            return False
        try:
            self._queue.put_nowait(episode)
        except asyncio.QueueFull:
            persistence_queue_drops_total.labels(reason="queue_full").inc()
            return False
        self._accepted_count += 1
        persistence_unflushed_rows.set(self.unflushed_count)
        return True

    async def run(self) -> None:
        batch: list[OpportunityEpisode] = []
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
        SELECT start_ns, end_ns, pair, quote_asset, buy_exchange, sell_exchange, buy_price,
               sell_price, spread_pct, max_size, theoretical_profit, peak_spread_pct, peak_size,
               peak_profit, pricing_ledgers, close_spread_pct, close_reason
        FROM opportunity_episodes
        ORDER BY start_ns DESC, id DESC
        LIMIT ?
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, (limit,))
            rows = await cursor.fetchall()
        return [_episode_payload(row) for row in rows]

    async def open_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM opportunity_episodes "
                "WHERE end_ns IS NULL AND close_reason IS NULL"
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def lifetimes(self, window_ns: int | None) -> dict[str, object] | None:
        """Lifetime distribution of episodes that started in the window and have closed."""
        cutoff_ns = 0 if window_ns is None else time.time_ns() - window_ns
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT end_ns - start_ns FROM opportunity_episodes "
                "WHERE start_ns >= ? AND end_ns IS NOT NULL",
                (cutoff_ns,),
            )
            rows = await cursor.fetchall()
        return lifetime_summary([int(row[0]) for row in rows])

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
            "SELECT COUNT(*), MAX(CAST(peak_spread_pct AS REAL)), "
            "COALESCE(SUM(CAST(peak_spread_pct AS REAL)), 0) "
            "FROM opportunity_episodes WHERE start_ns >= ? AND start_ns < ?",
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
                "SELECT quote_asset, SUM(CAST(peak_profit AS REAL)) FROM opportunity_episodes "
                "WHERE start_ns >= ? AND start_ns < ? GROUP BY quote_asset",
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
            "SELECT pair, COUNT(*) FROM opportunity_episodes "
            "WHERE start_ns >= ? AND start_ns < ? GROUP BY pair",
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
                "SELECT COUNT(*) FROM opportunity_episodes WHERE start_ns >= ? AND start_ns < ?",
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
            "SELECT start_ns / ?, COUNT(*), MAX(CAST(peak_spread_pct AS REAL)) "
            "FROM opportunity_episodes WHERE start_ns >= ?"
        )
        async with aiosqlite.connect(self.db_path) as db:
            if bucket_ns % MINUTE_NS == 0:
                # Whole-minute buckets align with the rollup, so read it rather
                # than every stored episode in the window.
                boundary_ns = _minute_boundary(cutoff_ns)
                cursor = await db.execute(
                    "SELECT minute_ns / ?, SUM(count), MAX(max_spread_pct) "
                    "FROM opportunity_minutes WHERE minute_ns >= ? GROUP BY 1",
                    (bucket_ns, boundary_ns),
                )
                for bucket, count, max_spread in await cursor.fetchall():
                    add(int(bucket), int(count), float(max_spread))
                cursor = await db.execute(
                    raw_query + " AND start_ns < ? GROUP BY 1",
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

    async def _flush(self, batch: Iterable[OpportunityEpisode]) -> None:
        batch = list(batch)
        affected = sorted(
            {((episode.start_ns // MINUTE_NS) * MINUTE_NS, episode.pair) for episode in batch}
        )
        db = self._db
        if db is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            db = self._db = await aiosqlite.connect(self.db_path)
        # Events are applied in arrival order, so an open and its close in
        # the same batch insert and then update the same row.
        await db.executemany(UPSERT_EPISODE_SQL, [_episode_row(episode) for episode in batch])
        for minute_ns, pair in affected:
            await db.execute(
                "DELETE FROM opportunity_minutes WHERE minute_ns = ? AND pair = ?",
                (minute_ns, pair),
            )
            await db.execute(
                "INSERT INTO opportunity_minutes "
                "(minute_ns, pair, count, max_spread_pct, sum_spread_pct, "
                "quote_asset, sum_profit) " + ROLLUP_REBUILD_SELECT,
                (minute_ns, pair, minute_ns, minute_ns + MINUTE_NS),
            )
        # Canonical rows and their rebuilt derived minutes commit together.
        # Replaying an open or close therefore cannot increment a rollup twice,
        # and a close whose bounded-queue open was dropped still contributes.
        await db.commit()
        self._flushed_count += len(batch)
        persistence_unflushed_rows.set(self.unflushed_count)


def _episode_payload(row: Any) -> dict[str, Any]:
    start_ns, end_ns = int(row[0]), row[1]
    return {
        "start_ns": start_ns,
        "end_ns": None if end_ns is None else int(end_ns),
        "duration_ns": None if end_ns is None else int(end_ns) - start_ns,
        "pair": row[2],
        "quote_asset": row[3],
        "buy_exchange": row[4],
        "sell_exchange": row[5],
        "buy_price": row[6],
        "sell_price": row[7],
        "spread_pct": row[8],
        "max_size": row[9],
        "theoretical_profit": row[10],
        "peak_spread_pct": row[11],
        "peak_size": row[12],
        "peak_profit": row[13],
        "pricing_ledgers": json.loads(row[14]),
        "close_spread_pct": row[15],
        "close_reason": row[16],
    }
