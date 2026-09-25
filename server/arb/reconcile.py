from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import cycle

import structlog

from arb.adapters.base import ExchangeAdapter, request_scoped_resync
from arb.metrics import (
    reconcile_confirmations_total,
    reconcile_evidence_total,
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
class SideDifference:
    price_pct: Decimal
    signed_size_pct: Decimal


@dataclass(frozen=True)
class ReconcileDifference:
    bids: SideDifference
    asks: SideDifference


@dataclass(frozen=True)
class ReconcileEvidence:
    price_pct: Decimal
    size_pct: Decimal
    size_signature: frozenset[str]

    @property
    def mismatched(self) -> bool:
        return self.price_pct > 0 or self.size_pct > 0

    @property
    def kind(self) -> str:
        if self.price_pct > 0 and self.size_pct > 0:
            return "price_and_size"
        return "price" if self.price_pct > 0 else "size"


@dataclass
class _TargetState:
    consecutive_price_mismatches: int = 0
    consecutive_size_mismatches: int = 0
    size_signature: frozenset[str] = field(default_factory=frozenset)
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
        size_confirmation_count: int = 5,
        cooldown_seconds: float = 300.0,
        on_book_invalidated: Callable[[BookEligibility], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cycle_seconds <= 0:
            raise ValueError("cycle_seconds must be positive")
        if confirmation_count <= 0:
            raise ValueError("confirmation_count must be positive")
        if size_confirmation_count <= 0:
            raise ValueError("size_confirmation_count must be positive")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self._adapter_by_name = {adapter.name: adapter for adapter in adapters}
        self.book_manager = book_manager
        self.cycle_seconds = cycle_seconds
        self.confirmation_count = confirmation_count
        self.size_confirmation_count = size_confirmation_count
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
            self._reset_mismatches(state)
            return

        live_levels = self.book_manager.level_snapshot(
            target.exchange, target.pair, limit=RECONCILE_DEPTH
        )
        if live_levels is None:
            self._reset_mismatches(state)
            return

        try:
            snapshot = await adapter.fetch_snapshot_with_context(
                target.pair, trigger_sequence=0, purpose="reconciliation"
            )
        except Exception as exc:
            self._reset_mismatches(state)
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
        current_levels = self.book_manager.level_snapshot(
            target.exchange, target.pair, limit=RECONCILE_DEPTH
        )
        if current_levels is None:
            self._reset_mismatches(state)
            return
        evidence = self._corroborate(
            self._difference(live_levels, snapshot_levels),
            self._difference(current_levels, snapshot_levels),
        )
        if not evidence.mismatched:
            self._reset_mismatches(state)
            return

        consecutive_mismatches, required_confirmations = self._record_mismatch(state, evidence)
        reconcile_mismatches_total.labels(exchange=target.exchange, pair=target.pair).inc()
        reconcile_evidence_total.labels(
            exchange=target.exchange, pair=target.pair, kind=evidence.kind
        ).inc()
        logger.warning(
            "snapshot_reconcile_mismatch",
            exchange=target.exchange,
            pair=target.pair,
            mismatch_kind=evidence.kind,
            price_mismatch_pct=str(evidence.price_pct),
            size_mismatch_pct=str(evidence.size_pct),
            size_signature=sorted(evidence.size_signature),
            consecutive_mismatches=consecutive_mismatches,
            confirmation_count=required_confirmations,
        )
        if consecutive_mismatches < required_confirmations:
            return

        await self._start_recovery(
            target, state, adapter, now, evidence.kind, required_confirmations
        )

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
        mismatch_kind: str,
        confirmation_count: int,
    ) -> None:
        self._reset_mismatches(state)
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
            mismatch_kind=mismatch_kind,
            confirmation_count=confirmation_count,
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
            if not request_scoped_resync(adapter, target.pair):
                adapter.request_reconnect("confirmed_drift")
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
        return ReconcileDifference(
            bids=self._side_difference(live_levels[0], snapshot_levels[0]),
            asks=self._side_difference(live_levels[1], snapshot_levels[1]),
        )

    @staticmethod
    def _side_difference(
        live_levels: list[PriceLevel], snapshot_levels: list[PriceLevel]
    ) -> SideDifference:
        if not live_levels or not snapshot_levels:
            return SideDifference(
                Decimal("100") if bool(live_levels) != bool(snapshot_levels) else Decimal("0"),
                Decimal("0"),
            )

        # The best price matters even when the two top-10 windows do not overlap.
        best_baseline = abs(snapshot_levels[0].price) or Decimal("1")
        price_pct = (
            abs(live_levels[0].price - snapshot_levels[0].price) / best_baseline
        ) * Decimal("100")

        # A thin side may contain fewer than ten levels. Compare only prices
        # inside the range both top-10 windows cover; a level outside either
        # window says nothing about whether the other book contains it.
        low = max(
            min(level.price for level in live_levels), min(level.price for level in snapshot_levels)
        )
        high = min(
            max(level.price for level in live_levels), max(level.price for level in snapshot_levels)
        )
        live_shared = {
            level.price: level.size for level in live_levels if low <= level.price <= high
        }
        snapshot_shared = {
            level.price: level.size for level in snapshot_levels if low <= level.price <= high
        }
        # A single extra or missing level should contribute its distance to
        # the nearest price, not the positional shift of every later level.
        # The existing 0.5% threshold then filters ordinary dense-book churn.
        for price in live_shared.keys() - snapshot_shared.keys():
            nearest = min(snapshot_levels, key=lambda level: abs(level.price - price)).price
            price_pct = max(price_pct, abs(price - nearest) / (abs(nearest) or 1) * 100)
        for price in snapshot_shared.keys() - live_shared.keys():
            nearest = min(live_levels, key=lambda level: abs(level.price - price)).price
            price_pct = max(price_pct, abs(price - nearest) / (abs(price) or 1) * 100)

        live_size = sum(live_shared.values(), start=Decimal("0"))
        snapshot_size = sum(snapshot_shared.values(), start=Decimal("0"))
        size_baseline = abs(snapshot_size) or Decimal("1")
        signed_size_pct = ((live_size - snapshot_size) / size_baseline) * Decimal("100")
        return SideDifference(price_pct, signed_size_pct)

    @staticmethod
    def _corroborate(before: ReconcileDifference, after: ReconcileDifference) -> ReconcileEvidence:
        price_values: list[Decimal] = []
        size_values: list[Decimal] = []
        size_signature: set[str] = set()
        for side, before_side, after_side in (
            ("bids", before.bids, after.bids),
            ("asks", before.asks, after.asks),
        ):
            if (
                before_side.price_pct > PRICE_MISMATCH_THRESHOLD_PCT
                and after_side.price_pct > PRICE_MISMATCH_THRESHOLD_PCT
            ):
                price_values.append(min(before_side.price_pct, after_side.price_pct))
            before_size = before_side.signed_size_pct
            after_size = after_side.signed_size_pct
            if (
                abs(before_size) > SIZE_MISMATCH_THRESHOLD_PCT
                and abs(after_size) > SIZE_MISMATCH_THRESHOLD_PCT
                and (before_size > 0) == (after_size > 0)
            ):
                direction = "live_above" if before_size > 0 else "live_below"
                size_signature.add(f"{side}:{direction}")
                size_values.append(min(abs(before_size), abs(after_size)))
        return ReconcileEvidence(
            price_pct=max(price_values, default=Decimal("0")),
            size_pct=max(size_values, default=Decimal("0")),
            size_signature=frozenset(size_signature),
        )

    def _record_mismatch(self, state: _TargetState, evidence: ReconcileEvidence) -> tuple[int, int]:
        """Count this mismatch and return (consecutive count, confirmations required).

        A price mismatch is counted on its own streak and resets the size streak.
        A size-only mismatch continues its streak only while the same set of levels
        keeps disagreeing; a different signature starts a new streak of one.
        """
        if evidence.price_pct > 0:
            state.consecutive_price_mismatches += 1
            state.consecutive_size_mismatches = 0
            state.size_signature = frozenset()
            return state.consecutive_price_mismatches, self.confirmation_count

        state.consecutive_price_mismatches = 0
        if evidence.size_signature == state.size_signature:
            state.consecutive_size_mismatches += 1
        else:
            state.consecutive_size_mismatches = 1
            state.size_signature = evidence.size_signature
        return state.consecutive_size_mismatches, self.size_confirmation_count

    @staticmethod
    def _reset_mismatches(state: _TargetState) -> None:
        state.consecutive_price_mismatches = 0
        state.consecutive_size_mismatches = 0
        state.size_signature = frozenset()
