from __future__ import annotations

from decimal import Decimal

from arb.detector import ArbitrageDetector
from arb.metrics import observe_route_open, route_metrics, route_open_age_skew_seconds
from arb.types import RouteAgeEvent, RouteLegAges, TopOfBook


def _top(exchange: str, bid: str, ask: str, received_monotonic_ns: int | None) -> TopOfBook:
    return TopOfBook(
        exchange=exchange,
        pair="LTC-USD",
        best_bid_price=Decimal(bid),
        best_bid_size=Decimal("1"),
        best_ask_price=Decimal(ask),
        best_ask_size=Decimal("1"),
        sequence=1,
        timestamp_ns=1,
        received_monotonic_ns=received_monotonic_ns,
    )


def _sample_count(histogram) -> float:  # type: ignore[no-untyped-def]
    return float(
        next(
            sample.value
            for sample in histogram.collect()[0].samples
            if sample.name.endswith("_count")
        )
    )


def _sample_sum(histogram) -> float:  # type: ignore[no-untyped-def]
    return float(
        next(
            sample.value
            for sample in histogram.collect()[0].samples
            if sample.name.endswith("_sum")
        )
    )


def test_route_open_observes_leg_ages_and_skew_once_per_episode() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), route_observer=observe_route_open)
    metrics = route_metrics("LTC-USD", "coinbase", "gemini")
    before = _sample_count(metrics.skew)

    books = [
        _top("coinbase", "100", "101", 1_000_000_000),
        _top("gemini", "103", "104", 1_250_000_000),
    ]
    assert len(detector.detect_for_pair("LTC-USD", books, 10, 2_000_000_000)) == 1
    # Resting spread: no second observation until the episode reopens.
    assert detector.detect_for_pair("LTC-USD", books, 11, 3_000_000_000) == []
    detector.close_all(12, 4_000_000_000)

    assert _sample_count(metrics.skew) == before + 1
    assert _sample_sum(metrics.skew) >= 0.25
    assert _sample_count(metrics.buy_age) >= 1 and _sample_count(metrics.sell_age) >= 1
    assert (
        route_open_age_skew_seconds.labels(
            pair="LTC-USD", buy_exchange="coinbase", sell_exchange="gemini"
        )
        is metrics.skew
    )


def test_route_open_skips_unknown_ages_and_other_event_kinds() -> None:
    metrics = route_metrics("LTC-USD", "gemini", "coinbase")
    before = (
        _sample_count(metrics.buy_age),
        _sample_count(metrics.sell_age),
        _sample_count(metrics.skew),
    )
    unknown = RouteLegAges(buy_age_ns=None, sell_age_ns=5_000_000, skew_ns=None)
    for kind in ("evaluated", "peak", "close"):
        observe_route_open(
            RouteAgeEvent(
                kind=kind,
                pair="LTC-USD",
                buy_exchange="gemini",
                sell_exchange="coinbase",
                start_ns=1,
                monotonic_ns=1,
                spread_pct=Decimal("1"),
                ages=RouteLegAges(1, 1, 0),
            )
        )
    observe_route_open(
        RouteAgeEvent(
            kind="open",
            pair="LTC-USD",
            buy_exchange="gemini",
            sell_exchange="coinbase",
            start_ns=1,
            monotonic_ns=1,
            spread_pct=Decimal("1"),
            ages=unknown,
        )
    )
    after = (
        _sample_count(metrics.buy_age),
        _sample_count(metrics.sell_age),
        _sample_count(metrics.skew),
    )
    assert after == (before[0], before[1] + 1, before[2])
