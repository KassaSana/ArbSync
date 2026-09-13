from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from arb.adapters.base import ExchangeAdapter
from arb.metrics import (
    reconcile_confirmations_total,
    reconcile_failures_total,
    reconcile_mismatches_total,
    reconcile_recoveries_total,
)
from arb.orderbook import OrderBookManager
from arb.reconcile import SnapshotReconciler
from arb.types import BookEligibility, EventKind, MarketEvent, PriceLevel


class ReconcileAdapter(ExchangeAdapter):
    name = "stub"
    ws_url = "wss://example.test"
    snapshot_url = "https://example.test/snapshot"

    def __init__(self, pairs: list[str]) -> None:
        super().__init__(pairs)
        self.bid = Decimal("101")
        self.ask = Decimal("102")
        self.bid_size = Decimal("1")
        self.ask_size = Decimal("1")
        self.empty_snapshot = False
        self.fetch_error: Exception | None = None
        self.fetches: list[str] = []
        self.reconnect_requests = 0
        self.fetch_hook: Callable[[], None] | None = None

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return symbol

    async def subscribe(self, websocket: object) -> None:
        return None

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return []

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        self.fetches.append(pair)
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.empty_snapshot:
            return MarketEvent(
                exchange=self.name,
                pair=pair,
                kind=EventKind.SNAPSHOT,
                sequence=trigger_sequence,
                timestamp_ns=trigger_sequence,
            )
        snapshot = MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=trigger_sequence,
            bids=(PriceLevel(price=self.bid, size=self.bid_size),),
            asks=(PriceLevel(price=self.ask, size=self.ask_size),),
        )
        if self.fetch_hook is not None:
            self.fetch_hook()
        return snapshot

    def request_reconnect(self) -> None:
        self.reconnect_requests += 1
        super().request_reconnect()


def apply_book(
    manager: OrderBookManager,
    pair: str = "BTC-USD",
    *,
    bid: str = "100",
    ask: str = "101",
    bid_size: str = "1",
    ask_size: str = "1",
    sequence: int = 1,
) -> None:
    result = manager.apply(
        MarketEvent(
            exchange="stub",
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=sequence,
            timestamp_ns=sequence,
            bids=(PriceLevel(price=Decimal(bid), size=Decimal(bid_size)),),
            asks=(PriceLevel(price=Decimal(ask), size=Decimal(ask_size)),),
        )
    )
    assert result.accepted is True


def metric_value(metric: object, **labels: str) -> float:
    return metric.labels(**labels)._value.get()  # type: ignore[attr-defined, no-any-return]


@pytest.mark.asyncio
async def test_transient_mismatch_resets_confirmation_streak() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    manager = OrderBookManager()
    apply_book(manager)
    reconciler = SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")], confirmation_count=2)

    await reconciler.reconcile_next()
    adapter.bid = Decimal("100")
    adapter.ask = Decimal("101")
    await reconciler.reconcile_next()
    adapter.bid = Decimal("101")
    adapter.ask = Decimal("102")
    await reconciler.reconcile_next()

    assert manager.eligibility("stub", "BTC-USD").eligible is True
    assert adapter.reconnect_requests == 0


@pytest.mark.asyncio
async def test_persistent_mismatch_invalidates_before_requesting_recovery() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    manager = OrderBookManager()
    apply_book(manager)
    notifications: list[bool] = []

    async def record_invalidation(status: BookEligibility) -> None:
        notifications.append(status.eligible)

    confirmed_before = metric_value(reconcile_confirmations_total, exchange="stub", pair="BTC-USD")
    started_before = metric_value(
        reconcile_recoveries_total,
        exchange="stub",
        pair="BTC-USD",
        outcome="started",
    )
    reconciler = SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD")],
        confirmation_count=3,
        on_book_invalidated=record_invalidation,
    )

    await reconciler.reconcile_next()
    await reconciler.reconcile_next()
    assert manager.eligibility("stub", "BTC-USD").eligible is True
    await reconciler.reconcile_next()

    assert manager.eligibility("stub", "BTC-USD").eligible is False
    assert notifications == [False]
    assert adapter.reconnect_requests == 1
    assert (
        metric_value(reconcile_confirmations_total, exchange="stub", pair="BTC-USD")
        - confirmed_before
        == 1
    )
    assert (
        metric_value(
            reconcile_recoveries_total,
            exchange="stub",
            pair="BTC-USD",
            outcome="started",
        )
        - started_before
        == 1
    )


@pytest.mark.asyncio
async def test_aggregate_size_divergence_can_confirm_recovery() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.bid = Decimal("100")
    adapter.ask = Decimal("101")
    adapter.bid_size = Decimal("3")
    manager = OrderBookManager()
    apply_book(manager)
    reconciler = SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD")],
        confirmation_count=1,
        size_confirmation_count=1,
    )

    await reconciler.reconcile_next()

    assert adapter.reconnect_requests == 1
    assert manager.eligibility("stub", "BTC-USD").eligible is False


@pytest.mark.asyncio
async def test_market_movement_during_snapshot_fetch_is_not_corroborated() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.bid = Decimal("102")
    adapter.ask = Decimal("103")
    adapter.bid_size = Decimal("3")
    adapter.ask_size = Decimal("3")
    manager = OrderBookManager()
    apply_book(manager)
    adapter.fetch_hook = lambda: apply_book(
        manager,
        bid="102",
        ask="103",
        bid_size="3",
        ask_size="3",
        sequence=2,
    )

    await SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD")],
        confirmation_count=1,
        size_confirmation_count=1,
    ).reconcile_next()

    assert adapter.reconnect_requests == 0
    assert manager.eligibility("stub", "BTC-USD").eligible is True


@pytest.mark.asyncio
async def test_size_only_recovery_uses_longer_confirmation_count() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.bid = Decimal("100")
    adapter.ask = Decimal("101")
    adapter.bid_size = Decimal("3")
    adapter.ask_size = Decimal("3")
    manager = OrderBookManager()
    apply_book(manager)
    reconciler = SnapshotReconciler(
        [adapter], manager, [("stub", "BTC-USD")], size_confirmation_count=5
    )

    for _ in range(4):
        await reconciler.reconcile_next()
    assert adapter.reconnect_requests == 0
    await reconciler.reconcile_next()

    assert adapter.reconnect_requests == 1
    assert manager.eligibility("stub", "BTC-USD").eligible is False


@pytest.mark.asyncio
async def test_size_direction_change_resets_confirmation_streak() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.bid = Decimal("100")
    adapter.ask = Decimal("101")
    adapter.bid_size = Decimal("3")
    adapter.ask_size = Decimal("3")
    manager = OrderBookManager()
    apply_book(manager)
    reconciler = SnapshotReconciler(
        [adapter], manager, [("stub", "BTC-USD")], size_confirmation_count=2
    )

    await reconciler.reconcile_next()
    adapter.bid_size = Decimal("0.25")
    adapter.ask_size = Decimal("0.25")
    await reconciler.reconcile_next()

    assert adapter.reconnect_requests == 0
    assert manager.eligibility("stub", "BTC-USD").eligible is True


@pytest.mark.asyncio
async def test_price_and_size_changes_within_tolerance_do_not_mismatch() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.bid = Decimal("100.1")
    adapter.ask = Decimal("101.1")
    adapter.bid_size = Decimal("1.4")
    adapter.ask_size = Decimal("1.4")
    manager = OrderBookManager()
    apply_book(manager)
    before = metric_value(reconcile_mismatches_total, exchange="stub", pair="BTC-USD")

    await SnapshotReconciler(
        [adapter], manager, [("stub", "BTC-USD")], confirmation_count=1
    ).reconcile_next()

    assert metric_value(reconcile_mismatches_total, exchange="stub", pair="BTC-USD") == before
    assert manager.eligibility("stub", "BTC-USD").eligible is True


@pytest.mark.asyncio
async def test_missing_snapshot_levels_count_as_a_mismatch() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.empty_snapshot = True
    manager = OrderBookManager()
    apply_book(manager)
    before = metric_value(reconcile_mismatches_total, exchange="stub", pair="BTC-USD")

    await SnapshotReconciler(
        [adapter], manager, [("stub", "BTC-USD")], confirmation_count=2
    ).reconcile_next()

    assert metric_value(reconcile_mismatches_total, exchange="stub", pair="BTC-USD") - before == 1
    assert manager.eligibility("stub", "BTC-USD").eligible is True


@pytest.mark.asyncio
async def test_cooldown_suppresses_repeat_recovery_until_fresh_confirmations() -> None:
    now = [0.0]
    adapter = ReconcileAdapter(["BTC-USD"])
    manager = OrderBookManager()
    apply_book(manager)
    reconciler = SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD")],
        confirmation_count=1,
        cooldown_seconds=100,
        clock=lambda: now[0],
    )

    await reconciler.reconcile_next()
    apply_book(manager, sequence=2)
    reconciler.observe_book("stub", "BTC-USD")
    fetches_after_recovery = len(adapter.fetches)

    now[0] = 99
    await reconciler.reconcile_next()
    assert len(adapter.fetches) == fetches_after_recovery
    assert adapter.reconnect_requests == 1

    now[0] = 100
    await reconciler.reconcile_next()
    assert adapter.reconnect_requests == 2


@pytest.mark.asyncio
async def test_recovery_completion_and_timeout_are_recorded() -> None:
    now = [0.0]
    adapter = ReconcileAdapter(["BTC-USD"])
    manager = OrderBookManager()
    apply_book(manager)
    completed_before = metric_value(
        reconcile_recoveries_total,
        exchange="stub",
        pair="BTC-USD",
        outcome="completed",
    )
    failed_before = metric_value(
        reconcile_recoveries_total,
        exchange="stub",
        pair="BTC-USD",
        outcome="failed",
    )
    reconciler = SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD")],
        confirmation_count=1,
        cooldown_seconds=10,
        clock=lambda: now[0],
    )

    await reconciler.reconcile_next()
    apply_book(manager, sequence=2)
    now[0] = 2
    reconciler.observe_book("stub", "BTC-USD")
    assert (
        metric_value(
            reconcile_recoveries_total,
            exchange="stub",
            pair="BTC-USD",
            outcome="completed",
        )
        - completed_before
        == 1
    )

    now[0] = 10
    await reconciler.reconcile_next()
    now[0] = 20
    reconciler.observe_book("stub", "BTC-USD")
    assert (
        metric_value(
            reconcile_recoveries_total,
            exchange="stub",
            pair="BTC-USD",
            outcome="failed",
        )
        - failed_before
        == 1
    )


@pytest.mark.asyncio
async def test_snapshot_fetch_failure_is_counted_without_stopping_reconciler() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    adapter.fetch_error = OSError("REST unavailable")
    manager = OrderBookManager()
    apply_book(manager)
    before = metric_value(
        reconcile_failures_total,
        exchange="stub",
        pair="BTC-USD",
        phase="snapshot_fetch",
    )
    reconciler = SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")])

    await reconciler.reconcile_next()
    adapter.fetch_error = None
    await reconciler.reconcile_next()

    assert (
        metric_value(
            reconcile_failures_total,
            exchange="stub",
            pair="BTC-USD",
            phase="snapshot_fetch",
        )
        - before
        == 1
    )
    assert manager.eligibility("stub", "BTC-USD").eligible is True


@pytest.mark.asyncio
async def test_reconcile_skips_when_book_is_stale() -> None:
    adapter = ReconcileAdapter(["BTC-USD"])
    manager = OrderBookManager()

    await SnapshotReconciler([adapter], manager, [("stub", "BTC-USD")]).reconcile_next()

    assert adapter.fetches == []


@pytest.mark.asyncio
async def test_cycle_is_spread_across_round_robin_targets() -> None:
    adapter = ReconcileAdapter(["BTC-USD", "ETH-USD"])
    adapter.bid = Decimal("100")
    adapter.ask = Decimal("101")
    manager = OrderBookManager()
    apply_book(manager, "BTC-USD")
    apply_book(manager, "ETH-USD")
    reconciler = SnapshotReconciler(
        [adapter],
        manager,
        [("stub", "BTC-USD"), ("stub", "ETH-USD")],
        cycle_seconds=60,
    )

    await reconciler.reconcile_next()
    await reconciler.reconcile_next()
    await reconciler.reconcile_next()

    assert reconciler.target_interval_seconds == 30
    assert adapter.fetches == ["BTC-USD", "ETH-USD", "BTC-USD"]
