"""Measure what offline net-interval extraction costs on real and synthetic books.

Replays one capture twice, without and with the `NetSignalRecorder` book
observer, and reports the wall-time delta, per-call observer timing, resident
memory growth during the replay, and signal row count. Two comparison figures accompany it: the
periodic `DepthSampler.sample_all` walk at the configured cadence (the
sampled-interpolation baseline a live implementation could fall back to) and
a synthetic worst case with 300-level books on both legs plus a thin third
venue. Research-only; it never touches SQLite or live ingestion.

```powershell
uv run python tools/perf_net_intervals.py `
  --capture server/tests/fixtures/captured/three_venue_150s_20260917.jsonl.gz `
  --output artifacts/research/arb-041/perf_fixture.json
```
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psutil
from arb.capture import read_capture
from arb.config import load_config
from arb.detector import ArbitrageDetector
from arb.orderbook import OrderBookManager
from arb.pricing import DepthSampler
from arb.replay import replay_frames
from arb.types import EventKind, MarketEvent, PriceLevel
from net_intervals import DEPTH_WINDOW_LEVELS, NetSignalRecorder
from research import _adapter_depths


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile, the same rule the research datasets use."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(fraction * len(ordered)) + 1))
    return ordered[rank - 1]


def _resident_bytes() -> int:
    return int(psutil.Process().memory_info().rss)


@dataclass(frozen=True)
class _ReplayCost:
    wall_seconds: float
    digest: str
    transitions: int
    observer_calls: list[float]
    signal_rows: int
    resident_growth_bytes: int
    sample_all_seconds: list[float]
    sample_interval_seconds: float
    notionals: list[str]
    capture_seconds: float


async def _replay(capture: Path, config_path: Path, *, with_observer: bool) -> _ReplayCost:
    config = load_config(config_path)
    header, frames = read_capture(capture)
    book_manager = OrderBookManager(max_age_seconds=config.order_books.max_age_seconds)
    exchanges = set(header.exchanges)
    fees = {
        exchange: fee for exchange, fee in config.fees.taker_pct.items() if exchange in exchanges
    }
    sampler = DepthSampler(
        book_manager,
        config.pricing.notionals,
        _adapter_depths(header),
        fees,
        interval_seconds=config.pricing.sample_interval_seconds,
    )
    detector = ArbitrageDetector(
        threshold_pct=Decimal(str(config.detector.threshold_pct)),
        ledger_factory=sampler.ledgers_for_route,
    )
    observer_calls: list[float] = []
    recorder: NetSignalRecorder | None = None
    observer = None
    if with_observer:
        recorder = NetSignalRecorder(book_manager, sampler)
        observe = recorder.observe

        def timed(exchange: str, pair: str, wall_ns: int, mono_ns: int) -> None:
            started = time.perf_counter()
            observe(exchange, pair, wall_ns, mono_ns)
            observer_calls.append(time.perf_counter() - started)

        observer = timed
    resident_before = _resident_bytes()
    started_wall = time.perf_counter()
    # Same GC discipline as `research.replay_for_research`, so observer timings
    # measure the walk rather than cyclic-GC rescans of the accumulated rows.
    gc.disable()
    try:
        report = await replay_frames(
            header,
            frames,
            threshold_pct=Decimal(str(config.detector.threshold_pct)),
            max_age_seconds=config.order_books.max_age_seconds,
            book_manager=book_manager,
            detector=detector,
            depth_sampler=sampler,
            book_observer=observer,
        )
    finally:
        gc.enable()
    wall_seconds = time.perf_counter() - started_wall
    resident_growth = _resident_bytes() - resident_before
    # The periodic sampler walk over the final books, evaluated at the last
    # recorded instant so the books are still eligible: the cost a live
    # implementation would pay per cadence tick instead of per event.
    sample_all_seconds: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        sampler.sample_all(frames[-1].mono_ns)
        sample_all_seconds.append(time.perf_counter() - started)
    capture_seconds = (
        (frames[-1].mono_ns - frames[0].mono_ns) / 1_000_000_000 if len(frames) > 1 else 0.0
    )
    return _ReplayCost(
        wall_seconds=wall_seconds,
        digest=report.digest,
        transitions=len(report.transitions),
        observer_calls=observer_calls,
        signal_rows=recorder.signal_rows if recorder is not None else 0,
        resident_growth_bytes=resident_growth,
        sample_all_seconds=sample_all_seconds,
        sample_interval_seconds=config.pricing.sample_interval_seconds,
        notionals=[str(value) for value in config.pricing.notionals],
        capture_seconds=capture_seconds,
    )


def _levels(start: int, step: int, count: int, size: str) -> tuple[PriceLevel, ...]:
    return tuple(PriceLevel(Decimal(start + step * index), Decimal(size)) for index in range(count))


def _synthetic_worst_case(levels: int = 300, ticks: int = 200) -> dict[str, object]:
    """Per-observe cost with deep books on both legs and a thin third venue.

    Venues `a` and `b` carry `levels` levels per side at 0.1 base each, so the
    largest configured notional walks well past `DEPTH_WINDOW_LEVELS` and
    escalates to the full book on every event; venue `c` has five levels, so
    routes through it stop short. Each tick touches the top of `a`'s ask side
    and re-prices the four routes that include `a`.
    """
    manager = OrderBookManager(max_age_seconds=60.0)
    notionals = (Decimal("100"), Decimal("1000"), Decimal("10000"), Decimal("50000"))
    fees = {"a": Decimal("0.6"), "b": Decimal("0.6"), "c": Decimal("0.4")}
    sampler = DepthSampler(
        manager, notionals, {"a": None, "b": None, "c": None}, fees, interval_seconds=5.0
    )
    books = {
        "a": (_levels(999, -1, levels, "0.1"), _levels(1000, 1, levels, "0.1")),
        "b": (_levels(1001, -1, levels, "0.1"), _levels(1002, 1, levels, "0.1")),
        "c": (_levels(1001, -1, 5, "0.1"), _levels(1003, 1, 5, "0.1")),
    }
    for exchange, (bids, asks) in books.items():
        manager.apply(
            MarketEvent(exchange, "BTC-USD", EventKind.SNAPSHOT, 1, 1, bids, asks),
            received_monotonic_ns=1,
        )
    recorder = NetSignalRecorder(manager, sampler)
    timings: list[float] = []
    for tick in range(ticks):
        sequence = 2 + tick
        manager.apply(
            MarketEvent(
                "a",
                "BTC-USD",
                EventKind.DELTA,
                sequence,
                sequence,
                (),
                (PriceLevel(Decimal(1000), Decimal("0.1") + Decimal(tick % 3) / 100),),
            ),
            received_monotonic_ns=sequence,
        )
        started = time.perf_counter()
        recorder.observe("a", "BTC-USD", sequence, sequence)
        timings.append(time.perf_counter() - started)
    return {
        "levels_per_side": levels,
        "depth_window_levels": DEPTH_WINDOW_LEVELS,
        "notionals": [str(value) for value in notionals],
        "routes_per_observe": 4,
        "observe_calls": len(timings),
        "observe_mean_seconds": statistics.fmean(timings),
        "observe_p90_seconds": _percentile(timings, 0.9),
        "signal_rows": recorder.signal_rows,
    }


def measure(capture: Path, config_path: Path) -> dict[str, object]:
    without = asyncio.run(_replay(capture, config_path, with_observer=False))
    with_observer = asyncio.run(_replay(capture, config_path, with_observer=True))
    if without.digest != with_observer.digest:
        raise RuntimeError("the book observer changed the replay digest")
    calls = with_observer.observer_calls
    observer_seconds = sum(calls)
    sample_mean = statistics.fmean(with_observer.sample_all_seconds)
    return {
        "capture": str(capture),
        "digest": with_observer.digest,
        "capture_seconds": with_observer.capture_seconds,
        "transitions": with_observer.transitions,
        "notionals": with_observer.notionals,
        "without_observer": {
            "wall_seconds": without.wall_seconds,
            "resident_growth_bytes": without.resident_growth_bytes,
        },
        "with_observer": {
            "wall_seconds": with_observer.wall_seconds,
            "resident_growth_bytes": with_observer.resident_growth_bytes,
            "signal_rows": with_observer.signal_rows,
        },
        "observer": {
            "calls": len(calls),
            "total_seconds": observer_seconds,
            "mean_seconds": statistics.fmean(calls) if calls else 0.0,
            "p90_seconds": _percentile(calls, 0.9),
            "p99_seconds": _percentile(calls, 0.99),
            "max_seconds": max(calls, default=0.0),
            "wall_delta_seconds": with_observer.wall_seconds - without.wall_seconds,
            "seconds_per_capture_second": (
                observer_seconds / with_observer.capture_seconds
                if with_observer.capture_seconds
                else None
            ),
            "resident_growth_delta_bytes": (
                with_observer.resident_growth_bytes - without.resident_growth_bytes
            ),
        },
        "sampled_baseline": {
            "sample_interval_seconds": with_observer.sample_interval_seconds,
            "sample_all_mean_seconds": sample_mean,
            "sample_all_p90_seconds": _percentile(with_observer.sample_all_seconds, 0.9),
            "seconds_per_capture_second": sample_mean / with_observer.sample_interval_seconds,
        },
        "synthetic_deep_book": _synthetic_worst_case(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", type=Path, required=True, help="capture JSONL or JSONL.gz")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report here")
    return parser


def main() -> None:
    args = _parser().parse_args()
    document = measure(args.capture, args.config)
    text = json.dumps(document, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
