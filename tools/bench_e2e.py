from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from decimal import Decimal
from typing import Any

import websockets
from arb.adapters.base import parse_levels
from arb.detector import ArbitrageDetector
from arb.orderbook import OrderBookManager
from arb.types import EventKind, MarketEvent
from bench_utils import save_result

HOST = "127.0.0.1"


def build_message(
    *,
    exchange: str,
    pair: str,
    kind: str,
    sequence: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
) -> str:
    return json.dumps(
        {
            "exchange": exchange,
            "pair": pair,
            "kind": kind,
            "sequence": sequence,
            "bids": bids,
            "asks": asks,
        }
    )


def parse_event(message: str, timestamp_ns: int) -> MarketEvent:
    payload = json.loads(message)
    return MarketEvent(
        exchange=payload["exchange"],
        pair=payload["pair"],
        kind=EventKind(payload["kind"]),
        sequence=int(payload["sequence"]),
        timestamp_ns=timestamp_ns,
        bids=parse_levels(payload["bids"]),
        asks=parse_levels(payload["asks"]),
    )


async def synthetic_feed(websocket: websockets.ServerConnection, iterations: int) -> None:
    pair = "BTC-USD"
    bootstrap = [
        build_message(
            exchange="gemini",
            pair=pair,
            kind="snapshot",
            sequence=1,
            bids=[("100.0", "3.0")],
            asks=[("100.4", "2.0")],
        ),
        build_message(
            exchange="coinbase",
            pair=pair,
            kind="snapshot",
            sequence=1,
            bids=[("100.8", "2.5")],
            asks=[("101.0", "3.0")],
        ),
        build_message(
            exchange="binance",
            pair=pair,
            kind="snapshot",
            sequence=1,
            bids=[("100.6", "2.0")],
            asks=[("100.9", "2.0")],
        ),
    ]
    for message in bootstrap:
        await websocket.send(message)

    for sequence in range(2, iterations + 2):
        bid = Decimal("100.8") + (Decimal(sequence % 7) * Decimal("0.01"))
        ask = bid + Decimal("0.15")
        await websocket.send(
            build_message(
                exchange="coinbase",
                pair=pair,
                kind="delta",
                sequence=sequence,
                bids=[(str(bid), "2.5")],
                asks=[(str(ask), "3.0")],
            )
        )


def summarize_latencies(
    latencies_ns: list[int], iterations: int, elapsed_seconds: float, episode_events: int = 0
) -> dict[str, Any]:
    if len(latencies_ns) < 2:
        raise ValueError("Need at least two samples to compute percentile stats.")
    quantiles = statistics.quantiles(latencies_ns, n=100)
    return {
        "iterations": iterations,
        "detections_timed": len(latencies_ns),
        "episode_events": episode_events,
        "throughput_per_minute": int((iterations / elapsed_seconds) * 60),
        "p50_latency_us": round(statistics.median(latencies_ns) / 1_000, 2),
        "p95_latency_us": round(quantiles[94] / 1_000, 2),
        "p99_latency_us": round(quantiles[98] / 1_000, 2),
        "max_latency_us": round(max(latencies_ns) / 1_000, 2),
    }


async def run_benchmark(iterations: int) -> dict[str, Any]:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    manager = OrderBookManager()
    latencies_ns: list[int] = []
    episode_events = 0

    async with websockets.serve(
        lambda websocket: synthetic_feed(websocket, iterations), HOST, 0
    ) as server:
        port = server.sockets[0].getsockname()[1]
        started = time.perf_counter()
        async with websockets.connect(f"ws://{HOST}:{port}") as websocket:
            processed = 0
            while processed < iterations + 3:
                message = await websocket.recv()
                recv_ns = time.perf_counter_ns()
                event = parse_event(message, recv_ns)
                result = manager.apply(event)
                if result.accepted and result.top_of_book is not None:
                    pair_books = [
                        top
                        for exchange in ("gemini", "coinbase", "binance")
                        if (top := manager.top_of_book(exchange, event.pair)) is not None
                    ]
                    episodes = detector.detect_for_pair(event.pair, pair_books, recv_ns)
                    episode_events += len(episodes)
                    # The detector does the same work whether or not a spread
                    # transitions, and episodes only emit on transitions, so
                    # every delta that reached detection is a latency sample.
                    if len(pair_books) >= 2 and event.kind is EventKind.DELTA:
                        yield_ns = time.perf_counter_ns()
                        latencies_ns.append(yield_ns - recv_ns)
                processed += 1
        elapsed = time.perf_counter() - started

    return summarize_latencies(latencies_ns, iterations, elapsed, episode_events)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark end-to-end detection latency under synthetic websocket load."
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10_000,
        help="Number of synthetic delta events to benchmark.",
    )
    args = parser.parse_args()

    result = asyncio.run(run_benchmark(args.iterations))
    save_result("e2e_benchmark", result)

    print(f"iterations={result['iterations']}")
    print(f"detections_timed={result['detections_timed']}")
    print(f"episode_events={result['episode_events']}")
    print(f"throughput_per_minute={result['throughput_per_minute']}")
    print(f"p50_latency_us={result['p50_latency_us']:.2f}")
    print(f"p95_latency_us={result['p95_latency_us']:.2f}")
    print(f"p99_latency_us={result['p99_latency_us']:.2f}")
    print(f"max_latency_us={result['max_latency_us']:.2f}")


if __name__ == "__main__":
    main()
