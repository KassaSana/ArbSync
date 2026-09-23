"""Audit Gemini's incremental books against Gemini's top-20 snapshots at the same update id.

Needs a capture recorded by `tools/gemini_audit_capture.py`. Gemini's
`{symbol}@depth20` snapshots carry a `lastUpdateId` in the `@depth` id space,
so each one is compared with the incremental book only when the book has
applied exactly that update id: no timing ambiguity and no REST. The report
covers:

- `comparisons`: aligned (compared), unaligned (the book skipped that id), and
  how many aligned comparisons matched Gemini's top 20 exactly.
- `discrepancies`: disagreeing levels by kind (`missing`, `ghost`, `size`)
  and rank bucket (`best`, `1-4`, `5-19`).
- `runs`: how long each disagreeing level persisted across consecutive
  aligned comparisons of its pair (a run of one comparison healed by the next).
- `trades`: whether each trade's price rested in the incremental book and in
  the latest aligned snapshot, so real prints adjudicate a disagreement.
- `episodes` (with `--config`): replayed episodes with a Gemini leg whose
  Gemini price disagreed with the latest aligned snapshot, and how many would
  not have crossed the detection threshold at Gemini's true best price.

    uv run python tools/gemini_book_audit.py var/capture.jsonl.gz --config var/arb047.toml
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb.adapters.gemini import GeminiAdapter, normalize_gemini_symbol
from arb.capture import CaptureFrame, read_capture
from arb.types import EventKind, MarketEvent

SECOND_NS = 1_000_000_000
SIDES = ("b", "a")
TRADE_WINDOW_NS = 2 * SECOND_NS

Side = dict[Decimal, Decimal]


def _rank_bucket(rank: int) -> str:
    if rank == 0:
        return "best"
    return "1-4" if rank < 5 else "5-19"


def _top(side: Side, count: int, *, descending: bool) -> list[Decimal]:
    return sorted(side, reverse=descending)[:count]


@dataclass
class Comparison:
    """One aligned comparison's outcome for one pair."""

    wall_ns: int
    true_best: dict[str, Decimal | None]
    book_best: dict[str, Decimal | None]
    keys: set[tuple[str, Decimal, str]]


@dataclass
class _Run:
    first_ns: int
    last_ns: int
    comparisons: int
    rank: int


@dataclass
class AuditReport:
    comparisons: Counter[str] = field(default_factory=Counter)
    comparisons_by_pair: dict[str, Counter[str]] = field(default_factory=dict)
    discrepancies: Counter[str] = field(default_factory=Counter)
    run_comparisons: list[int] = field(default_factory=list)
    run_seconds: list[float] = field(default_factory=list)
    run_buckets: Counter[str] = field(default_factory=Counter)
    trades: Counter[str] = field(default_factory=Counter)
    history: dict[str, list[Comparison]] = field(default_factory=dict)
    episodes: Counter[str] = field(default_factory=Counter)

    def as_payload(self) -> dict[str, Any]:
        runs = len(self.run_seconds)
        return {
            "comparisons": dict(sorted(self.comparisons.items())),
            "comparisons_by_pair": {
                pair: dict(sorted(counts.items()))
                for pair, counts in sorted(self.comparisons_by_pair.items())
            },
            "discrepancies": dict(sorted(self.discrepancies.items())),
            "runs": {
                "count": runs,
                "healed_by_next_comparison": sum(1 for n in self.run_comparisons if n == 1),
                "median_seconds": statistics.median(self.run_seconds) if runs else None,
                "over_5s": sum(1 for s in self.run_seconds if s > 5),
                "over_30s": sum(1 for s in self.run_seconds if s > 30),
                "max_seconds": max(self.run_seconds) if runs else None,
                "by_bucket": dict(sorted(self.run_buckets.items())),
            },
            "trades": dict(sorted(self.trades.items())),
            "episodes": dict(sorted(self.episodes.items())),
        }


def _compare(
    book: dict[str, Side], partial: dict[str, list[tuple[Decimal, Decimal]]]
) -> tuple[set[tuple[str, Decimal, str]], dict[tuple[str, Decimal, str], int]]:
    """Disagreeing (side, price, kind) keys and their ranks within Gemini's top N."""
    keys: dict[tuple[str, Decimal, str], int] = {}
    for side in SIDES:
        descending = side == "b"
        levels = partial[side]
        if not levels:
            continue
        ours = book[side]
        for rank, (price, size) in enumerate(levels):
            if price not in ours:
                keys[(side, price, "missing")] = rank
            elif ours[price] != size:
                keys[(side, price, "size")] = rank
        # Only the price range Gemini's snapshot covers can be checked for ghosts.
        worst = levels[-1][0]
        true_prices = {price for price, _ in levels}
        for rank, price in enumerate(_top(ours, len(levels), descending=descending)):
            inside = price >= worst if descending else price <= worst
            if inside and price not in true_prices:
                keys[(side, price, "ghost")] = rank
    return set(keys), keys


async def audit(frames: list[CaptureFrame], pairs: list[str]) -> AuditReport:
    adapter = GeminiAdapter(pairs)
    report = AuditReport()
    books: dict[str, dict[str, Side]] = {}
    last_id: dict[str, int] = {}
    pending: dict[str, tuple[int, dict[str, list[tuple[Decimal, Decimal]]]]] = {}
    runs: dict[str, dict[tuple[str, Decimal, str], _Run]] = {}
    # When the depth stream mentioned each (pair, side, price), and the trades
    # that printed better than the book's best, to classify after the pass.
    mentions: dict[tuple[str, str, Decimal], list[int]] = {}
    beyond: list[tuple[str, str, Decimal, int]] = []

    def close_runs(pair: str, keep: set[tuple[str, Decimal, str]]) -> None:
        active = runs.setdefault(pair, {})
        for key in [key for key in active if key not in keep]:
            run = active.pop(key)
            report.run_comparisons.append(run.comparisons)
            report.run_seconds.append((run.last_ns - run.first_ns) / SECOND_NS)
            report.run_buckets[f"{_rank_bucket(run.rank)}:{run.comparisons > 1}"] += 1

    def count(pair: str, outcome: str) -> None:
        report.comparisons[outcome] += 1
        report.comparisons_by_pair.setdefault(pair, Counter())[outcome] += 1

    def compare(pair: str, partial: dict[str, list[tuple[Decimal, Decimal]]], wall: int) -> None:
        book = books[pair]
        keys, ranks = _compare(book, partial)
        count(pair, "match" if not keys else "mismatch")
        count(pair, "aligned")
        for (_, _, kind), rank in ranks.items():
            report.discrepancies[f"{kind}:{_rank_bucket(rank)}"] += 1
        active = runs.setdefault(pair, {})
        for key in keys:
            run = active.get(key)
            if run is None:
                active[key] = _Run(wall, wall, 1, ranks[key])
            else:
                run.last_ns, run.comparisons = wall, run.comparisons + 1
                run.rank = min(run.rank, ranks[key])
        close_runs(pair, keys)
        report.history.setdefault(pair, []).append(
            Comparison(
                wall_ns=wall,
                true_best={
                    side: (partial[side][0][0] if partial[side] else None) for side in SIDES
                },
                book_best={
                    side: next(iter(_top(book[side], 1, descending=side == "b")), None)
                    for side in SIDES
                },
                keys=keys,
            )
        )

    def drop_pending(pair: str) -> None:
        if pending.pop(pair, None) is not None:
            count(pair, "unaligned")

    def apply(event: MarketEvent, wall: int) -> None:
        pair = event.pair
        if event.kind is EventKind.RESET:
            books.pop(pair, None)
            last_id.pop(pair, None)
            drop_pending(pair)
            close_runs(pair, set())
            return
        if event.kind is EventKind.SNAPSHOT:
            books[pair] = {side: {} for side in SIDES}
            drop_pending(pair)
            close_runs(pair, set())
        elif event.kind is not EventKind.DELTA:
            return
        book = books.get(pair)
        if book is None:
            return
        for side, levels in (("b", event.bids), ("a", event.asks)):
            for level in levels:
                mentions.setdefault((pair, side, level.price), []).append(wall)
                if level.size > 0:
                    book[side][level.price] = level.size
                else:
                    book[side].pop(level.price, None)
        if event.exchange_last_sequence is not None:
            last_id[pair] = event.exchange_last_sequence
        waiting = pending.get(pair)
        if waiting is not None and last_id.get(pair, -1) >= waiting[0]:
            pending.pop(pair)
            if last_id[pair] == waiting[0]:
                compare(pair, waiting[1], wall)
            else:
                count(pair, "unaligned")

    def trade(payload: dict[str, Any], wall: int) -> None:
        pair = normalize_gemini_symbol(str(payload["s"]))
        book = books.get(pair)
        history = report.history.get(pair)
        if book is None or not history:
            report.trades["no_book"] += 1
            return
        # `m` true: the buyer was the maker, so the trade hit a resting bid.
        side = "b" if payload.get("m") else "a"
        price = Decimal(str(payload["p"]))
        in_book = price in book[side]
        ours = next(iter(_top(book[side], 1, descending=side == "b")), None)
        beyond_book = ours is None or (price > ours if side == "b" else price < ours)
        report.trades[f"book={'in' if in_book else 'out'}"] += 1
        if beyond_book:
            report.trades["better_than_book_best"] += 1
            beyond.append((pair, side, price, wall))

    for frame in frames:
        if frame.exchange != "gemini":
            continue
        if frame.kind == "connection" and frame.connection is not None:
            if frame.connection.connected:
                await adapter.reset_state()
            for pair in list(books):
                books.pop(pair)
                last_id.pop(pair, None)
                drop_pending(pair)
                close_runs(pair, set())
            continue
        if frame.kind != "ws" or frame.raw is None:
            continue
        payload = json.loads(frame.raw)
        if "lastUpdateId" in payload and "symbol" in payload:
            pair = normalize_gemini_symbol(str(payload["symbol"]))
            partial = {
                "b": [(Decimal(p), Decimal(q)) for p, q in payload.get("bids", [])],
                "a": [(Decimal(p), Decimal(q)) for p, q in payload.get("asks", [])],
            }
            target = int(payload["lastUpdateId"])
            drop_pending(pair)
            current = last_id.get(pair)
            if pair not in books or current is None:
                count(pair, "unaligned")
            elif current == target:
                compare(pair, partial, frame.wall_ns)
            elif current < target:
                pending[pair] = (target, partial)
            else:
                count(pair, "unaligned")
            continue
        if "t" in payload and "p" in payload and "s" in payload and "e" not in payload:
            trade(payload, frame.wall_ns)
            continue
        if adapter._reconnect_requested:
            continue
        result = await adapter.ingest_message(frame.raw, frame.mono_ns)
        for event in result.events:
            apply(event, frame.wall_ns)
    for pair in list(runs):
        close_runs(pair, set())
    # A trade better than the book's best either hit an order placed and
    # filled between one-second depth batches (the stream mentions that price
    # around the trade) or liquidity the depth stream never showed at all.
    for pair, side, price, wall in beyond:
        times = mentions.get((pair, side, price), [])
        seen = any(abs(at - wall) <= TRADE_WINDOW_NS for at in times)
        report.trades[f"better_than_book_best:stream_{'mentioned' if seen else 'silent'}"] += 1
    return report


def join_episodes(
    report: AuditReport,
    episodes: list[dict[str, object]],
    threshold_pct: Decimal,
    *,
    window_ns: int = 2 * SECOND_NS,
) -> None:
    """Check each Gemini-leg episode against the latest aligned snapshot before it opened."""
    for episode in episodes:
        buy, sell = str(episode["buy_exchange"]), str(episode["sell_exchange"])
        if "gemini" not in (buy, sell):
            continue
        report.episodes["gemini_leg"] += 1
        history = report.history.get(str(episode["pair"]), [])
        start = int(str(episode["start_ns"]))
        index = bisect.bisect_right([c.wall_ns for c in history], start) - 1
        if index < 0 or start - history[index].wall_ns > window_ns:
            report.episodes["no_aligned_snapshot_within_2s"] += 1
            continue
        comparison = history[index]
        side = "a" if buy == "gemini" else "b"
        true_price = comparison.true_best[side]
        if comparison.book_best[side] == true_price:
            report.episodes["gemini_price_confirmed"] += 1
            continue
        report.episodes["gemini_price_contradicted"] += 1
        if true_price is None:
            continue
        buy_price = true_price if buy == "gemini" else Decimal(str(episode["buy_price"]))
        sell_price = true_price if sell == "gemini" else Decimal(str(episode["sell_price"]))
        spread = (sell_price - buy_price) / buy_price * Decimal(100)
        if spread < threshold_pct:
            report.episodes["phantom_at_true_price"] += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--config", type=Path, help="replay episodes with this configuration")
    parser.add_argument("--allow-lossy", action="store_true")
    args = parser.parse_args()
    header, frames = read_capture(args.capture, allow_lossy=args.allow_lossy)
    pairs = header.exchanges.get("gemini", [])
    if not pairs:
        parser.error("capture has no gemini pairs")
    report = asyncio.run(audit(frames, pairs))
    if args.config is not None:
        from arb.config import load_config
        from research import _canonical_episode_rows, replay_for_research

        config = load_config(args.config)
        replay, _, _, _ = asyncio.run(
            replay_for_research(header, frames, config, net_intervals=False)
        )
        join_episodes(
            report,
            _canonical_episode_rows(replay),
            Decimal(str(config.detector.threshold_pct)),
        )
    print(json.dumps(report.as_payload(), indent=2, default=str))


if __name__ == "__main__":
    main()
