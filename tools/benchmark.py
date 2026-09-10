from __future__ import annotations

import statistics
import time
from decimal import Decimal

from arb.detector import ArbitrageDetector
from arb.types import TopOfBook
from bench_utils import save_result


def make_book(exchange: str, bid: str, ask: str) -> TopOfBook:
    return TopOfBook(
        exchange=exchange,
        pair="BTC-USD",
        best_bid_price=Decimal(bid),
        best_bid_size=Decimal("1"),
        best_ask_price=Decimal(ask),
        best_ask_size=Decimal("1"),
        sequence=1,
        timestamp_ns=time.time_ns(),
    )


def main(iterations: int = 100_000) -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    books = [
        make_book("gemini", "101", "101.2"),
        make_book("coinbase", "101.5", "101.7"),
        make_book("binance", "101.8", "102"),
    ]

    latencies_us: list[float] = []
    started = time.perf_counter()
    for _ in range(iterations):
        begin = time.perf_counter()
        detector.detect_for_pair("BTC-USD", books, time.time_ns())
        latencies_us.append((time.perf_counter() - begin) * 1_000_000)
    elapsed = time.perf_counter() - started

    result = {
        "iterations": iterations,
        "throughput_per_minute": int((iterations / elapsed) * 60),
        "p50_latency_us": round(statistics.median(latencies_us), 2),
        "p95_latency_us": round(statistics.quantiles(latencies_us, n=20)[18], 2),
    }
    save_result("detector_microbenchmark", result)

    print(f"iterations={result['iterations']}")
    print(f"throughput_per_minute={result['throughput_per_minute']}")
    print(f"p50_latency_us={result['p50_latency_us']:.2f}")
    print(f"p95_latency_us={result['p95_latency_us']:.2f}")


if __name__ == "__main__":
    main()
