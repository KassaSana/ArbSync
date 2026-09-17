from __future__ import annotations

from decimal import Decimal

import pytest
from arb.pricing import DepthQuote, FillRateTracker, price_book, walk_levels
from arb.types import PriceLevel
from hypothesis import given, settings
from hypothesis import strategies as st


def levels(*pairs: tuple[str, str]) -> list[PriceLevel]:
    return [PriceLevel(Decimal(price), Decimal(size)) for price, size in pairs]


def test_walk_takes_a_partial_last_level_and_spends_exactly_the_notional() -> None:
    asks = levels(("100", "1"), ("102", "2"), ("110", "5"))
    fill = walk_levels(asks, Decimal("250"))

    # 100 from the first level, 150 of the 204 available at 102.
    assert fill.insufficient_depth is False
    assert fill.filled_notional == Decimal("250")
    assert fill.levels_used == 2
    # 1 + 150/102 = 42/17 base; 250 / (42/17) = 4250/42 = 2125/21, each rounded once.
    assert fill.filled_base == Decimal(42) / Decimal(17)
    assert fill.vwap == Decimal(2125) / Decimal(21)


def test_walk_reports_insufficient_depth_with_what_was_available() -> None:
    asks = levels(("100", "1"), ("102", "2"))
    fill = walk_levels(asks, Decimal("1000"))

    assert fill.insufficient_depth is True
    assert fill.vwap is None
    assert fill.filled_notional == Decimal("304")
    assert fill.filled_base == Decimal("3")
    assert fill.levels_used == 2


def test_walk_rejects_a_non_positive_notional_and_skips_empty_levels() -> None:
    with pytest.raises(ValueError):
        walk_levels(levels(("100", "1")), Decimal("0"))
    fill = walk_levels(levels(("100", "0"), ("101", "1")), Decimal("50"))
    assert fill.levels_used == 1
    assert fill.vwap == Decimal("101")


def test_price_book_quotes_both_sides_for_every_notional() -> None:
    quotes = price_book(
        "gemini",
        "BTC-USD",
        bids=levels(("99", "1")),
        asks=levels(("101", "1")),
        notionals=[Decimal("50"), Decimal("500")],
        subscribed_depth_levels=None,
    )
    assert [(q.side, str(q.fill.notional), q.fill.insufficient_depth) for q in quotes] == [
        ("buy", "50", False),
        ("sell", "50", False),
        ("buy", "500", True),
        ("sell", "500", True),
    ]
    payload = quotes[0].as_payload()
    assert payload["vwap"] == "101"
    assert payload["subscribed_depth_levels"] is None
    assert quotes[2].as_payload()["vwap"] is None


# --- Properties (ARB-032) ---

price = st.integers(min_value=1_00, max_value=200_00).map(lambda v: Decimal(v) / 100)
size = st.integers(min_value=1, max_value=50).map(lambda v: Decimal(v) / 10)
book_side = st.lists(st.tuples(price, size), min_size=1, max_size=8).map(
    lambda rows: [PriceLevel(p, s) for p, s in sorted(rows, key=lambda r: r[0])]
)
notional = st.integers(min_value=1, max_value=100_000).map(Decimal)


@settings(max_examples=300)
@given(asks=book_side, small=notional, large=notional)
def test_vwap_is_monotonically_non_improving_in_notional(
    asks: list[PriceLevel], small: Decimal, large: Decimal
) -> None:
    small, large = min(small, large), max(small, large)
    lesser = walk_levels(asks, small)
    greater = walk_levels(asks, large)
    if greater.insufficient_depth:
        return
    assert lesser.vwap is not None and greater.vwap is not None
    # Buying more can only walk deeper into worse prices.
    assert greater.vwap >= lesser.vwap


@settings(max_examples=300)
@given(asks=book_side, fraction=st.integers(min_value=1, max_value=100))
def test_vwap_equals_top_of_book_within_the_first_level(
    asks: list[PriceLevel], fraction: int
) -> None:
    top = asks[0]
    within_first = top.price * top.size * fraction / 100
    fill = walk_levels(asks, within_first)
    assert fill.insufficient_depth is False
    assert fill.levels_used == 1
    assert fill.vwap == top.price


@settings(max_examples=300)
@given(asks=book_side, wanted=notional)
def test_insufficient_depth_exactly_when_summed_depth_is_short(
    asks: list[PriceLevel], wanted: Decimal
) -> None:
    available = sum((level.price * level.size for level in asks), Decimal(0))
    fill = walk_levels(asks, wanted)
    assert fill.insufficient_depth is (available < wanted)
    if fill.insufficient_depth:
        assert fill.filled_notional == available
        assert fill.vwap is None
    else:
        assert fill.filled_notional == wanted
        assert fill.vwap is not None


# --- Fill-rate tracker ---


def quote(exchange: str, side: str, notional: str, insufficient: bool) -> DepthQuote:
    fill = walk_levels(levels(("100", "1")), Decimal(notional))
    assert fill.insufficient_depth is insufficient
    return DepthQuote(exchange, "BTC-USD", side, 5000 if exchange == "binance" else None, fill)  # type: ignore[arg-type]


def test_fill_rate_tracker_counts_per_venue_notional_and_side() -> None:
    tracker = FillRateTracker(notionals=(Decimal("50"), Decimal("500")))
    tracker.observe([quote("binance", "buy", "50", False), quote("binance", "buy", "500", True)])
    tracker.observe([quote("binance", "buy", "50", False), quote("binance", "buy", "500", True)])
    tracker.observe([quote("gemini", "sell", "500", True)])
    tracker.observe_ineligible("binance", "BTC-USD")

    rows = tracker.rows()
    assert [
        (r["exchange"], r["notional"], r["side"], r["observations"], r["filled"]) for r in rows
    ] == [
        ("binance", "50", "buy", 2, 2),
        ("binance", "500", "buy", 2, 0),
        ("gemini", "500", "sell", 1, 0),
    ]
    assert rows[0]["fill_rate"] == 1.0 and rows[1]["fill_rate"] == 0.0
    # The ineligible sample is reported beside the book, never as a failed fill.
    assert rows[0]["ineligible_samples"] == 1 and rows[2]["ineligible_samples"] == 0
    assert rows[0]["subscribed_depth_levels"] == 5000 and rows[2]["subscribed_depth_levels"] is None


# --- Manager depth read and the sampler ---


def snapshot(exchange: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]):
    from arb.types import EventKind, MarketEvent

    return MarketEvent(
        exchange=exchange,
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=1,
        bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in asks),
    )


def test_depth_levels_returns_every_level_best_first_only_while_eligible() -> None:
    from arb.orderbook import OrderBookManager

    manager = OrderBookManager(max_age_seconds=1.0, clock=lambda: 0)
    manager.apply(snapshot("gemini", [("99", "1"), ("100", "2")], [("101", "3"), ("105", "1")]))

    sides = manager.depth_levels("gemini", "BTC-USD", 0)
    assert sides is not None
    bids, asks = sides
    assert [str(level.price) for level in bids] == ["100", "99"]
    assert [str(level.price) for level in asks] == ["101", "105"]

    # Aged past freshness: no depth to price, rather than a stale walk.
    assert manager.depth_levels("gemini", "BTC-USD", 2_000_000_000) is None
    assert manager.depth_levels("gemini", "ETH-USD", 0) is None


def test_sampler_observes_eligible_books_and_counts_ineligible_ones() -> None:
    from arb.orderbook import OrderBookManager
    from arb.pricing import DepthSampler

    manager = OrderBookManager(max_age_seconds=1.0, clock=lambda: 0)
    manager.apply(snapshot("gemini", [("99", "10")], [("101", "10")]))  # 1010 available
    manager.apply(snapshot("binance", [("99", "1")], [("101", "1")]))  # 101 available
    sampler = DepthSampler(
        manager,
        [Decimal("100"), Decimal("1000")],
        {"gemini": None, "binance": 5000},
        interval_seconds=5.0,
    )

    sampler.sample_all(0)
    manager.invalidate("binance", "BTC-USD")
    sampler.sample_all(0)

    assert sampler.samples == 2
    rows = {(r["exchange"], r["notional"], r["side"]): r for r in sampler.tracker.rows()}
    assert rows[("gemini", "100", "buy")]["filled"] == 2
    assert rows[("gemini", "1000", "buy")]["filled"] == 2
    assert rows[("binance", "100", "buy")]["observations"] == 1
    assert rows[("binance", "1000", "buy")]["filled"] == 0
    # The second binance sample was ineligible: not a failed fill, a missing observation.
    assert rows[("binance", "100", "buy")]["ineligible_samples"] == 1
    assert rows[("binance", "100", "buy")]["subscribed_depth_levels"] == 5000
    assert sampler.quote("binance", "BTC-USD", 0) == []
    assert len(sampler.quote_pair("BTC-USD", 0)) == 4  # gemini only: 2 notionals x 2 sides
