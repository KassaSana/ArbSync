from __future__ import annotations

from decimal import Decimal

from arb.orderbook import OrderBookManager, SortedLevels
from arb.types import BookUpdateResult, EventKind, MarketEvent, PriceLevel
from hypothesis import given, settings
from hypothesis import strategies as st


def event(
    *,
    kind: EventKind,
    sequence: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    exchange: str = "gemini",
    pair: str = "BTC-USD",
) -> MarketEvent:
    return MarketEvent(
        exchange=exchange,
        pair=pair,
        kind=kind,
        sequence=sequence,
        timestamp_ns=sequence,
        bids=tuple(PriceLevel(price=Decimal(price), size=Decimal(size)) for price, size in bids),
        asks=tuple(PriceLevel(price=Decimal(price), size=Decimal(size)) for price, size in asks),
    )


price_bid = st.integers(min_value=90_00, max_value=100_00).map(lambda value: f"{value / 100:.2f}")
price_ask = st.integers(min_value=101_00, max_value=111_00).map(lambda value: f"{value / 100:.2f}")
size_any = st.integers(min_value=0, max_value=5).map(lambda value: f"{value}")
size_positive = st.integers(min_value=1, max_value=5).map(lambda value: f"{value}")
levels_bid = st.lists(st.tuples(price_bid, size_any), min_size=0, max_size=4)
levels_ask = st.lists(st.tuples(price_ask, size_any), min_size=0, max_size=4)
snapshot_bid_levels = st.lists(st.tuples(price_bid, size_positive), min_size=1, max_size=4)
snapshot_ask_levels = st.lists(st.tuples(price_ask, size_positive), min_size=1, max_size=4)


def assert_book_invariants(manager: OrderBookManager) -> None:
    book = manager._books[("gemini", "BTC-USD")]
    bid_prices = book.bids._prices
    ask_prices = book.asks._prices

    assert bid_prices == sorted(bid_prices)
    assert ask_prices == sorted(ask_prices)
    assert list(reversed(bid_prices)) == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)
    assert all(size > Decimal("0") for size in book.bids._sizes.values())
    assert all(size > Decimal("0") for size in book.asks._sizes.values())

    top = manager.top_of_book("gemini", "BTC-USD")
    if top is not None:
        assert top.best_bid_price < top.best_ask_price


@settings(max_examples=100, deadline=None)
@given(
    snapshot_bids=snapshot_bid_levels,
    snapshot_asks=snapshot_ask_levels,
    deltas=st.lists(st.tuples(levels_bid, levels_ask), min_size=1, max_size=40),
)
def test_orderbook_invariants_hold_under_generated_updates(
    snapshot_bids: list[tuple[str, str]],
    snapshot_asks: list[tuple[str, str]],
    deltas: list[tuple[list[tuple[str, str]], list[tuple[str, str]]]],
) -> None:
    manager = OrderBookManager()
    result = manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=1, bids=snapshot_bids, asks=snapshot_asks)
    )

    assert result.accepted is True
    assert_book_invariants(manager)

    for index, (bids, asks) in enumerate(deltas, start=2):
        result = manager.apply(event(kind=EventKind.DELTA, sequence=index, bids=bids, asks=asks))
        assert result.accepted is True or result.reason == "book_incomplete"
        assert_book_invariants(manager)


@settings(max_examples=50, deadline=None)
@given(
    snapshot_bids=snapshot_bid_levels,
    snapshot_asks=snapshot_ask_levels,
    gap_size=st.integers(min_value=2, max_value=5),
    delta_bids=levels_bid,
    delta_asks=levels_ask,
)
def test_sequence_gap_detection_fires_when_expected(
    snapshot_bids: list[tuple[str, str]],
    snapshot_asks: list[tuple[str, str]],
    gap_size: int,
    delta_bids: list[tuple[str, str]],
    delta_asks: list[tuple[str, str]],
) -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=snapshot_bids, asks=snapshot_asks)
    )

    result: BookUpdateResult = manager.apply(
        event(kind=EventKind.DELTA, sequence=10 + gap_size, bids=delta_bids, asks=delta_asks)
    )

    assert result.accepted is False
    assert result.reason == "sequence_gap"
    assert result.stale is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is False


level_price = st.integers(min_value=1, max_value=40).map(lambda value: Decimal(value) / 4)
level_size = st.integers(min_value=0, max_value=3).map(Decimal)
level_operations = st.lists(
    st.one_of(
        st.tuples(st.just("set"), level_price, level_size),
        st.tuples(st.just("remove"), level_price, st.just(Decimal(0))),
    ),
    max_size=60,
)


@settings(max_examples=200, deadline=None)
@given(descending=st.booleans(), operations=level_operations, limit=st.integers(0, 6))
def test_sorted_levels_match_a_dict_oracle(
    descending: bool, operations: list[tuple[str, Decimal, Decimal]], limit: int
) -> None:
    """set_level/remove keep the price list sorted and in step with the size map.

    The oracle is a plain dict of the levels that should remain: a level exists
    only while its most recent size was positive. Every query the book manager
    relies on (best, top_n) must agree with that dict sorted the right way.
    """
    levels = SortedLevels(descending=descending)
    oracle: dict[Decimal, Decimal] = {}

    for operation, price, size in operations:
        if operation == "set":
            levels.set_level(price, size)
            if size > 0:
                oracle[price] = size
            else:
                oracle.pop(price, None)
        else:
            levels.remove(price)
            oracle.pop(price, None)

        expected = sorted(oracle, reverse=descending)
        assert levels._prices == sorted(oracle)
        assert set(levels._prices) == set(levels._sizes) == set(oracle)
        assert all(levels._sizes[price] == oracle[price] for price in oracle)

        best = levels.best()
        if expected:
            assert best == PriceLevel(price=expected[0], size=oracle[expected[0]])
        else:
            assert best is None
        assert levels.top_n(limit) == [
            PriceLevel(price=price, size=oracle[price]) for price in expected[:limit]
        ]
