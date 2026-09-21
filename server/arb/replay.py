"""Deterministic, time-faithful replay of capture files through the production path.

Frames are fed back through the live adapters' own sequence state machines,
the real `OrderBookManager`, and the real detector via `process_market_event`,
with no network access. Replaying the same capture twice must produce the same
transitions, lifecycle boundaries, and detector outputs; `digest` makes that
checkable.

Scheduling contract. One priority queue keyed on the recorded monotonic clock
drives everything that can change canonical state:

- WebSocket frames at their recorded receipt time.
- REST snapshot completions at their recorded response time, so a snapshot
  can never affect state before it existed and the updates received while it
  was in flight stay buffered for the adapter's own alignment rules.
- Connection boundaries (capture v2), which clear an exchange's books exactly
  as the live connection-state callback does.
- Age-based eligibility expiry at the deterministic boundary
  `last_receipt + max_age + 1ns`, so a quiet book closes its episodes on time
  rather than when the next frame happens to arrive.
- Depth samples at the sampler's interval.

Entries at the same instant run in the order connection, snapshot completion,
WebSocket frame, expiry, depth sample, then file order. That tie-break is part
of the determinism claim and must not change without a digest version bump.

Provenance. Capture v2 snapshot frames carry which pair, purpose, and
connection generation requested them and when the response landed; replay
matches requests against that and refuses a capture whose recorded recovery
diverges from the replayed one. Legacy captures (v1, or v2 without complete
provenance) fall back to URL order with the record time as completion time,
and the report says so in `timing_fidelity`.

Timestamp rules: the manager clock and every receipt/detection stamp follow
the recorded monotonic and wall-clock timeline. Synthetic instants (expiry,
depth samples) derive wall time from the most recent frame by the same
monotonic offset. Re-parsed `timestamp_ns` values are an exception: adapters
re-stamp wall clock at parse time, so they are carried but excluded from the
digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from arb.adapters import ADAPTER_TYPES
from arb.adapters.base import ExchangeAdapter, SnapshotRequest, request_scoped_resync
from arb.broadcast import LiveBroadcaster
from arb.capture import CaptureFrame, CaptureHeader, read_capture
from arb.detector import ArbitrageDetector
from arb.main import BookEligibilityPublisher, process_market_event
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.pricing import DepthSampler
from arb.types import MarketEvent, OpportunityEpisode

# Offline-only hook: (exchange, pair, wall_ns, mono_ns) after a book may have
# changed. Fired after every transition (accepted or rejected), per expiring
# book, and per book on connection boundaries. Must not mutate books and must
# not affect the report digest.
BookObserver = Callable[[str, str, int, int], None]


class ReplayError(ValueError):
    """A capture cannot be replayed: skew, truncation, or unknown venue."""


REPLAY_OBSERVATION_VERSION = 1
REPLAY_LIFECYCLE_VERSION = 1

SYNC_SNAPSHOT_PURPOSES = frozenset({"initial_sync", "sequence_gap", "scoped_recovery"})

TimingFidelity = Literal["recorded", "legacy_snapshot_order"]

_PRIORITY_CONNECTION = 0
_PRIORITY_SNAPSHOT = 1
_PRIORITY_WS = 2
_PRIORITY_EXPIRY = 3
_PRIORITY_SAMPLE = 4


@dataclass(frozen=True)
class ReplayObservation:
    """Canonical top-of-book state immediately after one replayed event.

    The normalized event levels remain on :class:`ReplayTransition` for protocol
    auditing. Research consumers must use this post-apply observation instead of
    inferring a price from the levels changed by an input delta.
    """

    exchange: str
    pair: str
    best_bid_price: str
    best_ask_price: str
    wall_ns: int
    mono_ns: int
    sequence: int
    eligible: bool
    version: int = REPLAY_OBSERVATION_VERSION


@dataclass(frozen=True)
class ReplayTransition:
    exchange: str
    pair: str
    kind: str
    sequence: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    accepted: bool
    reason: str | None
    resync_requested: bool
    wall_ns: int = 0
    mono_ns: int = 0


@dataclass(frozen=True)
class ReplayLifecycleEvent:
    """One recovery or lifecycle boundary the replay drove on the recorded clock.

    Kinds: `connected`, `disconnected`, `reconnect_requested`, `scoped_resync`,
    `snapshot_requested`, `snapshot_completed`, `snapshot_retry`,
    `snapshot_abandoned`, `book_expired`.
    """

    kind: str
    exchange: str
    pair: str | None
    wall_ns: int
    mono_ns: int
    detail: str = ""
    version: int = REPLAY_LIFECYCLE_VERSION


@dataclass
class ReplayReport:
    transitions: list[ReplayTransition] = field(default_factory=list)
    observations: list[ReplayObservation] = field(default_factory=list)
    opportunities: list[OpportunityEpisode] = field(default_factory=list)
    lifecycle: list[ReplayLifecycleEvent] = field(default_factory=list)
    snapshots_consumed: int = 0
    snapshots_skipped_reconciliation: int = 0
    snapshots_unmatched: int = 0
    frames_skipped_disconnected: int = 0
    legacy_immediate_reconnects: int = 0
    timing_fidelity: TimingFidelity = "recorded"
    digest: str = ""


class _VirtualClock:
    """Manual monotonic clock driven by the recorded timeline."""

    def __init__(self, start_ns: int) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns


@dataclass(frozen=True)
class _ResolvedSnapshot:
    index: int
    frame: CaptureFrame
    completion_mono_ns: int
    completion_wall_ns: int


class _SnapshotLedger:
    """Match adapter snapshot requests to recorded REST responses.

    Strict mode (complete v2 provenance) matches on the recorded pair and
    purpose and refuses a response from another connection generation or one
    that completed before the replayed request was made: both mean the
    replayed recovery is not the recorded one. Legacy mode matches by request
    URL in file order and completes at the record time, or immediately if the
    record predates the request. Reconciliation responses are never sync
    candidates; the reconciler is not replayed.
    """

    def __init__(self, frames: list[CaptureFrame], *, strict: bool) -> None:
        self.strict = strict
        self.consumed = 0
        self._entries: list[tuple[int, CaptureFrame]] = []
        self._consumed: set[int] = set()
        for index, frame in enumerate(frames):
            if frame.kind != "snapshot":
                continue
            if frame.payload is None:
                raise ReplayError(f"capture frame {index} has no snapshot payload")
            self._entries.append((index, frame))

    @staticmethod
    def _is_reconciliation(frame: CaptureFrame) -> bool:
        provenance = frame.snapshot_provenance
        return provenance is not None and provenance.purpose not in SYNC_SNAPSHOT_PURPOSES

    def skipped_reconciliation(self) -> int:
        return sum(1 for _, frame in self._entries if self._is_reconciliation(frame))

    def unmatched(self) -> int:
        return sum(
            1
            for index, frame in self._entries
            if index not in self._consumed and not self._is_reconciliation(frame)
        )

    def resolve(
        self,
        exchange: str,
        url: str,
        request: SnapshotRequest,
        *,
        now_ns: int,
        generation: int,
    ) -> _ResolvedSnapshot:
        for index, frame in self._entries:
            if index in self._consumed or frame.exchange != exchange:
                continue
            if self.strict:
                provenance = frame.snapshot_provenance
                if (
                    provenance is None
                    or provenance.pair != request.pair
                    or provenance.purpose != request.purpose
                ):
                    continue
                if provenance.connection_generation != generation:
                    raise ReplayError(
                        f"snapshot provenance mismatch at capture frame {index}: recorded "
                        f"{request.purpose} for {exchange} {request.pair} belongs to connection "
                        f"generation {provenance.connection_generation}, replay is on "
                        f"generation {generation}"
                    )
                if provenance.response_mono_ns < now_ns:
                    raise ReplayError(
                        f"snapshot provenance mismatch at capture frame {index}: recorded "
                        f"{request.purpose} for {exchange} {request.pair} completed "
                        f"{now_ns - provenance.response_mono_ns} ns before replay requested it"
                    )
                self._consumed.add(index)
                self.consumed += 1
                return _ResolvedSnapshot(
                    index, frame, provenance.response_mono_ns, provenance.response_wall_ns
                )
            if self._is_reconciliation(frame) or (frame.url or "") != url:
                continue
            self._consumed.add(index)
            self.consumed += 1
            completion = max(frame.mono_ns, now_ns)
            return _ResolvedSnapshot(
                index, frame, completion, frame.wall_ns + (completion - frame.mono_ns)
            )
        raise ReplayError(
            f"capture has no snapshot data for {exchange} {request.pair} ({request.purpose}, "
            f"{url}); the recording is missing REST traffic and cannot replay"
        )


@dataclass(frozen=True)
class _PendingSnapshot:
    exchange: str
    request: SnapshotRequest
    event: MarketEvent
    resolved: _ResolvedSnapshot
    generation: int


@dataclass(frozen=True)
class _ExpiryTick:
    version: int


class _CollectingStore:
    """Stand-in persistence that keeps episode events in memory for hashing."""

    def __init__(self) -> None:
        self.opportunities: list[OpportunityEpisode] = []

    async def enqueue(self, episode: OpportunityEpisode) -> bool:
        self.opportunities.append(episode)
        return True


class _FanoutStore(_CollectingStore):
    """Collect opportunities for the report while also persisting them."""

    def __init__(self, stores: list[OpportunityStore]) -> None:
        super().__init__()
        self._stores = stores

    async def enqueue(self, episode: OpportunityEpisode) -> bool:
        await super().enqueue(episode)
        results = [await store.enqueue(episode) for store in self._stores]
        return all(results)

    async def enqueue_all(self, episodes: list[OpportunityEpisode]) -> None:
        for episode in episodes:
            await self.enqueue(episode)


def _build_adapters(header: CaptureHeader) -> dict[str, ExchangeAdapter]:
    known = {adapter_type.name: adapter_type for adapter_type in ADAPTER_TYPES}
    adapters: dict[str, ExchangeAdapter] = {}
    for exchange, symbols in header.exchanges.items():
        adapter_type = known.get(exchange)
        if adapter_type is None:
            raise ReplayError(
                f"capture names unknown exchange {exchange!r}; known exchanges are {sorted(known)}"
            )
        adapters[exchange] = adapter_type(list(symbols))
    return adapters


def _digest(report: ReplayReport) -> str:
    canonical = json.dumps(
        {
            "transitions": [
                [
                    transition.exchange,
                    transition.pair,
                    transition.kind,
                    transition.sequence,
                    [list(level) for level in transition.bids],
                    [list(level) for level in transition.asks],
                    transition.accepted,
                    transition.reason or "",
                    transition.resync_requested,
                ]
                for transition in report.transitions
            ],
            "observations": [
                [
                    observation.version,
                    observation.exchange,
                    observation.pair,
                    observation.best_bid_price,
                    observation.best_ask_price,
                    observation.wall_ns,
                    observation.mono_ns,
                    observation.sequence,
                    observation.eligible,
                ]
                for observation in report.observations
            ],
            "lifecycle": [
                [
                    boundary.version,
                    boundary.kind,
                    boundary.exchange,
                    boundary.pair or "",
                    boundary.wall_ns,
                    boundary.mono_ns,
                    boundary.detail,
                ]
                for boundary in report.lifecycle
            ],
            "opportunities": [
                {key: str(value) for key, value in opportunity.as_payload().items()}
                for opportunity in report.opportunities
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class _Replay:
    """One replay run: the scheduler and the state it threads through."""

    def __init__(
        self,
        header: CaptureHeader,
        frames: list[CaptureFrame],
        *,
        speed: float | None,
        recorded: bool,
        adapters: dict[str, ExchangeAdapter],
        book_manager: OrderBookManager,
        detector: ArbitrageDetector,
        store: _FanoutStore,
        broadcaster: LiveBroadcaster,
        depth_sampler: DepthSampler | None,
        clock: _VirtualClock,
        book_observer: BookObserver | None = None,
    ) -> None:
        self.frames = frames
        self.speed = speed
        self.recorded = recorded
        self.adapters = adapters
        self.manager = book_manager
        self.detector = detector
        self.store = store
        self.broadcaster = broadcaster
        self.depth_sampler = depth_sampler
        self.clock = clock
        self.book_observer = book_observer
        self.report = ReplayReport()
        strict = header.provenance == "complete"
        self.ledger = _SnapshotLedger(frames, strict=strict)
        self.report.timing_fidelity = "recorded" if strict else "legacy_snapshot_order"
        tracked = [
            (adapter.name, pair)
            for adapter in adapters.values()
            for pair in adapter.expected_pairs()
        ]
        self.publisher = BookEligibilityPublisher(
            book_manager, detector, store, broadcaster, tracked
        )
        self._heap: list[tuple[int, int, int, str, Any]] = []
        self._sequence = len(frames)
        self._generation: dict[str, int] = dict.fromkeys(adapters, 0)
        self._down: dict[str, bool] = dict.fromkeys(adapters, False)
        self._has_connection_frames: set[str] = set()
        self._anchor_wall_ns = frames[0].wall_ns if frames else 0
        self._anchor_mono_ns = frames[0].mono_ns if frames else 0
        self._deadlines: dict[tuple[str, str], int] = {}
        self._expiry_version = 0
        self._scheduled_expiry: int | None = None
        self._pending_request: dict[str, SnapshotRequest] = {}
        self._pending_resolution: dict[str, _ResolvedSnapshot] = {}
        for exchange, adapter in adapters.items():
            adapter.client_get_json = self._make_fetch_stub(exchange)  # type: ignore[method-assign]
        self._load_frames()

    # -- setup -------------------------------------------------------------

    def _make_fetch_stub(self, exchange: str) -> Any:
        async def fake_client_get_json(url: str) -> dict[str, Any]:
            request = self._pending_request[exchange]
            resolved = self.ledger.resolve(
                exchange,
                url,
                request,
                now_ns=self.clock.now_ns,
                generation=self._generation[exchange],
            )
            self._pending_resolution[exchange] = resolved
            assert resolved.frame.payload is not None
            return resolved.frame.payload

        return fake_client_get_json

    def _push(
        self, mono_ns: int, priority: int, kind: str, payload: Any, seq: int | None = None
    ) -> None:
        if seq is None:
            self._sequence += 1
            seq = self._sequence
        heapq.heappush(self._heap, (mono_ns, priority, seq, kind, payload))

    def _load_frames(self) -> None:
        previous_mono_ns: int | None = None
        for index, frame in enumerate(self.frames):
            if frame.kind == "snapshot":
                continue
            if previous_mono_ns is not None and frame.mono_ns < previous_mono_ns:
                raise ReplayError(
                    f"capture frame {index} goes backwards in monotonic time; "
                    "the recording is out of order and cannot replay"
                )
            previous_mono_ns = frame.mono_ns
            if frame.exchange not in self.adapters:
                raise ReplayError(f"capture frame {index} names unknown exchange")
            if frame.kind == "connection":
                self._has_connection_frames.add(frame.exchange)
                self._push(frame.mono_ns, _PRIORITY_CONNECTION, "connection", frame, seq=index)
            else:
                if frame.raw is None:
                    raise ReplayError(f"capture frame {index} has no raw message")
                self._push(frame.mono_ns, _PRIORITY_WS, "ws", frame, seq=index)
        if self.depth_sampler is not None and self.frames:
            interval_ns = int(self.depth_sampler.interval_seconds * 1_000_000_000)
            if interval_ns > 0:
                self._push(self.clock.now_ns + interval_ns, _PRIORITY_SAMPLE, "sample", interval_ns)

    # -- helpers -----------------------------------------------------------

    def _now_ns(self) -> int | None:
        return self.clock.now_ns if self.recorded else None

    def _wall_at(self, mono_ns: int) -> int:
        return self._anchor_wall_ns + (mono_ns - self._anchor_mono_ns)

    def _lifecycle(
        self,
        kind: str,
        exchange: str,
        pair: str | None,
        wall_ns: int,
        mono_ns: int,
        detail: str = "",
    ) -> None:
        self.report.lifecycle.append(
            ReplayLifecycleEvent(kind, exchange, pair, wall_ns, mono_ns, detail)
        )

    def _update_deadline(self, exchange: str, pair: str) -> None:
        status = self.manager.eligibility(exchange, pair, self.clock.now_ns)
        key = (exchange, pair)
        if status.eligible and status.age_ns is not None:
            self._deadlines[key] = self.clock.now_ns + (status.max_age_ns - status.age_ns) + 1
        else:
            self._deadlines.pop(key, None)

    def _reschedule_expiry(self) -> None:
        if not self.recorded:
            return
        deadline = min(self._deadlines.values(), default=None)
        if deadline == self._scheduled_expiry:
            return
        self._expiry_version += 1
        self._scheduled_expiry = deadline
        if deadline is not None:
            self._push(deadline, _PRIORITY_EXPIRY, "expiry", _ExpiryTick(self._expiry_version))

    async def _publish_statuses(self, exchange: str, wall_ns: int, mono_ns: int) -> None:
        """Re-evaluate every book of `exchange` on the replay clock and publish it."""
        for book_exchange, pair in self.manager.known_pairs():
            if book_exchange != exchange:
                continue
            status = self.manager.eligibility(exchange, pair, self._now_ns())
            await self.publisher.publish(
                status,
                immediate=True,
                detected_at_ns=wall_ns if self.recorded else None,
                now_monotonic_ns=self._now_ns(),
            )
            self._update_deadline(exchange, pair)
        self._reschedule_expiry()

    async def _handle_reconnect_request(
        self, exchange: str, wall_ns: int, mono_ns: int, detail: str
    ) -> None:
        adapter = self.adapters[exchange]
        self._lifecycle("reconnect_requested", exchange, None, wall_ns, mono_ns, detail)
        if exchange in self._has_connection_frames:
            # Live, the request tears the socket down and the recorded
            # disconnect/reconnect boundaries carry the rest.
            self._down[exchange] = True
            return
        # Legacy capture without connection frames: the best available
        # approximation is the old immediate reset, which leaves the books
        # standing until the fresh snapshots replace them.
        await adapter.reset_state()
        self.report.legacy_immediate_reconnects += 1

    async def _schedule_snapshot(
        self, exchange: str, request: SnapshotRequest, wall_ns: int, mono_ns: int
    ) -> None:
        adapter = self.adapters[exchange]
        self._pending_request[exchange] = request
        try:
            event = await adapter.fetch_snapshot_with_context(
                request.pair, trigger_sequence=0, purpose=request.purpose
            )
        finally:
            self._pending_request.pop(exchange, None)
        resolved = self._pending_resolution.pop(exchange)
        if self.recorded:
            event = replace(event, received_monotonic_ns=resolved.completion_mono_ns)
        pending = _PendingSnapshot(exchange, request, event, resolved, self._generation[exchange])
        self._lifecycle(
            "snapshot_requested",
            exchange,
            request.pair,
            wall_ns,
            mono_ns,
            f"purpose={request.purpose} attempt={request.attempt} frame={resolved.index}",
        )
        self._push(resolved.completion_mono_ns, _PRIORITY_SNAPSHOT, "snapshot", pending)

    async def _process_event(
        self, event: MarketEvent, wall_ns: int, mono_ns: int, *, resync_requested: bool
    ) -> None:
        adapter = self.adapters[event.exchange]
        if self.recorded:
            stamped = (
                replace(event, received_monotonic_ns=self.clock.now_ns)
                if event.received_monotonic_ns is None
                else event
            )
        else:
            stamped = replace(event, received_monotonic_ns=None)
        result = await process_market_event(
            stamped,
            book_manager=self.manager,
            detector=self.detector,
            store=self.store,
            broadcaster=self.broadcaster,
            detected_at_ns=wall_ns if self.recorded else None,
            now_monotonic_ns=self._now_ns(),
            eligibility_publisher=self.publisher,
        )
        status = self.manager.eligibility(event.exchange, event.pair, self._now_ns())
        if result.top_of_book is not None:
            top = result.top_of_book
            self.report.observations.append(
                ReplayObservation(
                    exchange=top.exchange,
                    pair=top.pair,
                    best_bid_price=str(top.best_bid_price),
                    best_ask_price=str(top.best_ask_price),
                    wall_ns=wall_ns,
                    mono_ns=mono_ns,
                    sequence=top.sequence,
                    eligible=status.eligible,
                )
            )
        self.report.transitions.append(
            ReplayTransition(
                exchange=event.exchange,
                pair=event.pair,
                kind=event.kind.value,
                sequence=event.sequence,
                bids=tuple((str(level.price), str(level.size)) for level in event.bids),
                asks=tuple((str(level.price), str(level.size)) for level in event.asks),
                accepted=result.accepted,
                reason=result.reason,
                resync_requested=resync_requested,
                wall_ns=wall_ns,
                mono_ns=mono_ns,
            )
        )
        if result.requires_resync:
            # Mirror `consume_adapter`: scoped recovery when the venue offers
            # it, otherwise the whole connection.
            if request_scoped_resync(adapter, event.pair):
                self._lifecycle(
                    "scoped_resync",
                    event.exchange,
                    event.pair,
                    wall_ns,
                    mono_ns,
                    result.reason or "",
                )
            else:
                adapter.request_reconnect()
        self._update_deadline(event.exchange, event.pair)
        self._reschedule_expiry()
        self._notify_book(event.exchange, event.pair, wall_ns, mono_ns)

    def _notify_book(self, exchange: str, pair: str, wall_ns: int, mono_ns: int) -> None:
        """Invoke the offline book observer without affecting the digest."""
        if self.book_observer is not None:
            self.book_observer(exchange, pair, wall_ns, mono_ns)

    async def _process_ingest(
        self,
        exchange: str,
        events: list[MarketEvent],
        requests: list[SnapshotRequest],
        wall_ns: int,
        mono_ns: int,
        *,
        resync_requested: bool,
    ) -> None:
        adapter = self.adapters[exchange]
        if not adapter._reconnect_requested:
            for request in requests:
                await self._schedule_snapshot(exchange, request, wall_ns, mono_ns)
        for event in events:
            await self._process_event(event, wall_ns, mono_ns, resync_requested=resync_requested)
        if adapter._reconnect_requested:
            await self._handle_reconnect_request(exchange, wall_ns, mono_ns, "adapter")

    # -- dispatch ----------------------------------------------------------

    async def _on_connection(self, frame: CaptureFrame) -> None:
        assert frame.connection is not None
        boundary = frame.connection
        exchange = frame.exchange
        adapter = self.adapters[exchange]
        self._anchor_wall_ns, self._anchor_mono_ns = frame.wall_ns, frame.mono_ns
        if boundary.connected:
            self._generation[exchange] = boundary.generation
            adapter.connection_generation = boundary.generation
            adapter.connected = True
            await adapter.reset_state()
            self._down[exchange] = False
            self.manager.set_exchange_connected(exchange, True)
            self._lifecycle(
                "connected",
                exchange,
                None,
                frame.wall_ns,
                frame.mono_ns,
                f"generation={boundary.generation}",
            )
        else:
            adapter.connected = False
            self._down[exchange] = True
            self.manager.set_exchange_connected(exchange, False)
            self._lifecycle(
                "disconnected",
                exchange,
                None,
                frame.wall_ns,
                frame.mono_ns,
                f"generation={boundary.generation} reason={boundary.reason or ''}",
            )
        await self._publish_statuses(exchange, frame.wall_ns, frame.mono_ns)
        for book_exchange, pair in self.manager.known_pairs():
            if book_exchange == exchange:
                self._notify_book(exchange, pair, frame.wall_ns, frame.mono_ns)

    async def _on_ws(self, index: int, frame: CaptureFrame) -> None:
        exchange = frame.exchange
        adapter = self.adapters[exchange]
        self._anchor_wall_ns, self._anchor_mono_ns = frame.wall_ns, frame.mono_ns
        if self._down[exchange]:
            self.report.frames_skipped_disconnected += 1
            return
        assert frame.raw is not None
        try:
            result = await adapter.ingest_message(frame.raw, frame.mono_ns)
        except ReplayError:
            raise
        except Exception as exc:
            raise ReplayError(f"capture frame {index} failed to parse: {exc}") from exc
        await self._process_ingest(
            exchange,
            result.events,
            result.snapshot_requests,
            frame.wall_ns,
            frame.mono_ns,
            resync_requested=adapter._reconnect_requested,
        )

    async def _on_snapshot(self, pending: _PendingSnapshot) -> None:
        exchange = pending.exchange
        adapter = self.adapters[exchange]
        wall_ns = pending.resolved.completion_wall_ns
        mono_ns = pending.resolved.completion_mono_ns
        self._anchor_wall_ns, self._anchor_mono_ns = wall_ns, mono_ns
        if self._down[exchange] or self._generation[exchange] != pending.generation:
            # Live, the fetch task died with its connection.
            self._lifecycle(
                "snapshot_abandoned",
                exchange,
                pending.request.pair,
                wall_ns,
                mono_ns,
                f"frame={pending.resolved.index}",
            )
            return
        try:
            result = adapter.complete_snapshot(pending.request.pair, pending.event)
        except RuntimeError as exc:
            self._lifecycle(
                "snapshot_completed",
                exchange,
                pending.request.pair,
                wall_ns,
                mono_ns,
                f"frame={pending.resolved.index} outcome={exc}",
            )
            await self._handle_reconnect_request(exchange, wall_ns, mono_ns, str(exc))
            return
        self._lifecycle(
            "snapshot_completed",
            exchange,
            pending.request.pair,
            wall_ns,
            mono_ns,
            f"purpose={pending.request.purpose} attempt={pending.request.attempt} "
            f"generation={pending.generation} frame={pending.resolved.index}",
        )
        for request in result.snapshot_requests:
            self._lifecycle(
                "snapshot_retry",
                exchange,
                request.pair,
                wall_ns,
                mono_ns,
                f"attempt={request.attempt}",
            )
        await self._process_ingest(
            exchange,
            result.events,
            result.snapshot_requests,
            wall_ns,
            mono_ns,
            resync_requested=adapter._reconnect_requested,
        )

    async def _on_expiry(self, tick: _ExpiryTick, mono_ns: int) -> None:
        if tick.version != self._expiry_version:
            return
        self._scheduled_expiry = None
        wall_ns = self._wall_at(mono_ns)
        expiring = [key for key, deadline in self._deadlines.items() if deadline <= mono_ns]
        await self.publisher.scan_once(detected_at_ns=wall_ns, now_monotonic_ns=mono_ns)
        for exchange, pair in expiring:
            self._lifecycle("book_expired", exchange, pair, wall_ns, mono_ns)
        for exchange, pair in list(self._deadlines):
            self._update_deadline(exchange, pair)
        self._reschedule_expiry()
        for exchange, pair in expiring:
            self._notify_book(exchange, pair, wall_ns, mono_ns)

    def _on_sample(self, interval_ns: int, mono_ns: int) -> None:
        assert self.depth_sampler is not None
        self.depth_sampler.sample_all(mono_ns)
        self._push(mono_ns + interval_ns, _PRIORITY_SAMPLE, "sample", interval_ns)

    # -- run ---------------------------------------------------------------

    async def run(self) -> ReplayReport:
        previous_mono_ns: int | None = None
        last_frame_mono_ns = max((frame.mono_ns for frame in self.frames), default=0)
        while self._heap:
            mono_ns, _, seq, kind, payload = heapq.heappop(self._heap)
            if kind in ("sample", "expiry") and mono_ns > last_frame_mono_ns:
                # The capture ended: its last instant is the shutdown boundary,
                # and nothing recorded after it can age or sample a book.
                continue
            if kind == "expiry" and payload.version != self._expiry_version:
                continue
            if (
                self.speed is not None
                and previous_mono_ns is not None
                and mono_ns > previous_mono_ns
            ):
                await asyncio.sleep((mono_ns - previous_mono_ns) / 1_000_000_000 / self.speed)
            previous_mono_ns = mono_ns
            self.clock.now_ns = max(self.clock.now_ns, mono_ns)
            if kind == "connection":
                await self._on_connection(payload)
            elif kind == "ws":
                await self._on_ws(seq, payload)
            elif kind == "snapshot":
                await self._on_snapshot(payload)
            elif kind == "expiry":
                await self._on_expiry(payload, mono_ns)
            else:
                self._on_sample(payload, mono_ns)
        return self.report


async def replay_frames(
    header: CaptureHeader,
    frames: list[CaptureFrame],
    *,
    threshold_pct: Decimal = Decimal("0.1"),
    max_age_seconds: float = 30.0,
    speed: float | None = None,
    timeline: Literal["recorded", "live"] = "recorded",
    book_manager: OrderBookManager | None = None,
    detector: ArbitrageDetector | None = None,
    store: OpportunityStore | None = None,
    broadcaster: LiveBroadcaster | None = None,
    depth_sampler: DepthSampler | None = None,
    book_observer: BookObserver | None = None,
) -> ReplayReport:
    """Replay validated capture frames through the production pipeline.

    Components default to fresh offline instances; serve mode passes the
    pipeline's own so the dashboard reads replayed state. On the `recorded`
    timeline every stamp follows the capture, which is what makes replays
    deterministic; on the `live` timeline stamps follow the replay machine,
    so serve mode streams like a live venue, and age expiry is left to the
    pipeline's own monitor. `speed` paces real sleeping between scheduled
    instants on either timeline; `None` replays as fast as possible. The
    digest is only comparable across runs on the recorded timeline.

    `book_observer` is an offline-only hook called after every transition and
    lifecycle boundary that can change a book. It never affects the report.
    """
    if speed is not None and speed <= 0:
        raise ReplayError(f"replay speed must be greater than zero; got {speed!r}")
    adapters = _build_adapters(header)
    clock = _VirtualClock(frames[0].mono_ns if frames else 0)
    active_manager = (
        book_manager
        if book_manager is not None
        else OrderBookManager(max_age_seconds=max_age_seconds, clock=clock)
    )
    active_detector = (
        detector if detector is not None else ArbitrageDetector(threshold_pct=threshold_pct)
    )
    active_broadcaster = broadcaster if broadcaster is not None else LiveBroadcaster()
    active_store = _FanoutStore([store] if store is not None else [])
    recorded = timeline == "recorded"
    try:
        replay = _Replay(
            header,
            frames,
            speed=speed,
            recorded=recorded,
            adapters=adapters,
            book_manager=active_manager,
            detector=active_detector,
            store=active_store,
            broadcaster=active_broadcaster,
            depth_sampler=depth_sampler,
            clock=clock,
            book_observer=book_observer,
        )
        report = await replay.run()
        # The capture ended with these spreads still standing. Closing them
        # as `shutdown` at the last recorded instant keeps the report a full
        # accounting and the digest deterministic.
        if frames and detector is None:
            last_wall_ns = max(frame.wall_ns for frame in frames)
            await active_store.enqueue_all(
                active_detector.close_all(
                    last_wall_ns if recorded else time.time_ns(),
                    clock.now_ns if recorded else None,
                )
            )
        report.opportunities.extend(active_store.opportunities)
        report.snapshots_consumed = replay.ledger.consumed
        report.snapshots_skipped_reconciliation = replay.ledger.skipped_reconciliation()
        report.snapshots_unmatched = replay.ledger.unmatched()
        report.digest = _digest(report)
        return report
    finally:
        await active_broadcaster.aclose()


async def replay_file(
    path: str | Path,
    *,
    threshold_pct: Decimal = Decimal("0.1"),
    max_age_seconds: float = 30.0,
    speed: float | None = None,
    timeline: Literal["recorded", "live"] = "recorded",
    book_observer: BookObserver | None = None,
) -> ReplayReport:
    """Read, validate, and replay one capture file."""
    header, frames = read_capture(path)
    return await replay_frames(
        header,
        frames,
        threshold_pct=threshold_pct,
        max_age_seconds=max_age_seconds,
        speed=speed,
        timeline=timeline,
        book_observer=book_observer,
    )


__all__ = [
    "REPLAY_LIFECYCLE_VERSION",
    "REPLAY_OBSERVATION_VERSION",
    "SYNC_SNAPSHOT_PURPOSES",
    "BookObserver",
    "ReplayError",
    "ReplayLifecycleEvent",
    "ReplayObservation",
    "ReplayReport",
    "ReplayTransition",
    "replay_file",
    "replay_frames",
]
