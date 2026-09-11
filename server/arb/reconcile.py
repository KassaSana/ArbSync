from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import cycle

import structlog

from arb.adapters.base import ExchangeAdapter
from arb.metrics import (
    reconcile_confirmations_total,
    reconcile_failures_total,
    reconcile_mismatches_total,
    reconcile_recoveries_total,
)
from arb.orderbook import OrderBookManager
from arb.types import BookEligibility, PriceLevel

logger = structlog.get_logger(__name__)

PRICE_MISMATCH_THRESHOLD_PCT = Decimal("0.5")
SIZE_MISMATCH_THRESHOLD_PCT = Decimal("50")
RECONCILE_DEPTH = 10


@dataclass(frozen=True)
class ReconcileTarget:
    exchange: str
    pair: str


@dataclass(frozen=True)
class ReconcileDifference:
    price_pct: Decimal
    size_pct: Decimal

    @property
    def mismatched(self) -> bool:
        return (
            self.price_pct > PRICE_MISMATCH_THRESHOLD_PCT
            or self.size_pct > SIZE_MISMATCH_THRESHOLD_PCT
        )


@dataclass
class _TargetState:
    consecutive_mismatches: int = 0
    cooldown_until: float = 0.0
    recovery_started_at: float | None = None
    recovery_failure_recorded: bool = False


class SnapshotReconciler:
    """Confirm live/REST divergence and hand recovery back to the adapter.

    ``cycle_seconds`` is the nominal duration of a complete pass. Checks are
    spread evenly across that duration so adapters do not receive a REST burst.
    Network request time may make the actual cycle slightly longer.
    """

    def __init__(
        self,
        adapters: list[ExchangeAdapter],
        book_manager: OrderBookManager,
        pairs: list[tuple[str, str]],
        *,
        cycle_seconds: float = 60.0,
        confirmation_count: int = 3,
        cooldown_seconds: float = 300.0,
        on_book_invalidated: Callable[[BookEligibility], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cycle_seconds <= 0:
            raise ValueError("cycle_seconds must be positive")
        if confirmation_count <= 0:
            raise ValueError("confirmation_count must be positive")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self._adapter_by_name = {adapter.name: adapter for adapter in adapters}
        self.book_manager = book_manager
        self.cycle_seconds = cycle_seconds
        self.confirmation_count = confirmation_count
        self.cooldown_seconds = cooldown_seconds
        self._on_book_invalidated = on_book_invalidated
        self._clock = clock
        targets = tuple(ReconcileTarget(exchange, pair) for exchange, pair in pairs)
        self._targets = cycle(targets)
        self._target_count = len(targets)
        self._states = {target: _TargetState() for target in targets}

    @property
    def target_interval_seconds(self) -> float:
        return self.cycle_seconds / max(1, self._target_count)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.target_interval_seconds)
            await self.reconcile_next()

    async def reconcile_next(self) -> None:
        target = next(self._targets, None)
        if target is None:
            return
        adapter = self._adapter_by_name.get(target.exchange)
        if adapter is None:
            return

        state = self._states[target]
        now = self._clock()
        self._observe_recovery(target, state, now)
        if state.recovery_started_at is not None or now < state.cooldown_until:
            state.consecutive_mismatches = 0
            return

        live_levels = self.book_manager.level_snapshot(
            target.exchange, target.pair, limit=RECONCILE_DEPTH
        )
        if live_levels is None:
            state.consecutive_mismatches = 0
            return

        try:
            snapshot = await adapter.fetch_snapshot(target.pair, trigger_sequence=0)
        except Exception as exc:
            state.consecutive_mismatches = 0
            reconcile_failures_total.labels(
                exchange=target.exchange, pair=target.pair, phase="snapshot_fetch"
            ).inc()
            logger.warning(
                "snapshot_reconcile_failed",
                exchange=target.exchange,
                pair=target.pair,
                phase="snapshot_fetch",
                error=repr(exc),
            )
            return

        snapshot_levels = (
            list(snapshot.bids[:RECONCILE_DEPTH]),
            list(snapshot.asks[:RECONCILE_DEPTH]),
        )
        difference = self._difference(live_levels, snapshot_levels)
        if not difference.mismatched:
            state.consecutive_mismatches = 0
            return

        state.consecutive_mismatches += 1
        reconcile_mismatches_total.labels(exchange=target.exchange, pair=target.pair).inc()
        logger.warning(
            "snapshot_reconcile_mismatch",
            exchange=target.exchange,
            pair=target.pair,
            price_mismatch_pct=str(difference.price_pct),
            size_mismatch_pct=str(difference.size_pct),
            consecutive_mismatches=state.consecutive_mismatches,
            confirmation_count=self.confirmation_count,
        )
        if state.consecutive_mismatches < self.confirmation_count:
            return

        await self._start_recovery(target, state, adapter, now)

    def observe_book(self, exchange: str, pair: str) -> None:
        """Record recovery completion when a new eligible snapshot reaches the manager."""
        target = ReconcileTarget(exchange, pair)
        state = self._states.get(target)
        if state is not None:
            self._observe_recovery(target, state, self._clock())

    def _observe_recovery(self, target: ReconcileTarget, state: _TargetState, now: float) -> None:
        started_at = state.recovery_started_at
        if started_at is None:
            return
        if self.book_manager.eligibility(target.exchange, target.pair).eligible:
            state.recovery_started_at = None
            reconcile_recoveries_total.labels(
                exchange=target.exchange, pair=target.pair, outcome="completed"
            ).inc()
            logger.info(
                "snapshot_recovery_completed",
                exchange=target.exchange,
                pair=target.pair,
                duration_seconds=now - started_at,
            )
            return
        if not state.recovery_failure_recorded and now - started_at >= self.cooldown_seconds:
            state.recovery_failure_recorded = True
            reconcile_recoveries_total.labels(
                exchange=target.exchange, pair=target.pair, outcome="failed"
            ).inc()
            logger.error(
                "snapshot_recovery_failed",
                exchange=target.exchange,
                pair=target.pair,
                timeout_seconds=self.cooldown_seconds,
            )

    async def _start_recovery(
        self,
        target: ReconcileTarget,
        state: _TargetState,
        adapter: ExchangeAdapter,
        now: float,
    ) -> None:
        state.consecutive_mismatches = 0
        state.cooldown_until = now + self.cooldown_seconds
        state.recovery_started_at = now
        state.recovery_failure_recorded = False
        reconcile_confirmations_total.labels(exchange=target.exchange, pair=target.pair).inc()
        status = self.book_manager.invalidate(target.exchange, target.pair)
        reconcile_recoveries_total.labels(
            exchange=target.exchange, pair=target.pair, outcome="started"
        ).inc()
        logger.error(
            "snapshot_reconcile_confirmed",
            exchange=target.exchange,
            pair=target.pair,
            confirmation_count=self.confirmation_count,
        )

        if self._on_book_invalidated is not None:
            try:
                await self._on_book_invalidated(status)
            except Exception as exc:
                reconcile_failures_total.labels(
                    exchange=target.exchange,
                    pair=target.pair,
                    phase="invalidation_notification",
                ).inc()
                logger.error(
                    "snapshot_reconcile_failed",
                    exchange=target.exchange,
                    pair=target.pair,
                    phase="invalidation_notification",
                    error=repr(exc),
                )

        try:
            adapter.request_reconnect()
        except Exception as exc:
            state.recovery_failure_recorded = True
            reconcile_failures_total.labels(
                exchange=target.exchange, pair=target.pair, phase="recovery_request"
            ).inc()
            reconcile_recoveries_total.labels(
                exchange=target.exchange, pair=target.pair, outcome="failed"
            ).inc()
            logger.error(
                "snapshot_reconcile_failed",
                exchange=target.exchange,
                pair=target.pair,
                phase="recovery_request",
                error=repr(exc),
            )

    def _difference(
        self,
        live_levels: tuple[list[PriceLevel], list[PriceLevel]],
        snapshot_levels: tuple[list[PriceLevel], list[PriceLevel]],
    ) -> ReconcileDifference:
        bid_price, bid_size = self._side_difference(live_levels[0], snapshot_levels[0])
        ask_price, ask_size = self._side_difference(live_levels[1], snapshot_levels[1])
        return ReconcileDifference(
            price_pct=max(bid_price, ask_price),
            size_pct=max(bid_size, ask_size),
        )

    @staticmethod
    def _side_difference(
        live_levels: list[PriceLevel], snapshot_levels: list[PriceLevel]
    ) -> tuple[Decimal, Decimal]:
        price_pct = Decimal("0")
        if len(live_levels) != len(snapshot_levels):
            price_pct = Decimal("100")
        else:
            for live_level, snapshot_level in zip(live_levels, snapshot_levels):
                baseline = abs(snapshot_level.price) or Decimal("1")
                price_pct = max(
                    price_pct,
                    (abs(live_level.price - snapshot_level.price) / baseline) * Decimal("100"),
                )

        live_size = sum((level.size for level in live_levels), start=Decimal("0"))
        snapshot_size = sum((level.size for level in snapshot_levels), start=Decimal("0"))
        size_baseline = abs(snapshot_size) or Decimal("1")
        size_pct = (abs(live_size - snapshot_size) / size_baseline) * Decimal("100")
        return price_pct, size_pct
