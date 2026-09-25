"""Audit Binance.US REST snapshots against independently replayed depth books.

Only a book stopped at the snapshot's exact lastUpdateId is compared. The
historical captures contain no Binance.US trade subscription, so trade
adjudication needs a separate short live probe.

    python tools/binance_snapshot_audit.py var/capture-arb040-2026-09-20_165459.jsonl.gz
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb.adapters.base import parse_levels
from arb.adapters.binance import normalize_binance_symbol
from arb.capture import CaptureFrame, read_capture
from arb.orderbook import OrderBookManager
from arb.replay import replay_frames
from arb.types import PriceLevel

DEPTH = 20
Levels = tuple[list[PriceLevel], list[PriceLevel]]
BookKey = tuple[str, int, int]


def compare(book: Levels, reference: Levels) -> Counter[str]:
    result: Counter[str] = Counter()
    for side, live, rest in (("bids", book[0], reference[0]), ("asks", book[1], reference[1])):
        live_prices = {level.price: level.size for level in live}
        rest_prices = {level.price: level.size for level in rest}
        for rank, level in enumerate(rest):
            size = live_prices.get(level.price)
            if size is None:
                result[f"{side}_rest_only"] += 1
                if rank == 0:
                    result[f"{side}_rest_only_best"] += 1
            elif size != level.size:
                result[f"{side}_size_differs"] += 1
        for rank, level in enumerate(live):
            if level.price not in rest_prices:
                result[f"{side}_stream_only"] += 1
                if rank == 0:
                    result[f"{side}_stream_only_best"] += 1
    if not result:
        result["exact_match"] = 1
    else:
        result["mismatch"] = 1
    return result


async def analyze(path: Path) -> dict[str, Any]:
    header, frames = read_capture(path)
    snapshots: list[CaptureFrame] = []
    wanted: set[BookKey] = set()
    wanted_prices: set[tuple[str, int, str, Decimal]] = set()
    updates: dict[tuple[str, int], BookKey] = {}
    actions: dict[tuple[str, int, str, Decimal], list[tuple[int, bool]]] = defaultdict(list)
    generation = 0
    trade_frames = 0
    for frame in frames:
        if frame.exchange == "binance" and frame.kind == "snapshot":
            provenance = frame.snapshot_provenance
            if (
                provenance is not None
                and provenance.purpose == "reconciliation"
                and frame.payload is not None
                and "lastUpdateId" in frame.payload
            ):
                key = (
                    provenance.pair,
                    provenance.connection_generation,
                    int(frame.payload["lastUpdateId"]),
                )
                wanted.add(key)
                snapshots.append(frame)
                for side, field in (("bids", "bids"), ("asks", "asks")):
                    for price, _size in frame.payload[field][:DEPTH]:
                        wanted_prices.add(
                            (
                                provenance.pair,
                                provenance.connection_generation,
                                side,
                                Decimal(str(price)),
                            )
                        )
    for frame in frames:
        if frame.exchange != "binance":
            continue
        if frame.kind == "connection" and frame.connection is not None:
            generation = frame.connection.generation
        if frame.kind != "ws" or frame.raw is None:
            continue
        payload = json.loads(frame.raw)
        if payload.get("e") == "trade":
            trade_frames += 1
        if not all(field in payload for field in ("s", "u", "U", "b", "a")):
            continue
        pair = normalize_binance_symbol(str(payload["s"]))
        update_id = int(payload["u"])
        updates[(pair, frame.mono_ns)] = (pair, generation, update_id)
        for side, field in (("bids", "b"), ("asks", "a")):
            for price, size in payload[field]:
                action_key = (pair, generation, side, Decimal(str(price)))
                if action_key in wanted_prices:
                    actions[action_key].append((update_id, Decimal(str(size)) == 0))
    for history in actions.values():
        history.sort()

    matched: dict[BookKey, Levels] = {}
    last_local_sequence: dict[tuple[str, int], int | None] = {}
    manager = OrderBookManager(max_age_seconds=60)

    def observe(exchange: str, pair: str, _wall_ns: int, mono_ns: int) -> None:
        if exchange != "binance":
            return
        key = updates.get((pair, mono_ns))
        if key is None:
            return
        book = manager._books.get((exchange, pair))
        if book is None or book.sequence is None:
            return
        local_key = (pair, key[1])
        if book.sequence == last_local_sequence.get(local_key):
            return
        last_local_sequence[local_key] = book.sequence
        if key not in wanted or key in matched:
            return
        levels = manager.level_snapshot(exchange, pair, DEPTH)
        if levels is not None:
            matched[key] = levels

    replay = await replay_frames(header, frames, book_manager=manager, book_observer=observe)
    results: dict[str, Counter[str]] = defaultdict(Counter)
    for frame in snapshots:
        provenance = frame.snapshot_provenance
        payload = frame.payload
        assert provenance is not None and payload is not None
        pair = provenance.pair
        label = f"binance:{pair}"
        key = (pair, provenance.connection_generation, int(payload["lastUpdateId"]))
        book = matched.get(key)
        if book is None:
            results[label]["unaligned"] += 1
            continue
        reference: Levels = (
            list(parse_levels(payload["bids"])[:DEPTH]),
            list(parse_levels(payload["asks"])[:DEPTH]),
        )
        result = compare(book, reference)
        results[label]["aligned"] += 1
        results[label].update(result)
        for side, live, rest in (("bids", book[0], reference[0]), ("asks", book[1], reference[1])):
            live_prices = {level.price for level in live}
            for level in rest:
                history = actions.get((pair, key[1], side, level.price), [])
                action_index = bisect.bisect_right(history, (key[2], True)) - 1
                if (
                    level.price not in live_prices
                    and action_index >= 0
                    and history[action_index][1]
                ):
                    results[label][f"{side}_rest_only_previously_deleted"] += 1

    return {
        "capture": str(path),
        "replay_digest": replay.digest,
        "replay_snapshot_failures": replay.snapshots_failed,
        "replay_snapshot_failures_inferred": replay.snapshots_failed_inferred,
        "replay_snapshots_cancelled_in_flight": replay.snapshots_cancelled_in_flight,
        "snapshots": len(snapshots),
        "trade_frames": trade_frames,
        "method": "compare only at identical lastUpdateId; unaligned snapshots are not judged",
        "by_pair": {key: dict(sorted(value.items())) for key, value in sorted(results.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(analyze(args.capture)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
