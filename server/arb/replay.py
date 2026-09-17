"""Deterministic replay of capture files through the real production path.

Frames are fed back through the live adapters' own parsing and sequence
state, the real `OrderBookManager`, and the real detector via
`process_market_event`, with no network access: REST snapshot payloads come
from the capture's snapshot frames. Replaying the same capture twice must
produce the same transitions and detector outputs; `digest` makes that
checkable.

Timestamp rules: the manager clock and every receipt/detection stamp follow
the recorded monotonic and wall-clock timeline, so book-age eligibility
behaves as it did live. Re-parsed `timestamp_ns` values are an exception:
adapters re-stamp wall clock at parse time, so they are carried but excluded
from the digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb.adapters import ADAPTER_TYPES
from arb.adapters.base import ExchangeAdapter
from arb.broadcast import LiveBroadcaster
from arb.capture import CaptureFrame, CaptureHeader, read_capture
from arb.detector import ArbitrageDetector
from arb.main import process_market_event
from arb.orderbook import OrderBookManager
from arb.types import ArbitrageOpportunity


class ReplayError(ValueError):
    """A capture cannot be replayed: skew, truncation, or unknown venue."""


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


@dataclass
class ReplayReport:
    transitions: list[ReplayTransition] = field(default_factory=list)
    opportunities: list[ArbitrageOpportunity] = field(default_factory=list)
    snapshots_consumed: int = 0
    digest: str = ""


class _VirtualClock:
    """Manual monotonic clock driven by the recorded timeline."""

    def __init__(self, start_ns: int) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns


class _SnapshotStub:
    """Serve recorded REST snapshot payloads to `fetch_snapshot` in order.

    The reconciler also fetches snapshots during a live capture, so its
    frames share this queue. Ordering is preserved, and Binance's own
    align-and-retry logic skips entries that predate the buffered updates,
    exactly as it would against live REST responses. An empty queue means
    the capture genuinely lacks the data, which is an error, never a
    live fetch.
    """

    def __init__(self) -> None:
        self._payloads: dict[str, deque[dict[str, Any]]] = {}
        self.consumed = 0

    def add(self, exchange: str, payload: dict[str, Any]) -> None:
        self._payloads.setdefault(exchange, deque()).append(payload)

    def pop(self, exchange: str) -> dict[str, Any]:
        queue = self._payloads.get(exchange)
        if not queue:
            raise ReplayError(
                f"capture has no snapshot data for {exchange}; "
                "the recording is missing REST traffic and cannot replay"
            )
        self.consumed += 1
        return queue.popleft()


class _CollectingStore:
    """Stand-in persistence that keeps opportunities in memory for hashing."""

    def __init__(self) -> None:
        self.opportunities: list[ArbitrageOpportunity] = []

    async def enqueue(self, opportunity: ArbitrageOpportunity) -> bool:
        self.opportunities.append(opportunity)
        return True


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
            "opportunities": [
                {key: str(value) for key, value in opportunity.as_payload().items()}
                for opportunity in report.opportunities
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def replay_frames(
    header: CaptureHeader,
    frames: list[CaptureFrame],
    *,
    threshold_pct: Decimal = Decimal("0.1"),
    max_age_seconds: float = 30.0,
    speed: float | None = None,
) -> ReplayReport:
    """Replay validated capture frames through the production pipeline."""
    if speed is not None and speed <= 0:
        raise ReplayError(f"replay speed must be greater than zero; got {speed!r}")
    adapters = _build_adapters(header)
    stub = _SnapshotStub()
    for exchange, adapter in adapters.items():
        original = adapter

        async def fake_client_get_json(
            url: str, _adapter: ExchangeAdapter = original
        ) -> dict[str, Any]:
            return stub.pop(_adapter.name)

        adapter.client_get_json = fake_client_get_json  # type: ignore[method-assign]
    clock = _VirtualClock(frames[0].mono_ns if frames else 0)
    manager = OrderBookManager(max_age_seconds=max_age_seconds, clock=clock)
    detector = ArbitrageDetector(threshold_pct=threshold_pct)
    store = _CollectingStore()
    broadcaster = LiveBroadcaster()
    report = ReplayReport()
    try:
        # REST responses arrive concurrently with the messages that trigger
        # them, so preload them in recorded order; the adapters consume them
        # through the same align-and-retry logic as live responses.
        for index, frame in enumerate(frames):
            if frame.kind != "snapshot":
                continue
            if frame.payload is None:
                raise ReplayError(f"capture frame {index} has no snapshot payload")
            stub.add(frame.exchange, frame.payload)
        previous_mono_ns: int | None = None
        for index, frame in enumerate(frames):
            if frame.kind == "snapshot":
                continue
            if frame.mono_ns < clock.now_ns:
                raise ReplayError(
                    f"capture frame {index} goes backwards in monotonic time; "
                    "the recording is out of order and cannot replay"
                )
            if (
                speed is not None
                and previous_mono_ns is not None
                and frame.mono_ns > previous_mono_ns
            ):
                await asyncio.sleep((frame.mono_ns - previous_mono_ns) / 1_000_000_000 / speed)
            previous_mono_ns = frame.mono_ns
            clock.now_ns = frame.mono_ns
            frame_adapter = adapters.get(frame.exchange)
            if frame_adapter is None:
                raise ReplayError(f"capture frame {index} names unknown exchange")
            if frame.raw is None:
                raise ReplayError(f"capture frame {index} has no raw message")
            try:
                events = await frame_adapter.parse_message(frame.raw)
            except Exception as exc:
                raise ReplayError(f"capture frame {index} failed to parse: {exc}") from exc
            resync_requested = frame_adapter._reconnect_requested
            if resync_requested:
                # Mirror the reconnect in `connect`: drop per-connection
                # state so the next message re-synchronizes like live.
                await frame_adapter.reset_state()
            for event in events:
                if event.received_monotonic_ns != clock.now_ns:
                    stamped = replace(event, received_monotonic_ns=clock.now_ns)
                else:
                    stamped = event
                result = await process_market_event(
                    stamped,
                    book_manager=manager,
                    detector=detector,
                    store=store,  # type: ignore[arg-type]
                    broadcaster=broadcaster,
                    detected_at_ns=frame.wall_ns,
                    now_monotonic_ns=clock.now_ns,
                )
                report.transitions.append(
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
                    )
                )
        report.opportunities.extend(store.opportunities)
        report.snapshots_consumed = stub.consumed
        report.digest = _digest(report)
        return report
    finally:
        await broadcaster.aclose()


async def replay_file(
    path: str | Path,
    *,
    threshold_pct: Decimal = Decimal("0.1"),
    max_age_seconds: float = 30.0,
    speed: float | None = None,
) -> ReplayReport:
    """Read, validate, and replay one capture file."""
    header, frames = read_capture(path)
    return await replay_frames(
        header,
        frames,
        threshold_pct=threshold_pct,
        max_age_seconds=max_age_seconds,
        speed=speed,
    )


__all__ = [
    "ReplayError",
    "ReplayReport",
    "ReplayTransition",
    "replay_file",
    "replay_frames",
]
