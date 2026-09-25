"""Compare old and price-set reconciliation evidence on recorded REST checks.

The replay supplies the book immediately before a recorded REST request and
immediately after its response. These are fixed-book counterfactual counts:
changing a historical confirmation would also change later recovery traffic.

    python tools/reconcile_capture.py var/capture-arb040-2026-09-20_165459.jsonl.gz
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb.adapters import ADAPTER_TYPES
from arb.capture import CaptureFrame, read_capture
from arb.orderbook import OrderBookManager
from arb.reconcile import (
    RECONCILE_DEPTH,
    ReconcileDifference,
    SideDifference,
    SnapshotReconciler,
    _TargetState,
)
from arb.replay import replay_frames
from arb.types import PriceLevel

Levels = tuple[list[PriceLevel], list[PriceLevel]]
Key = tuple[str, str]


@dataclass(frozen=True)
class Check:
    frame: CaptureFrame
    request_ns: int
    response_ns: int


def _old_side(live: list[PriceLevel], snapshot: list[PriceLevel]) -> SideDifference:
    price_pct = Decimal("100") if len(live) != len(snapshot) else Decimal("0")
    if len(live) == len(snapshot):
        for left, right in zip(live, snapshot):
            price_pct = max(
                price_pct,
                abs(left.price - right.price) / (abs(right.price) or Decimal("1")) * 100,
            )
    left_size = sum((level.size for level in live), Decimal("0"))
    right_size = sum((level.size for level in snapshot), Decimal("0"))
    return SideDifference(price_pct, (left_size - right_size) / (abs(right_size) or 1) * 100)


def _difference(left: Levels, right: Levels, *, old: bool) -> ReconcileDifference:
    side = _old_side if old else SnapshotReconciler._side_difference
    return ReconcileDifference(side(left[0], right[0]), side(left[1], right[1]))


async def analyze(path: Path) -> dict[str, Any]:
    header, frames = read_capture(path)
    checks: dict[Key, list[Check]] = defaultdict(list)
    for frame in frames:
        provenance = frame.snapshot_provenance
        if (
            frame.kind == "snapshot"
            and provenance is not None
            and provenance.purpose == "reconciliation"
            and frame.payload is not None
            and provenance.request_mono_ns is not None
        ):
            checks[(frame.exchange, provenance.pair)].append(
                Check(frame, provenance.request_mono_ns, provenance.response_mono_ns)
            )

    query_times: dict[Key, list[int]] = {
        key: sorted({time for check in entries for time in (check.request_ns, check.response_ns)})
        for key, entries in checks.items()
    }
    position: dict[Key, int] = defaultdict(int)
    previous: dict[Key, Levels | None] = {}
    sampled: dict[tuple[Key, int], Levels | None] = {}
    manager = OrderBookManager(max_age_seconds=60)

    def observe(exchange: str, pair: str, _wall_ns: int, mono_ns: int) -> None:
        key = (exchange, pair)
        times = query_times.get(key, [])
        cursor = position[key]
        while cursor < len(times) and times[cursor] < mono_ns:
            sampled[(key, times[cursor])] = previous.get(key)
            cursor += 1
        previous[key] = manager.level_snapshot(exchange, pair, RECONCILE_DEPTH)
        while cursor < len(times) and times[cursor] == mono_ns:
            sampled[(key, times[cursor])] = previous[key]
            cursor += 1
        position[key] = cursor

    replay = await replay_frames(header, frames, book_manager=manager, book_observer=observe)
    for key, times in query_times.items():
        for stamp in times[position[key] :]:
            sampled[(key, stamp)] = previous.get(key)

    adapters = {
        cls.name: cls(header.exchanges[cls.name])
        for cls in ADAPTER_TYPES
        if cls.name in header.exchanges
    }
    reconciler = SnapshotReconciler(list(adapters.values()), manager, list(checks))
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    states: dict[tuple[Key, bool], _TargetState] = defaultdict(_TargetState)
    for key, entries in checks.items():
        exchange, pair = key
        for check in sorted(entries, key=lambda item: item.response_ns):
            label = f"{exchange}:{pair}"
            before = sampled.get((key, check.request_ns))
            after = sampled.get((key, check.response_ns))
            if before is None or after is None:
                counts[label]["skipped_no_book"] += 1
                continue
            adapter = adapters[exchange]

            async def fetch(
                _url: str, *, payload: dict[str, Any] = check.frame.payload or {}
            ) -> dict[str, Any]:
                return payload

            adapter.client_get_json = fetch  # type: ignore[assignment]
            snapshot = await adapter.fetch_snapshot(pair, 0)
            reference: Levels = (
                list(snapshot.bids[:RECONCILE_DEPTH]),
                list(snapshot.asks[:RECONCILE_DEPTH]),
            )
            counts[label]["checks"] += 1
            for old, name in ((True, "old"), (False, "new")):
                state = states[(key, old)]
                if check.response_ns / 1e9 < state.cooldown_until:
                    counts[label][f"{name}_cooldown_skips"] += 1
                    continue
                evidence = SnapshotReconciler._corroborate(
                    _difference(before, reference, old=old),
                    _difference(after, reference, old=old),
                )
                if not evidence.mismatched:
                    SnapshotReconciler._reset_mismatches(state)
                    continue
                counts[label][f"{name}_mismatches"] += 1
                counts[label][f"{name}_{evidence.kind}"] += 1
                streak, required = reconciler._record_mismatch(state, evidence)
                if streak >= required:
                    counts[label][f"{name}_confirmations"] += 1
                    state.cooldown_until = check.response_ns / 1e9 + 300
                    SnapshotReconciler._reset_mismatches(state)

    return {
        "capture": str(path),
        "replay_digest": replay.digest,
        "snapshots": sum(len(entries) for entries in checks.values()),
        "method": "fixed-book counterfactual; 3 price/5 size confirmations; 300s cooldown",
        "by_venue_pair": {
            key: dict(sorted(value.items())) for key, value in sorted(counts.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(analyze(args.capture)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
