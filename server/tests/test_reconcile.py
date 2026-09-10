from __future__ import annotations

from decimal import Decimal

import pytest
from arb.adapters.base import ExchangeAdapter
from arb.orderbook import OrderBookManager
from arb.reconcile import SnapshotReconciler
from arb.types import EventKind, MarketEvent, PriceLevel


class ReconcileAdapter(ExchangeAdapter):
    name = "stub"
    ws_url = "wss://example.test"
    snapshot_url = "https://example.test/snapshot"

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return symbol

    async def subscribe(self, websocket: object) -> None:
        return None

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return []

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=trigger_sequence,
            bids=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("102"), size=Decimal("1")),),
        )


def make_manager_with_top(bid: str, ask: str) -> OrderBookManager:
    manager = OrderBookManager()
    manager.apply(
        MarketEvent(
            exchange="stub",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=1,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal(bid), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal(ask), size=Decimal("1")),),
        )
    )
    return manager


@pytest.mark.asyncio
async def test_reconcile_next_handles_mismatch_without_crashing() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    manager = make_manager_with_top("100", "101")
    reconciler = SnapshotReconciler(
        [adapter], manager, [("stub", "BTC-USD")], interval_seconds=0.01
    )
    await reconciler.reconcile_next()


@pytest.mark.asyncio
async def test_mismatch_above_threshold_increments_metric() -> None:
    from arb.metrics import reconcile_mismatches_total

    adapter = ReconcileAdapter(["BTC-USD"])
    # Live book has bid 100/ask 101. Stub snapshot returns bid 101/ask 102 — diff is 1% > 0.5%.
    manager = make_manager_with_top("100", "101")
    counter = reconcile_mismatches_total.labels(exchange="stub", pair="BTC-USD")
    before = counter._value.get()
    reconciler = SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")])
    await reconciler.reconcile_next()
    assert counter._value.get() - before == 1


@pytest.mark.asyncio
async def test_mismatch_within_threshold_does_not_increment_metric() -> None:
    from arb.metrics import reconcile_mismatches_total

    class TightAdapter(ReconcileAdapter):
        async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
            # Live: 100/101. Snapshot: 100.1/101.1 — well under 0.5% diff.
            return MarketEvent(
                exchange=self.name,
                pair=pair,
                kind=EventKind.SNAPSHOT,
                sequence=trigger_sequence,
                timestamp_ns=1,
                bids=(PriceLevel(price=Decimal("100.1"), size=Decimal("1")),),
                asks=(PriceLevel(price=Decimal("101.1"), size=Decimal("1")),),
            )

    adapter = TightAdapter(["BTC-USD"])
    manager = make_manager_with_top("100", "101")
    counter = reconcile_mismatches_total.labels(exchange="stub", pair="BTC-USD")
    before = counter._value.get()
    reconciler = SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")])
    await reconciler.reconcile_next()
    assert counter._value.get() == before


@pytest.mark.asyncio
async def test_missing_snapshot_levels_count_as_a_mismatch() -> None:
    from arb.metrics import reconcile_mismatches_total

    class EmptySnapshotAdapter(ReconcileAdapter):
        async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
            return MarketEvent(
                exchange=self.name,
                pair=pair,
                kind=EventKind.SNAPSHOT,
                sequence=trigger_sequence,
                timestamp_ns=1,
                bids=(),
                asks=(),
            )

    adapter = EmptySnapshotAdapter(["BTC-USD"])
    manager = make_manager_with_top("100", "101")
    counter = reconcile_mismatches_total.labels(exchange="stub", pair="BTC-USD")
    before = counter._value.get()

    await SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")]).reconcile_next()

    assert counter._value.get() - before == 1


@pytest.mark.asyncio
async def test_reconcile_skips_when_book_is_stale() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    fetched: list[str] = []

    async def tracking_fetch(self, pair: str, trigger_sequence: int) -> MarketEvent:
        fetched.append(pair)
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=1,
            bids=(),
            asks=(),
        )

    import types as _types

    adapter.fetch_snapshot = _types.MethodType(tracking_fetch, adapter)
    manager = OrderBookManager()  # No book applied — stale.
    reconciler = SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")])
    await reconciler.reconcile_next()
    assert fetched == []  # Must not call REST when there's nothing to compare.


@pytest.mark.asyncio
async def test_reconcile_round_robins_targets() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    targets_seen: list[str] = []

    async def recording_fetch(self, pair: str, trigger_sequence: int) -> MarketEvent:
        targets_seen.append(pair)
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        )

    import types as _types

    adapter.fetch_snapshot = _types.MethodType(recording_fetch, adapter)

    manager = OrderBookManager()
    for pair in ("BTC-USD", "ETH-USD"):
        manager.apply(
            MarketEvent(
                exchange="stub",
                pair=pair,
                kind=EventKind.SNAPSHOT,
                sequence=1,
                timestamp_ns=1,
                bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
                asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
            )
        )

    reconciler = SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD"), ("stub", "ETH-USD")],
    )
    await reconciler.reconcile_next()
    await reconciler.reconcile_next()
    await reconciler.reconcile_next()  # Wraps back to first.
    assert targets_seen == ["BTC-USD", "ETH-USD", "BTC-USD"]
