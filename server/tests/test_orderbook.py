from decimal import Decimal

from arb.orderbook import OrderBookManager
from arb.types import EventKind, MarketEvent, PriceLevel


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


def test_snapshot_initializes_book() -> None:
    manager = OrderBookManager()
    result = manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=100,
            bids=[("100", "2"), ("99", "1")],
            asks=[("101", "3"), ("102", "1")],
        )
    )
    assert result.accepted is True
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("100")
    assert manager.best_ask("gemini", "BTC-USD") == Decimal("101")


def test_incomplete_snapshot_clears_chain_and_cannot_be_healed_by_delta() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )

    result = manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=20, bids=[("99", "1")], asks=[("101", "0")])
    )

    assert result.accepted is False
    assert result.reason == "snapshot_incomplete"
    assert result.stale is True
    assert result.requires_resync is True
    assert manager.best_bid("gemini", "BTC-USD") is None
    assert manager.eligibility("gemini", "BTC-USD").reason == "uninitialized"

    delta = manager.apply(event(kind=EventKind.DELTA, sequence=21, bids=[], asks=[("101", "1")]))
    assert delta.accepted is False
    assert delta.reason == "book_stale"
    assert delta.requires_resync is False

    recovered = manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=30, bids=[("99", "1")], asks=[("100", "1")])
    )
    assert recovered.accepted is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True


def test_crossed_snapshot_clears_chain_and_requires_resync() -> None:
    manager = OrderBookManager()

    result = manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=1, bids=[("101", "1")], asks=[("101", "1")])
    )

    assert result.accepted is False
    assert result.reason == "snapshot_crossed"
    assert result.stale is True
    assert result.requires_resync is True
    assert manager.top_of_book("gemini", "BTC-USD") is None
    assert manager.eligibility("gemini", "BTC-USD").reason == "uninitialized"


def test_delta_updates_best_levels() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )
    result = manager.apply(
        event(kind=EventKind.DELTA, sequence=11, bids=[("100.5", "1.5")], asks=[])
    )
    assert result.accepted is True
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("100.5")


def test_size_zero_removes_level() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=10,
            bids=[("100", "2"), ("99", "1")],
            asks=[("101", "3")],
        )
    )
    manager.apply(event(kind=EventKind.DELTA, sequence=11, bids=[("100", "0")], asks=[]))
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("99")


def test_out_of_order_delta_is_rejected() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )
    result = manager.apply(event(kind=EventKind.DELTA, sequence=10, bids=[("100.5", "1")], asks=[]))
    assert result.accepted is False
    assert result.reason == "out_of_order"


def test_gap_detection_marks_book_stale() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )
    result = manager.apply(event(kind=EventKind.DELTA, sequence=12, bids=[("100.5", "1")], asks=[]))
    assert result.accepted is False
    assert result.reason == "sequence_gap"
    assert result.requires_resync is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is False


def test_stale_book_blocks_new_deltas_until_snapshot() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )
    manager.apply(event(kind=EventKind.DELTA, sequence=12, bids=[("100.5", "1")], asks=[]))
    result = manager.apply(event(kind=EventKind.DELTA, sequence=13, bids=[("100.7", "1")], asks=[]))
    assert result.accepted is False
    assert result.reason == "book_stale"


def test_crossed_book_resets_book() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )
    result = manager.apply(event(kind=EventKind.DELTA, sequence=11, bids=[("102", "1")], asks=[]))
    assert result.accepted is False
    assert result.reason == "crossed_book"
    assert result.requires_resync is True


def test_incomplete_delta_can_be_healed_by_next_contiguous_delta() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )

    incomplete = manager.apply(
        event(kind=EventKind.DELTA, sequence=11, bids=[], asks=[("101", "0")])
    )
    assert incomplete.accepted is False
    assert incomplete.reason == "book_incomplete"
    assert incomplete.requires_resync is False
    assert manager.eligibility("gemini", "BTC-USD").reason == "incomplete"

    healed = manager.apply(event(kind=EventKind.DELTA, sequence=12, bids=[], asks=[("102", "1")]))
    assert healed.accepted is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True


def test_cold_start_delta_is_rejected_until_snapshot() -> None:
    manager = OrderBookManager()
    result = manager.apply(
        event(kind=EventKind.DELTA, sequence=1, bids=[("100", "1")], asks=[("101", "1")])
    )
    assert result.accepted is False
    assert result.reason == "book_stale"
    assert result.stale is True


def test_recovery_after_gap_with_new_snapshot() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("100", "2")], asks=[("101", "3")])
    )
    # Trigger a gap.
    manager.apply(event(kind=EventKind.DELTA, sequence=12, bids=[("100.5", "1")], asks=[]))
    assert manager.eligibility("gemini", "BTC-USD").eligible is False
    # Recover with a fresh snapshot at the new sequence.
    result = manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=20, bids=[("99", "1")], asks=[("100", "1")])
    )
    assert result.accepted is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("99")
    assert manager.best_ask("gemini", "BTC-USD") == Decimal("100")


def test_multiple_pairs_and_exchanges_are_isolated() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("100", "1")],
            asks=[("101", "1")],
            exchange="gemini",
            pair="BTC-USD",
        )
    )
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("200", "1")],
            asks=[("201", "1")],
            exchange="coinbase",
            pair="ETH-USD",
        )
    )
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("100")
    assert manager.best_bid("coinbase", "ETH-USD") == Decimal("200")
    assert manager.best_ask("coinbase", "BTC-USD") is None
    assert manager.best_bid("gemini", "ETH-USD") is None


def test_known_pairs_returns_all_seen_keys_sorted() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("100", "1")],
            asks=[("101", "1")],
            exchange="binance",
            pair="BTC-USD",
        )
    )
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("100", "1")],
            asks=[("101", "1")],
            exchange="gemini",
            pair="BTC-USD",
        )
    )
    assert manager.known_pairs() == [("binance", "BTC-USD"), ("gemini", "BTC-USD")]


def test_level_snapshot_returns_top_n_in_correct_order() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("100", "1"), ("99", "1"), ("98", "1")],
            asks=[("101", "1"), ("102", "1"), ("103", "1")],
        )
    )
    levels = manager.level_snapshot("gemini", "BTC-USD", limit=2)
    assert levels is not None
    bids, asks = levels
    assert [level.price for level in bids] == [Decimal("100"), Decimal("99")]
    assert [level.price for level in asks] == [Decimal("101"), Decimal("102")]


def test_level_snapshot_none_when_book_stale() -> None:
    manager = OrderBookManager()
    assert manager.level_snapshot("gemini", "BTC-USD") is None


def test_incomplete_snapshot_leaves_book_uninitialized() -> None:
    manager = OrderBookManager()
    result = manager.apply(event(kind=EventKind.SNAPSHOT, sequence=1, bids=[("100", "1")], asks=[]))
    assert result.reason == "snapshot_incomplete"
    assert manager.top_of_book("gemini", "BTC-USD") is None
    assert manager.eligibility("gemini", "BTC-USD").eligible is False
    assert manager.eligibility("gemini", "BTC-USD").reason == "uninitialized"


def test_size_zero_in_snapshot_does_not_create_level() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("100", "1"), ("99", "0")],
            asks=[("101", "1")],
        )
    )
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("100")
    levels = manager.level_snapshot("gemini", "BTC-USD")
    assert levels is not None
    assert len(levels[0]) == 1


def test_remove_nonexistent_level_is_noop() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=1, bids=[("100", "1")], asks=[("101", "1")])
    )
    # Removing a price level that doesn't exist must not crash or alter state.
    result = manager.apply(event(kind=EventKind.DELTA, sequence=2, bids=[("50", "0")], asks=[]))
    assert result.accepted is True
    assert manager.best_bid("gemini", "BTC-USD") == Decimal("100")


def test_book_becomes_ineligible_when_age_limit_is_exceeded() -> None:
    now = [1_000]
    manager = OrderBookManager(max_age_seconds=0.000001, clock=lambda: now[0])
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("100", "1")],
            asks=[("101", "1")],
        ),
        received_monotonic_ns=now[0],
    )
    assert manager.eligibility("gemini", "BTC-USD", now[0]).eligible is True

    now[0] += 1_001
    status = manager.eligibility("gemini", "BTC-USD", now[0])
    assert status.eligible is False
    assert status.reason == "too_old"
    assert manager.eligible_top_of_book("gemini", "BTC-USD", now[0]) is None


def test_disconnect_invalidates_book_until_new_snapshot() -> None:
    manager = OrderBookManager()
    manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=1, bids=[("100", "1")], asks=[("101", "1")])
    )
    assert manager.eligibility("gemini", "BTC-USD").eligible is True

    manager.set_exchange_connected("gemini", False)
    status = manager.eligibility("gemini", "BTC-USD")
    assert status.eligible is False
    assert status.reason == "disconnected"
    assert manager.top_of_book("gemini", "BTC-USD") is None

    manager.set_exchange_connected("gemini", True)
    delta = manager.apply(event(kind=EventKind.DELTA, sequence=2, bids=[("100.5", "1")], asks=[]))
    assert delta.accepted is False
    assert delta.reason == "book_stale"

    snapshot = manager.apply(
        event(kind=EventKind.SNAPSHOT, sequence=10, bids=[("99", "1")], asks=[("100", "1")])
    )
    assert snapshot.accepted is True
    assert manager.eligibility("gemini", "BTC-USD").eligible is True


def test_cached_top_tracks_size_updates_deletions_gaps_and_recovery() -> None:
    manager = OrderBookManager(clock=lambda: 1_000)

    def event(kind, sequence, bids=(), asks=()):
        return MarketEvent(
            "gemini",
            "BTC-USD",
            kind,
            sequence,
            sequence,
            bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in bids),
            asks=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in asks),
        )

    manager.apply(event(EventKind.SNAPSHOT, 1, [("100", "1"), ("99", "2")], [("101", "3")]))
    original = manager.top_of_book("gemini", "BTC-USD")
    manager.apply(event(EventKind.DELTA, 2, [("100", "4")]))
    updated = manager.top_of_book("gemini", "BTC-USD")
    assert updated.best_bid_size == Decimal("4")
    assert updated.sequence == 2
    assert original.best_bid_size == Decimal("1")
    manager.apply(event(EventKind.DELTA, 3, [("100", "0")]))
    assert manager.top_of_book("gemini", "BTC-USD").best_bid_price == Decimal("99")
    manager.apply(event(EventKind.DELTA, 5, [("98", "1")]))
    assert manager.top_of_book("gemini", "BTC-USD") is None
    manager.apply(event(EventKind.SNAPSHOT, 10, [("90", "1")], [("91", "1")]))
    assert manager.top_of_book("gemini", "BTC-USD").best_bid_price == Decimal("90")
    manager.set_exchange_connected("gemini", False)
    assert manager.top_of_book("gemini", "BTC-USD") is None


def test_eligible_books_matches_per_venue_eligibility_sweep() -> None:
    """The fast per-pair path must agree with evaluating each venue separately.

    `eligible_books` skips re-evaluating the book its caller just updated and
    reads a pair index instead of scanning every key, so it could silently
    diverge from the canonical decision it is supposed to share.
    """
    manager = OrderBookManager(clock=lambda: 1_000)
    venues = ("gemini", "coinbase", "binance")
    for exchange in venues:
        manager.apply(
            event(
                kind=EventKind.SNAPSHOT,
                sequence=1,
                bids=[("100", "1")],
                asks=[("101", "1")],
                exchange=exchange,
            )
        )
    # A second pair must not leak into the first pair's result.
    manager.apply(
        event(
            kind=EventKind.SNAPSHOT,
            sequence=1,
            bids=[("10", "1")],
            asks=[("11", "1")],
            exchange="gemini",
            pair="ETH-USD",
        )
    )

    def sweep(pair: str) -> list:
        found = [
            top
            for exchange, book_pair in sorted(manager._books)
            if book_pair == pair
            if (top := manager.eligible_top_of_book(exchange, pair, 1_000)) is not None
        ]
        return found if len(found) >= 2 else []

    assert manager.eligible_books("BTC-USD", 1_000) == sweep("BTC-USD")
    assert len(manager.eligible_books("BTC-USD", 1_000)) == 3
    assert manager.eligible_books("ETH-USD", 1_000) == sweep("ETH-USD") == []

    # Passing the caller's already-validated top must not change the outcome.
    known = manager.top_of_book("coinbase", "BTC-USD")
    assert manager.eligible_books("BTC-USD", 1_000, known=known) == sweep("BTC-USD")

    # A top for another pair must never be substituted in.
    other_pair_top = manager.top_of_book("gemini", "ETH-USD")
    assert manager.eligible_books("BTC-USD", 1_000, known=other_pair_top) == sweep("BTC-USD")

    # Dropping one venue must drop it from both paths identically.
    manager.set_exchange_connected("binance", False)
    assert manager.eligible_books("BTC-USD", 1_000) == sweep("BTC-USD")
    assert len(manager.eligible_books("BTC-USD", 1_000)) == 2
