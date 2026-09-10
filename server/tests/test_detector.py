from decimal import Decimal

from arb.detector import ArbitrageDetector
from arb.types import TopOfBook


def book(exchange: str, bid: str, bid_size: str, ask: str, ask_size: str) -> TopOfBook:
    return TopOfBook(
        exchange=exchange,
        pair="BTC-USD",
        best_bid_price=Decimal(bid),
        best_bid_size=Decimal(bid_size),
        best_ask_price=Decimal(ask),
        best_ask_size=Decimal(ask_size),
        sequence=1,
        timestamp_ns=1,
    )


def test_no_detection_when_books_incomplete() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    assert detector.detect_for_pair("BTC-USD", [book("gemini", "100", "1", "101", "1")], 1) == []


def test_detection_above_threshold() -> None:
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
    assert payload["timestamp_ns"] == "42"
    assert payload["buy_price"] == "100"
    assert payload["sell_price"] == "102"
    # Every Decimal field must be a string.
    for key in ("buy_price", "sell_price", "spread_pct", "max_size", "theoretical_profit"):
        assert isinstance(payload[key], str)


def test_mixed_quote_books_do_not_produce_an_opportunity() -> None:
    detector = ArbitrageDetector(threshold_pct=Decimal("0.1"))
    usd_book = book("gemini", "99", "1", "100", "1")
    usdt_book = TopOfBook(
        exchange="binance",
        pair="BTC-USDT",
        best_bid_price=Decimal("102"),
        best_bid_size=Decimal("1"),
        best_ask_price=Decimal("103"),
        best_ask_size=Decimal("1"),
        sequence=1,
        timestamp_ns=1,
    )

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
