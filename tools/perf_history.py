"""Measure filtered history pages and JSONL export on a representative database.

Seeds a schema-v4 SQLite file with synthetic episodes spread over a period,
using the venue-direction mix of a real local database and a real
``pricing_ledgers`` payload so rows have production size. It then times the
first and a deep page of ``OpportunityStore.history_page`` for each filter
shape, reports each query plan and whether the default per-page budget would
have been exceeded, and times a maximum-size export through
``history_export_chunks`` while sampling event-loop lag against an idle baseline.
Nothing here touches a live process.

```powershell
uv run python tools/perf_history.py --rows 1000000 `
  --output artifacts/benchmarks/performance/history-<date>.json
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from arb.api import HISTORY_EXPORT_MAX_ROWS
from arb.history import HistoryCursor, HistoryFilters, build_page_query
from arb.persistence import (
    CREATE_INDEX_SQL,
    HISTORY_EXPORT_PAGE_ROWS,
    HISTORY_QUERY_BUDGET_SECONDS,
    HistoryBudgetExceeded,
    OpportunityStore,
    _episode_export_line,
)

MINUTE_NS = 60_000_000_000
PAIRS = (
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",
    "DOGE-USD",
    "LTC-USD",
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
)
# (buy, sell) weights from the venue mix of a real local capture's episodes.
DIRECTIONS = (
    (("coinbase", "gemini"), 58),
    (("coinbase", "binance"), 14),
    (("binance", "coinbase"), 9),
    (("gemini", "coinbase"), 8),
    (("binance", "gemini"), 6),
    (("gemini", "binance"), 5),
)
CLOSE_REASONS = (("spread_closed", 989), ("book_ineligible", 10), ("shutdown", 1))
# One production ledger payload (three notionals), so rows have realistic width.
LEDGERS = json.dumps(
    [
        {
            "notional": notional,
            "top_of_book_spread_pct": "0.2274914690699098783795607665",
            "buy_vwap": "1.143295998614096749359435863",
            "sell_vwap": "1.1455",
            "gross_executable_spread_pct": "0.19277609547964313828391500",
            "depth_impact_pct": "-0.0347153735902667400956457665",
            "buy_taker_fee_pct": "0.6",
            "sell_taker_fee_pct": "0.4",
            "fee_impact_pct": "-1.000771104381918572553135660",
            "net_executable_spread_pct": "-0.8079950089020754142697206600",
            "filled_base": "87.46638935",
            "filled_quote": "100",
            "insufficient_depth": False,
        }
        for notional in ("100", "1000", "10000")
    ],
    separators=(",", ":"),
)


def seed(path: Path, rows: int, days: int, seed_value: int) -> tuple[int, int]:
    """Write `rows` episodes over `days`, newest last; return the start range."""
    asyncio.run(OpportunityStore(str(path)).initialize())
    rng = random.Random(seed_value)
    end_ns = 1_790_000_000_000_000_000
    start_ns = end_ns - days * 86_400_000_000_000
    step = (end_ns - start_ns) // rows
    directions = [d for d, _ in DIRECTIONS]
    direction_weights = [w for _, w in DIRECTIONS]
    reasons = [r for r, _ in CLOSE_REASONS]
    reason_weights = [w for _, w in CLOSE_REASONS]
    sql = (
        "INSERT INTO opportunity_episodes (start_ns, end_ns, pair, quote_asset, "
        "buy_exchange, sell_exchange, buy_price, sell_price, spread_pct, max_size, "
        "theoretical_profit, peak_spread_pct, peak_size, peak_profit, pricing_ledgers, "
        "close_spread_pct, close_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    with closing(sqlite3.connect(path)) as db:
        batch: list[tuple[object, ...]] = []
        for index in range(rows):
            start = start_ns + index * step + rng.randrange(step)
            pair = rng.choice(PAIRS)
            buy, sell = rng.choices(directions, direction_weights)[0]
            spread = f"{rng.uniform(0.01, 0.6):.6f}"
            is_open = index >= rows - 5
            reason = None if is_open else rng.choices(reasons, reason_weights)[0]
            batch.append(
                (
                    start,
                    None if is_open else start + rng.randrange(1_000_000, 5_000_000_000),
                    pair,
                    pair.rsplit("-", 1)[1],
                    buy,
                    sell,
                    "100.25",
                    "100.75",
                    spread,
                    "0.5",
                    "0.25",
                    spread,
                    "0.5",
                    "0.25",
                    LEDGERS,
                    None if is_open else "0",
                    reason,
                )
            )
            if len(batch) == 50_000:
                db.executemany(sql, batch)
                batch.clear()
        db.executemany(sql, batch)
        db.commit()
    return start_ns, end_ns


def plan(path: Path, filters: HistoryFilters, after: tuple[int, int] | None) -> str:
    sql, params = build_page_query(filters, after, 2**62, 101)
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(row[-1]) for row in rows)


async def time_page(store: OpportunityStore, filters: HistoryFilters, deep: bool) -> dict[str, Any]:
    """Time one 100-row page; for `deep`, start from a cursor half-way through the matches."""
    cursor = None
    if deep:
        first = await store.history_page(filters, None, 1, budget_seconds=600)
        if first.next_cursor is None:
            return {"skipped": "fewer than two matches"}
        # Jump the cursor to the middle of the matching rows, as a client that
        # had already paged through half of them would hold.
        snapshot = first.next_cursor.snapshot_max_id
        sql, params = build_page_query(filters, None, snapshot, 1)
        _, rest = sql.split(" FROM ", 1)
        where = rest.split(" ORDER BY ", 1)[0]
        with closing(sqlite3.connect(store.db_path)) as db:
            total = db.execute(f"SELECT COUNT(*) FROM {where}", params[:-1]).fetchone()[0]
            middle = db.execute(
                sql.replace("LIMIT ?", "LIMIT 1 OFFSET ?"), [*params[:-1], total // 2]
            ).fetchone()
        cursor = HistoryCursor(int(middle[1]), int(middle[0]), snapshot, filters.fingerprint())
    started = time.perf_counter()
    try:
        page = await store.history_page(filters, cursor, 100, budget_seconds=600)
    except HistoryBudgetExceeded:  # pragma: no cover - 600 s is effectively unbounded
        return {"error": "budget"}
    elapsed = time.perf_counter() - started
    return {
        "ms": round(elapsed * 1000, 2),
        "rows": len(page.items),
        "within_default_budget": elapsed < HISTORY_QUERY_BUDGET_SECONDS,
    }


async def loop_lag(duration: float | None, work: Any = None) -> dict[str, float]:
    """Event-loop scheduling lag of a 1 ms sleeper, idle for `duration` or while `work` runs.

    Timer resolution varies by platform, so compare the export run with the idle
    baseline rather than reading either alone.
    """
    lags: list[float] = []
    done = asyncio.Event()

    async def sleeper() -> None:
        while not done.is_set():
            started = time.perf_counter()
            await asyncio.sleep(0.001)
            lags.append(time.perf_counter() - started - 0.001)

    task = asyncio.create_task(sleeper())
    if work is None:
        await asyncio.sleep(duration or 0)
    else:
        await work
    done.set()
    await task
    lags.sort()
    return {
        "samples": len(lags),
        "p50_ms": round(lags[len(lags) // 2] * 1000, 2),
        "p99_ms": round(lags[int(len(lags) * 0.99)] * 1000, 2),
        "max_ms": round(lags[-1] * 1000, 2),
    }


async def time_export(store: OpportunityStore, filters: HistoryFilters) -> dict[str, Any]:
    """Drain a maximum-size export as the endpoint does, measuring event-loop lag alongside."""
    result: dict[str, Any] = {}

    async def drain() -> None:
        started = time.perf_counter()
        rows = 0
        encoded = 0
        slowest_encode = 0.0
        async for chunk in store.history_export_chunks(filters, None, HISTORY_EXPORT_MAX_ROWS):
            rows += chunk.rows
            encoded += len(chunk.lines)
        # Encoding is the part of each page that runs on the event loop; time it alone.
        rows_for_encode, _ = await store._history_rows(
            filters, None, HISTORY_EXPORT_PAGE_ROWS, budget_seconds=600
        )
        for _ in range(20):
            encode_started = time.perf_counter()
            "".join(_episode_export_line(row[1:]) for row in rows_for_encode).encode()
            slowest_encode = max(slowest_encode, time.perf_counter() - encode_started)
        result.update(
            rows=rows,
            bytes=encoded,
            seconds=round(time.perf_counter() - started, 3),
            page_rows=HISTORY_EXPORT_PAGE_ROWS,
            slowest_page_encode_ms=round(slowest_encode * 1000, 2),
        )

    result["idle_loop_lag"] = await loop_lag(2.0)
    result["export_loop_lag"] = await loop_lag(None, drain())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=43)
    parser.add_argument("--database", type=Path, help="reuse or create this file")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as scratch:
        path = args.database or Path(scratch) / "history.sqlite3"
        seeded_seconds = None
        if not path.exists():
            started = time.perf_counter()
            seed(path, args.rows, args.days, args.seed)
            seeded_seconds = round(time.perf_counter() - started, 1)
        with closing(sqlite3.connect(path)) as db:
            # Production databases never run ANALYZE, so plans must hold without
            # statistics. Indexes are (re)created as `initialize` would, without
            # its orphan marking, which would close the seeded open episodes.
            db.execute("DROP TABLE IF EXISTS sqlite_stat1")
            started = time.perf_counter()
            db.executescript(CREATE_INDEX_SQL)
            index_seconds = round(time.perf_counter() - started, 2)
            stored, first_ns, last_ns = db.execute(
                "SELECT COUNT(*), MIN(start_ns), MAX(start_ns) FROM opportunity_episodes"
            ).fetchone()
        hour = 3_600_000_000_000
        shapes: dict[str, HistoryFilters] = {
            "unfiltered": HistoryFilters(),
            "last_hour": HistoryFilters(from_ns=last_ns - hour, to_ns=last_ns + 1),
            "one_day_mid_history": HistoryFilters(
                from_ns=(first_ns + last_ns) // 2, to_ns=(first_ns + last_ns) // 2 + 24 * hour
            ),
            "pair": HistoryFilters(pair="SOL-USD"),
            "pair_one_day": HistoryFilters(
                pair="SOL-USD",
                from_ns=(first_ns + last_ns) // 2,
                to_ns=(first_ns + last_ns) // 2 + 24 * hour,
            ),
            "dense_route": HistoryFilters(buy_exchange="coinbase", sell_exchange="gemini"),
            "sparse_route": HistoryFilters(buy_exchange="gemini", sell_exchange="binance"),
            "state_closed": HistoryFilters(state="closed"),
            "state_open": HistoryFilters(state="open"),
            "rare_close_reason": HistoryFilters(close_reason="shutdown"),
            "no_match_close_reason": HistoryFilters(close_reason="orphaned"),
            "no_match_route_one_day": HistoryFilters(
                buy_exchange="gemini",
                sell_exchange="gemini",
                from_ns=(first_ns + last_ns) // 2,
                to_ns=(first_ns + last_ns) // 2 + 24 * hour,
            ),
        }
        store = OpportunityStore(str(path))

        async def measure() -> dict[str, Any]:
            results: dict[str, Any] = {}
            for name, filters in shapes.items():
                results[name] = {
                    "plan": plan(path, filters, (last_ns, 1)),
                    "first_page": await time_page(store, filters, deep=False),
                    "deep_page": await time_page(store, filters, deep=True),
                }
                print(name, json.dumps(results[name]), flush=True)
            export = await time_export(store, HistoryFilters())
            print("export", json.dumps(export), flush=True)
            return {"shapes": results, "export_unfiltered_max_rows": export}

        measured = asyncio.run(measure())
        report = {
            "tool": "tools/perf_history.py",
            "rows": stored,
            "days": args.days,
            "database_bytes": path.stat().st_size,
            "seed_seconds": seeded_seconds,
            "index_create_seconds": index_seconds,
            "default_page_budget_seconds": HISTORY_QUERY_BUDGET_SECONDS,
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            **measured,
        }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes((json.dumps(report, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
