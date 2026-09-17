from decimal import Decimal

from arb.detector import ArbitrageDetector
from arb.types import PricingLedger, TopOfBook


def book(
    exchange: str, bid: str, bid_size: str, ask: str, ask_size: str, pair: str = "BTC-USD"
) -> TopOfBook:
    return TopOfBook(
        exchange=exchange,
        pair=pair,
        best_bid_price=Decimal(bid),
        best_bid_size=Decimal(bid_size),
        best_ask_price=Decimal(ask),
        best_ask_size=Decimal(ask_size),
        sequence=1,
        timestamp_ns=1,
    )


class Clock:
    """A settable monotonic clock so lifetimes are asserted exactly."""

    def __init__(self, now_ns: int = 0) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns


def test_no_detection_when_books_incomplete() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    assert detector.detect_for_pair("BTC-USD", [book("gemini", "100", "1", "101", "1")], 1) == []


def test_detection_above_threshold_opens_an_episode() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    opportunities = detector.detect_for_pair(
        "BTC-USD",
        [
            book("coinbase", "100.8", "2", "101", "1"),
            book("gemini", "102", "0.5", "102.4", "1"),
        ],
        10,
    )
    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.buy_exchange == "coinbase"
    assert opp.sell_exchange == "gemini"
    assert opp.buy_price == Decimal("101")
    assert opp.sell_price == Decimal("102")
    assert opp.max_size == Decimal("0.5")
    assert opp.start_ns == 10
    assert opp.is_open
    assert opp.end_ns is None and opp.duration_ns is None
    assert opp.peak_spread_pct == opp.spread_pct
    assert opp.peak_size == opp.max_size
    assert opp.peak_profit == opp.theoretical_profit
    assert detector.open_episodes() == [opp]


def test_episode_snapshots_pricing_ledgers_at_open() -> None:
    ledger = PricingLedger(
        Decimal("1000"),
        Decimal("1"),
        Decimal("100"),
        Decimal("101"),
        Decimal("1"),
        Decimal("0"),
        Decimal("0.4"),
        Decimal("0.6"),
        Decimal("-1.006"),
        Decimal("-0.006"),
        False,
    )
    calls: list[tuple[str, str, str, int]] = []

    def ledgers(pair: str, buy: str, sell: str, now_ns: int) -> tuple[PricingLedger, ...]:
        calls.append((pair, buy, sell, now_ns))
        return (ledger,)

    detector = ArbitrageDetector(Decimal("0.1"), ledger_factory=ledgers)
    [episode] = detector.detect_for_pair(
        "BTC-USD",
        [book("coinbase", "100", "1", "101", "1"), book("gemini", "102", "1", "103", "1")],
        10,
        20,
    )

    assert episode.pricing_ledgers == (ledger,)
    assert calls == [("BTC-USD", "coinbase", "gemini", 20)]


def test_no_detection_below_threshold() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("2"))
    opportunities = detector.detect_for_pair(
        "BTC-USD",
        [
            book("coinbase", "100.8", "2", "101", "1"),
            book("gemini", "102", "0.5", "102.4", "1"),
        ],
        10,
    )
    assert opportunities == []


def test_no_detection_with_empty_or_single_book() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    assert detector.detect_for_pair("BTC-USD", [], 1) == []
    assert detector.detect_for_pair("BTC-USD", [book("a", "100", "1", "101", "1")], 1) == []


def test_three_exchanges_yield_multiple_pairwise_opportunities() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    # gemini ask 100.0, coinbase bid 102, binance bid 103 — two arbs originate from buying on gemini.
    opportunities = detector.detect_for_pair(
        "BTC-USD",
        [
            book("gemini", "99.9", "5", "100.0", "5"),
            book("coinbase", "102.0", "1", "102.1", "1"),
            book("binance", "103.0", "2", "103.1", "1"),
        ],
        1,
    )
    legs = {(o.buy_exchange, o.sell_exchange) for o in opportunities}
    # gemini→coinbase, gemini→binance, coinbase→binance all qualify.
    assert ("gemini", "coinbase") in legs
    assert ("gemini", "binance") in legs
    assert ("coinbase", "binance") in legs
    # Reverse legs (coinbase→gemini, etc.) must NOT qualify because their bid <= other's ask.
    assert ("coinbase", "gemini") not in legs
    assert ("binance", "gemini") not in legs


def test_max_size_is_min_of_legs_and_profit_is_correct() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    [opp] = detector.detect_for_pair(
        "BTC-USD",
        [
            book("a", "99", "1", "100", "0.4"),
            book("b", "110", "0.7", "111", "1"),
        ],
        1,
    )
    assert opp.max_size == Decimal("0.4")
    # profit = max_size * (sell_bid - buy_ask) = 0.4 * (110 - 100) = 4
    assert opp.quote_asset == "USD"
    assert opp.theoretical_profit == Decimal("4.0")
    # spread_pct = (110 - 100) / 100 * 100 = 10
    assert opp.spread_pct == Decimal("10")


def test_threshold_is_strict_lower_bound() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("1"))
    # spread = (101 - 100) / 100 * 100 = 1.0 — exactly at threshold should pass (>=).
    opps = detector.detect_for_pair(
        "BTC-USD",
        [book("a", "99", "1", "100", "1"), book("b", "101", "1", "102", "1")],
        1,
    )
    assert len(opps) == 1
    # At threshold + epsilon below, should be rejected.
    detector_strict = ArbitrageDetector(threshold_pct=Decimal("1.01"))
    assert (
        detector_strict.detect_for_pair(
            "BTC-USD",
            [book("a", "99", "1", "100", "1"), book("b", "101", "1", "102", "1")],
            1,
        )
        == []
    )


def test_opportunity_payload_serializes_decimals_as_strings() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    [opp] = detector.detect_for_pair(
        "BTC-USD",
        [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")],
        42,
    )
    payload = opp.as_payload()
    assert payload["pair"] == "BTC-USD"
    assert payload["start_ns"] == "42"
    assert payload["end_ns"] is None
    assert payload["duration_ns"] is None
    assert payload["close_reason"] is None
    assert payload["buy_price"] == "100"
    assert payload["sell_price"] == "102"
    # Every Decimal field must be a string.
    for key in (
        "buy_price",
        "sell_price",
        "spread_pct",
        "max_size",
        "theoretical_profit",
        "peak_spread_pct",
        "peak_size",
        "peak_profit",
    ):
        assert isinstance(payload[key], str)


def test_mixed_quote_books_do_not_produce_an_opportunity() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    usd_book = book("gemini", "99", "1", "100", "1")
    usdt_book = book("binance", "102", "1", "103", "1", pair="BTC-USDT")

    assert detector.detect_for_pair("BTC-USD", [usd_book, usdt_book], 1) == []


def test_decimal_precision_is_preserved() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.0001"))
    # Use prices that float arithmetic would garble.
    [opp] = detector.detect_for_pair(
        "BTC-USD",
        [book("a", "0.1", "1", "0.1", "1"), book("b", "0.3", "1", "0.4", "1")],
        1,
    )
    # 0.3 - 0.1 should be exactly Decimal("0.2"), not 0.19999...
    assert opp.sell_price - opp.buy_price == Decimal("0.2")


# --- Episode lifecycle (ARB-031) ---


def test_resting_spread_is_one_episode_not_one_row_per_update() -> None:
    clock = Clock(1_000)
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=clock)
    books = [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")]

    [opened] = detector.detect_for_pair("BTC-USD", books, 10)
    assert opened.is_open

    # The same resting quotes re-detected many times produce nothing new.
    for wall in range(11, 20):
        clock.now_ns += 100
        assert detector.detect_for_pair("BTC-USD", books, wall) == []
    assert detector.open_episodes() == [opened]

    clock.now_ns += 100
    [closed] = detector.detect_for_pair(
        "BTC-USD", [book("a", "99", "1", "100", "1"), book("b", "100.05", "1", "101", "1")], 20
    )
    assert closed.route == opened.route
    assert closed.start_ns == 10
    assert closed.close_reason == "spread_closed"
    assert closed.duration_ns == 1_000  # ten monotonic ticks of 100 ns
    assert closed.end_ns == 10 + 1_000
    assert closed.close_spread_pct == Decimal("0.05")
    assert detector.open_episodes() == []


def test_peak_tracks_widest_spread_and_size_at_that_moment() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=Clock())
    [opened] = detector.detect_for_pair(
        "BTC-USD", [book("a", "99", "1", "100", "3"), book("b", "102", "2", "103", "1")], 1
    )
    assert opened.peak_spread_pct == Decimal("2")
    assert opened.peak_size == Decimal("2")

    # Wider spread with a smaller size at that moment: peak follows the spread.
    assert (
        detector.detect_for_pair(
            "BTC-USD", [book("a", "99", "1", "100", "3"), book("b", "105", "0.5", "106", "1")], 2
        )
        == []
    )
    # Narrower again with a larger size: peak must not move.
    assert (
        detector.detect_for_pair(
            "BTC-USD", [book("a", "99", "1", "100", "3"), book("b", "103", "9", "104", "1")], 3
        )
        == []
    )
    [closed] = detector.detect_for_pair(
        "BTC-USD", [book("a", "99", "1", "100", "3"), book("b", "100", "9", "101", "1")], 4
    )
    assert closed.peak_spread_pct == Decimal("5")
    assert closed.peak_size == Decimal("0.5")
    assert closed.peak_profit == Decimal("0.5") * Decimal("5")
    # Open-time values are preserved alongside the peak.
    assert closed.spread_pct == Decimal("2")
    assert closed.max_size == Decimal("2")
    assert closed.close_spread_pct == Decimal("0")


def test_missing_leg_closes_as_book_ineligible() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=Clock())
    [opened] = detector.detect_for_pair(
        "BTC-USD", [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")], 1
    )
    # Venue b left the eligible set; only a's book is offered.
    [closed] = detector.detect_for_pair("BTC-USD", [book("a", "99", "1", "100", "1")], 2)
    assert closed.route == opened.route
    assert closed.close_reason == "book_ineligible"
    assert closed.close_spread_pct is None


def test_close_for_book_closes_only_routes_touching_that_leg() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=Clock())
    opened = detector.detect_for_pair(
        "BTC-USD",
        [
            book("a", "99", "1", "100", "1"),
            book("b", "102", "1", "103", "1"),
            book("c", "104", "1", "105", "1"),
        ],
        1,
    )
    assert {o.route[1:] for o in opened} == {("a", "b"), ("a", "c"), ("b", "c")}

    closed = detector.close_for_book("c", "BTC-USD", 2)
    assert {o.route[1:] for o in closed} == {("a", "c"), ("b", "c")}
    assert all(o.close_reason == "book_ineligible" for o in closed)
    assert [o.route[1:] for o in detector.open_episodes()] == [("a", "b")]
    # Another pair's episodes are untouched by a close on this pair.
    assert detector.close_for_book("a", "ETH-USD", 3) == []


def test_close_all_marks_shutdown() -> None:
    clock = Clock(5)
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=clock)
    detector.detect_for_pair(
        "BTC-USD", [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")], 1
    )
    detector.detect_for_pair(
        "ETH-USD",
        [
            book("a", "9", "1", "10", "1", pair="ETH-USD"),
            book("b", "12", "1", "13", "1", pair="ETH-USD"),
        ],
        1,
    )
    clock.now_ns = 55
    closed = detector.close_all(99)
    assert {o.pair for o in closed} == {"BTC-USD", "ETH-USD"}
    assert all(o.close_reason == "shutdown" and o.duration_ns == 50 for o in closed)
    assert detector.open_episodes() == []
    assert detector.close_all(100) == []


def test_lifetime_uses_monotonic_delta_not_wall_clock() -> None:
    # A system-clock step backwards between open and close must not yield a
    # negative lifetime, and a step forwards must not inflate it.
    clock = Clock(0)
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=clock)
    books = [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")]
    [opened] = detector.detect_for_pair("BTC-USD", books, 1_000_000)
    clock.now_ns = 250
    [closed] = detector.detect_for_pair("BTC-USD", [books[0]], 1)  # wall clock stepped back
    assert closed.start_ns == opened.start_ns == 1_000_000
    assert closed.duration_ns == 250
    assert closed.end_ns == 1_000_250

    [reopened] = detector.detect_for_pair("BTC-USD", books, 2)
    clock.now_ns = 300
    [closed_again] = detector.detect_for_pair("BTC-USD", [books[0]], 10**12)  # stepped forward
    assert closed_again.duration_ns == 50
    assert closed_again.end_ns == reopened.start_ns + 50


def test_explicit_monotonic_argument_drives_replay_determinism() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    books = [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")]
    detector.detect_for_pair("BTC-USD", books, 1, monotonic_ns=1_000)
    [closed] = detector.detect_for_pair("BTC-USD", [books[0]], 2, monotonic_ns=4_000)
    assert closed.duration_ns == 3_000


def test_reopen_inside_one_wall_clock_tick_keeps_a_distinct_identity() -> None:
    # time.time_ns() advances in ~1 ms steps on some hosts, so a route that
    # closes and reopens between ticks would reuse (start_ns, route) and the
    # store's upsert would overwrite the first episode with the second.
    clock = Clock(0)
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"), monotonic_clock=clock)
    wide = [book("a", "99", "1", "100", "1"), book("b", "102", "1", "103", "1")]
    narrow = [book("a", "99", "1", "100", "1"), book("b", "100", "1", "101", "1")]

    [first] = detector.detect_for_pair("BTC-USD", wide, 5_000)
    clock.now_ns = 10
    [closed] = detector.detect_for_pair("BTC-USD", narrow, 5_000)
    [second] = detector.detect_for_pair("BTC-USD", wide, 5_000)

    assert first.start_ns == 5_000
    assert closed.end_ns == 5_010
    assert second.start_ns == 5_001, "nudged past the previous start, not the wall clock"
    assert (first.start_ns, first.route) != (second.start_ns, second.route)
    # Once the wall clock moves on, starts follow it again.
    clock.now_ns = 20
    detector.detect_for_pair("BTC-USD", narrow, 6_000)
    [third] = detector.detect_for_pair("BTC-USD", wide, 6_000)
    assert third.start_ns == 6_000
    # Other routes are nudged independently.
    other = [
        book("a", "9", "1", "10", "1", pair="ETH-USD"),
        book("b", "12", "1", "13", "1", pair="ETH-USD"),
    ]
    [eth] = detector.detect_for_pair("ETH-USD", other, 5_000)
    assert eth.start_ns == 5_000
