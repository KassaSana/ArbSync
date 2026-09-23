"""Classify Gemini book drift in a capture against Gemini's own fresh snapshots.

Each time a Gemini book is rebuilt (a reconnect or a single-pair resubscription),
the first depth frame of the new subscription is a full snapshot taken by the
exchange. Comparing the incrementally maintained book just before the rebuild
with that snapshot needs no REST request. A disagreement counts as ``stale``
only when the stream last mentioned that price more than ``--stale-seconds``
earlier. A price the stream never mentioned is reported as ``unannounced``,
not stale: an order placed during the last one-second depth batch before the
rebuild looks exactly like that (ARB-047 corrected ARB-046 on this point).
For an exact comparison, use ``tools/gemini_book_audit.py`` on a capture with
Gemini's ``@depth20`` stream.

The report also checks the two normalization assumptions the adapter relies
on: the ``U``/``u`` chain per symbol, and no frame repeating a price on a side.

    uv run python tools/gemini_drift.py var/capture.jsonl.gz
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb.adapters.gemini import GeminiAdapter
from arb.capture import CaptureFrame, read_capture
from arb.types import EventKind, MarketEvent

SECOND_NS = 1_000_000_000

Book = dict[str, dict[Decimal, Decimal]]


@dataclass
class _Chain:
    last_id: int | None = None


@dataclass
class DriftReport:
    frames: int = 0
    rebuilds: Counter[str] = field(default_factory=Counter)
    chain: Counter[str] = field(default_factory=Counter)
    repeated_price_frames: int = 0
    levels: Counter[str] = field(default_factory=Counter)
    levels_by_pair: dict[str, Counter[str]] = field(default_factory=dict)
    levels_by_rebuild: dict[str, Counter[str]] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "gemini_depth_frames": self.frames,
            "rebuilds_compared": dict(sorted(self.rebuilds.items())),
            "update_id_chain": dict(sorted(self.chain.items())),
            "frames_repeating_a_price": self.repeated_price_frames,
            "levels": dict(sorted(self.levels.items())),
            "levels_by_pair": {
                pair: dict(sorted(counts.items()))
                for pair, counts in sorted(self.levels_by_pair.items())
            },
            "levels_by_rebuild": {
                cause: dict(sorted(counts.items()))
                for cause, counts in sorted(self.levels_by_rebuild.items())
            },
        }


def _check_chain(chain: _Chain, first_id: int, last_id: int) -> str:
    previous = chain.last_id
    chain.last_id = last_id if previous is None else max(previous, last_id)
    if previous is None:
        return "first"
    if last_id <= previous:
        return "already_covered"
    if first_id in (previous, previous + 1):
        return "contiguous"
    if first_id < previous:
        return "overlapping"
    return "gap"


def _classify(
    old: dict[Decimal, Decimal],
    fresh: dict[Decimal, Decimal],
    *,
    touched_ns: dict[Decimal, int],
    now_ns: int,
    stale_ns: int,
    depth: int,
    descending: bool,
) -> Counter[str]:
    """Compare two sides within the price range of the old book's top ``depth`` levels."""
    counts: Counter[str] = Counter()
    top = sorted(old, reverse=descending)[:depth]
    if not top:
        return counts
    low, high = min(top), max(top)
    for price in set(old) | set(fresh):
        if not low <= price <= high:
            continue
        last = touched_ns.get(price)
        stale = last is not None and now_ns - last > stale_ns
        if price in old and price in fresh:
            kind = "agree" if old[price] == fresh[price] else "size_differs"
        elif price in fresh:
            kind = "missing_from_incremental"
            if last is None:
                counts[f"{kind}_unannounced"] += 1
                continue
        else:
            kind = "ghost_in_incremental"
        counts[f"{kind}_stale" if stale and kind != "agree" else kind] += 1
    return counts


async def analyze(
    frames: list[CaptureFrame],
    pairs: list[str],
    *,
    stale_seconds: float = 30.0,
    depth: int = 50,
) -> DriftReport:
    adapter = GeminiAdapter(pairs)
    report = DriftReport()
    stale_ns = int(stale_seconds * SECOND_NS)
    books: dict[str, Book] = {}
    # Books awaiting their replacement snapshot, with what dropped them:
    # "reconnect" (seconds of downtime, so the comparison also sees market
    # changes made while disconnected) or "pair_resync" (a sub-second gap).
    replaced: dict[str, tuple[Book, str]] = {}
    touched: dict[tuple[str, str], dict[Decimal, int]] = {}
    chains: dict[str, _Chain] = {}

    def apply(event: MarketEvent, wall_ns: int) -> None:
        pair = event.pair
        if event.kind is EventKind.RESET:
            if pair in books:
                replaced[pair] = (books.pop(pair), "pair_resync")
            return
        if event.kind not in (EventKind.SNAPSHOT, EventKind.DELTA):
            return
        if event.kind is EventKind.SNAPSHOT:
            previous = replaced.pop(pair, None)
            fresh: Book = {
                "b": {level.price: level.size for level in event.bids},
                "a": {level.price: level.size for level in event.asks},
            }
            if previous is not None:
                old, cause = previous
                report.rebuilds[cause] += 1
                pair_counts = report.levels_by_pair.setdefault(pair, Counter())
                cause_counts = report.levels_by_rebuild.setdefault(cause, Counter())
                for side in ("b", "a"):
                    counts = _classify(
                        old[side],
                        fresh[side],
                        touched_ns=touched.get((pair, side), {}),
                        now_ns=wall_ns,
                        stale_ns=stale_ns,
                        depth=depth,
                        descending=side == "b",
                    )
                    report.levels.update(counts)
                    pair_counts.update(counts)
                    cause_counts.update(counts)
            books[pair] = fresh
        else:
            book = books.get(pair)
            if book is None:
                return
            for side, levels in (("b", event.bids), ("a", event.asks)):
                for level in levels:
                    if level.size > 0:
                        book[side][level.price] = level.size
                    else:
                        book[side].pop(level.price, None)
        for side, levels in (("b", event.bids), ("a", event.asks)):
            side_touched = touched.setdefault((pair, side), {})
            for level in levels:
                side_touched[level.price] = wall_ns

    for frame in frames:
        if frame.exchange != "gemini":
            continue
        if frame.kind == "connection" and frame.connection is not None:
            if frame.connection.connected:
                await adapter.reset_state()
                chains.clear()
            else:
                replaced.update((pair, (book, "reconnect")) for pair, book in books.items())
                books.clear()
            continue
        if frame.kind != "ws" or frame.raw is None:
            continue
        payload = json.loads(frame.raw)
        if payload.get("e") == "depthUpdate":
            report.frames += 1
            symbol = str(payload.get("s", ""))
            if "U" in payload and "u" in payload:
                chain = chains.setdefault(symbol, _Chain())
                report.chain[_check_chain(chain, int(payload["U"]), int(payload["u"]))] += 1
            for side in ("b", "a"):
                prices = [Decimal(row[0]) for row in payload.get(side, [])]
                if len(prices) != len(set(prices)):
                    report.repeated_price_frames += 1
                    break
        elif isinstance(payload.get("id"), str) and str(payload["id"]).startswith("resync:"):
            # A new subscription restarts the symbol's update-id chain.
            chains.pop(str(payload["id"]).split(":")[1], None)
        if adapter._reconnect_requested:
            # Live, the socket is being torn down; its remaining frames are
            # never applied and the next connection frame resets the adapter.
            continue
        result = await adapter.ingest_message(frame.raw, frame.mono_ns)
        for event in result.events:
            apply(event, frame.wall_ns)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--stale-seconds", type=float, default=30.0)
    parser.add_argument("--depth", type=int, default=50)
    parser.add_argument("--allow-lossy", action="store_true")
    args = parser.parse_args()
    header, frames = read_capture(args.capture, allow_lossy=args.allow_lossy)
    pairs = header.exchanges.get("gemini", [])
    if not pairs:
        parser.error("capture has no gemini pairs")
    report = asyncio.run(analyze(frames, pairs, stale_seconds=args.stale_seconds, depth=args.depth))
    print(json.dumps(report.as_payload(), indent=2))


if __name__ == "__main__":
    main()
