from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

import structlog

from arb.adapters.base import ExchangeAdapter, IngestResult, parse_level_mappings, parse_levels
from arb.metrics import adapter_pair_resyncs_total, book_verifications_total
from arb.types import EventKind, MarketEvent, PriceLevel

logger = structlog.get_logger(__name__)

# A pair resync that has not produced a fresh snapshot within this long falls
# back to a full reconnect. Measured round trips are well under a second.
PAIR_RESYNC_TIMEOUT_NS = 10_000_000_000
# More resyncs of one pair than this within the window (for example a sparse
# book whose snapshots keep arriving crossed) escalate to a full reconnect, so
# connect()'s exponential backoff paces the retries instead of a tight loop.
PAIR_RESYNC_LIMIT = 3
PAIR_RESYNC_WINDOW_NS = 60_000_000_000

# Gemini's partial book stream: a top-N snapshot about once a second whose
# `lastUpdateId` is in the same id space as `@depth`, so the incremental book
# can be checked against Gemini's own levels at exactly the same update.
VERIFY_DEPTH = 20
# Once a pair has received a partial snapshot on a connection, going this long
# (frame time) without one verifying is a stalled check, not a quiet market:
# Gemini sends the partial every second even when nothing changes. The venue
# is reconnected rather than left trusted without verification.
VERIFY_STALL_NS = 30_000_000_000

_UNSUBSCRIBING = "unsubscribing"
_AWAITING_SNAPSHOT = "awaiting_snapshot"


def normalize_gemini_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    return s


def _resync_request_id(symbol: str, action: str, number: int) -> str:
    # Gemini echoes string ids verbatim, so every acknowledgement names the
    # pair and step it completes. Replay reads the same acknowledgements from
    # the capture and walks the same state machine without the outbound frames.
    return f"resync:{symbol}:{action}:{number}"


def _parse_resync_request_id(request_id: object) -> tuple[str, str] | None:
    if not isinstance(request_id, str):
        return None
    parts = request_id.split(":")
    if len(parts) != 4 or parts[0] != "resync" or parts[2] not in ("unsubscribe", "subscribe"):
        return None
    return parts[1], parts[2]


class GeminiAdapter(ExchangeAdapter):
    name = "gemini"
    ws_url = "wss://ws.gemini.com?snapshot=-1"
    snapshot_url = "https://api.gemini.com/v1/book"
    normalize_symbol = staticmethod(normalize_gemini_symbol)
    # Every book is checked against `@depth20` at the same update id. ARB-047
    # found Gemini's REST book is the stale side, so REST reconciliation is off.
    verifies_continuously = True

    def __init__(self, pairs: list[str]) -> None:
        super().__init__(pairs)
        self._initialized: set[str] = set()
        self._local_sequence: dict[str, int] = {}
        self._last_exchange_update_id: dict[str, int] = {}
        # Pairs being resubscribed on the open socket, and the monotonic
        # deadline after which the attempt escalates to a full reconnect.
        # Every time here is a frame receipt time, never a clock read, so
        # replay reaches the same decisions on the recorded timeline.
        self._resync_phase: dict[str, str] = {}
        self._resync_deadline_ns: dict[str, int] = {}
        self._resync_history: dict[str, deque[int]] = {}
        self._last_received_ns: int | None = None
        self._resync_number = 0
        # The latest top-N snapshot per pair waiting for the book to reach its
        # update id: (lastUpdateId, bids, asks).
        self._pending_verification: dict[
            str, tuple[int, tuple[PriceLevel, ...], tuple[PriceLevel, ...]]
        ] = {}
        # Per pair on this connection: the frame time verification was last
        # emitted (or armed). Pairs never sent a partial are not watched, so
        # captures recorded without `@depth20` replay unchanged.
        self._last_verified_ns: dict[str, int] = {}
        # Control frames for the stream loop to send; request_pair_resync is
        # synchronous and may run on another task while the loop awaits a read.
        self._outbox: list[str] = []
        self._outbox_ready = asyncio.Event()

    async def reset_state(self) -> None:
        await super().reset_state()
        self._initialized.clear()
        self._local_sequence.clear()
        self._last_exchange_update_id.clear()
        self._resync_phase.clear()
        self._resync_deadline_ns.clear()
        self._resync_history.clear()
        self._last_received_ns = None
        self._pending_verification.clear()
        self._last_verified_ns.clear()
        self._outbox.clear()
        self._outbox_ready.clear()

    async def subscribe(self, websocket: Any) -> None:
        # Resubscription only ever touches `@depth`; the partial stream keeps
        # flowing and is ignored while its pair is being rebuilt.
        streams: list[str] = []
        for pair in self.pairs:
            symbol = pair.lower().replace("-", "")
            streams.extend((f"{symbol}@depth", f"{symbol}@depth{VERIFY_DEPTH}"))
        await websocket.send(self.encode({"id": 1, "method": "SUBSCRIBE", "params": streams}))

    def request_pair_resync(self, pair: str) -> bool:
        """Resubscribe one pair's depth stream without dropping the socket.

        Gemini sends a full snapshot as the first frame of every new
        ``@depth`` subscription, so UNSUBSCRIBE then SUBSCRIBE rebuilds one
        book while the other pairs keep streaming. The SUBSCRIBE is sent only
        after the UNSUBSCRIBE is acknowledged, so the next frame for the pair
        is the snapshot rather than a tail delta of the old subscription.
        """
        if pair not in self.expected_pairs() or not self.connected or self._reconnect_requested:
            return False
        if pair in self._resync_phase:
            return True
        return self._begin_pair_resync(pair, trigger="external")

    def _begin_pair_resync(self, pair: str, *, trigger: str) -> bool:
        """Start resubscribing ``pair``; return False if it escalated to a reconnect."""
        self._drop_pair_state(pair)
        if self._resync_limit_reached(pair):
            self.request_reconnect("pair_resync_repeated")
            return False
        self._resync_phase[pair] = _UNSUBSCRIBING
        # The deadline starts at the next frame's receipt time (see _parse);
        # a repeated request keeps the first one, so a pair that never
        # recovers still escalates.
        self._send_control(pair, "unsubscribe")
        adapter_pair_resyncs_total.labels(exchange=self.name, trigger=trigger).inc()
        logger.info("adapter_pair_resync", exchange=self.name, pair=pair, trigger=trigger)
        return True

    def _resync_limit_reached(self, pair: str) -> bool:
        now_ns = self._last_received_ns
        if now_ns is None:
            return False
        history = self._resync_history.setdefault(pair, deque())
        while history and now_ns - history[0] >= PAIR_RESYNC_WINDOW_NS:
            history.popleft()
        if len(history) >= PAIR_RESYNC_LIMIT:
            return True
        history.append(now_ns)
        return False

    def _send_control(self, pair: str, action: str) -> None:
        symbol = pair.replace("-", "").lower()
        self._resync_number += 1
        self._outbox.append(
            self.encode(
                {
                    "id": _resync_request_id(symbol, action, self._resync_number),
                    "method": action.upper(),
                    "params": [f"{symbol}@depth"],
                }
            )
        )
        self._outbox_ready.set()

    def _drop_pair_state(self, pair: str) -> None:
        self._initialized.discard(pair)
        self._local_sequence.pop(pair, None)
        self._last_exchange_update_id.pop(pair, None)
        self._last_sequence_by_pair.pop(pair, None)
        self._drop_verification(pair)

    def _drop_verification(self, pair: str) -> None:
        if self._pending_verification.pop(pair, None) is not None:
            self._count_unaligned(pair)

    def _count_unaligned(self, pair: str) -> None:
        book_verifications_total.labels(exchange=self.name, pair=pair, outcome="unaligned").inc()

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return self._parse(message, time.monotonic_ns())

    async def ingest_message(self, text: str, received_monotonic_ns: int) -> IngestResult:
        return IngestResult(self._parse(text, received_monotonic_ns), [])

    def _parse(self, message: str, received_monotonic_ns: int) -> list[MarketEvent]:
        self._last_received_ns = received_monotonic_ns
        payload = json.loads(message)
        if payload.get("e") == "depthUpdate":
            events = self._on_depth_update(payload)
        elif "lastUpdateId" in payload and "symbol" in payload:
            events = self._on_partial_snapshot(payload)
        elif "id" in payload:
            events = self._on_acknowledgement(payload)
        else:
            events = []
        if any(deadline <= received_monotonic_ns for deadline in self._resync_deadline_ns.values()):
            self.request_reconnect("pair_resync_timeout")
        if any(
            received_monotonic_ns - verified_ns > VERIFY_STALL_NS
            for pair, verified_ns in self._last_verified_ns.items()
            if pair in self._initialized
        ):
            self.request_reconnect("verification_stalled")
        for pair in self._resync_phase:
            self._resync_deadline_ns.setdefault(
                pair, received_monotonic_ns + PAIR_RESYNC_TIMEOUT_NS
            )
        return events

    def _on_acknowledgement(self, payload: dict[str, Any]) -> list[MarketEvent]:
        parsed = _parse_resync_request_id(payload.get("id"))
        if parsed is None:
            return []
        symbol, action = parsed
        pair = normalize_gemini_symbol(symbol)
        if pair not in self.expected_pairs():
            return []
        if payload.get("status") != 200:
            logger.error(
                "adapter_pair_resync_rejected",
                exchange=self.name,
                pair=pair,
                action=action,
                response=payload,
            )
            self.request_reconnect("pair_resync_failed")
            return []
        if action != "unsubscribe" or self._resync_phase.get(pair) == _AWAITING_SNAPSHOT:
            return []

        events: list[MarketEvent] = []
        if pair not in self._resync_phase:
            # Replay sees the recorded acknowledgement of a request made by a
            # live caller it does not run (the reconciler); follow it the same way.
            if pair in self._initialized:
                events.append(self._reset_event(pair, time.time_ns()))
            self._drop_pair_state(pair)
            adapter_pair_resyncs_total.labels(exchange=self.name, trigger="external").inc()
        self._resync_phase[pair] = _AWAITING_SNAPSHOT
        self._send_control(pair, "subscribe")
        return events

    def _on_depth_update(self, payload: dict[str, Any]) -> list[MarketEvent]:
        symbol = payload.get("s")
        if not symbol or "U" not in payload or "u" not in payload:
            self.request_reconnect("protocol_error")
            return []

        pair = normalize_gemini_symbol(str(symbol))
        if self._resync_phase.get(pair) == _UNSUBSCRIBING:
            # The old subscription's last frames; the book was already dropped.
            return []
        first_id = int(payload["U"])
        last_id = int(payload["u"])
        if self._resync_phase.get(pair) == _AWAITING_SNAPSHOT and first_id != last_id:
            # A subscription snapshot is a single update id (U == u). Anything
            # else is a stray delta and must not seed the book; the resync
            # deadline escalates if the snapshot never comes.
            return []
        timestamp_ns = int(payload.get("E", time.time_ns()))
        if first_id > last_id:
            return self._sequence_gap(pair, timestamp_ns)

        bids = parse_levels(payload.get("b", []))
        asks = parse_levels(payload.get("a", []))

        if pair not in self._initialized:
            self._resync_phase.pop(pair, None)
            self._resync_deadline_ns.pop(pair, None)
            if pair in self._last_verified_ns and self._last_received_ns is not None:
                self._last_verified_ns[pair] = self._last_received_ns
            self._initialized.add(pair)
            self._local_sequence[pair] = 1
            self._last_exchange_update_id[pair] = last_id
            self._last_sequence_by_pair[pair] = 1
            return [
                MarketEvent(
                    exchange=self.name,
                    pair=pair,
                    kind=EventKind.SNAPSHOT,
                    sequence=1,
                    timestamp_ns=timestamp_ns,
                    bids=bids,
                    asks=asks,
                    exchange_first_sequence=first_id,
                    exchange_last_sequence=last_id,
                ),
                *self._verification_due(pair),
            ]

        # Gemini chains frames by repeating the previous frame's last id as
        # the next frame's first id; U == previous u + 1 is also contiguous.
        previous_id = self._last_exchange_update_id[pair]
        if last_id <= previous_id:
            return []
        if first_id > previous_id + 1:
            return self._sequence_gap(pair, timestamp_ns)

        sequence = self._local_sequence[pair] + 1
        self._local_sequence[pair] = sequence
        self._last_exchange_update_id[pair] = last_id
        self._last_sequence_by_pair[pair] = sequence
        return [
            MarketEvent(
                exchange=self.name,
                pair=pair,
                kind=EventKind.DELTA,
                sequence=sequence,
                timestamp_ns=timestamp_ns,
                bids=bids,
                asks=asks,
                exchange_first_sequence=first_id,
                exchange_last_sequence=last_id,
            ),
            *self._verification_due(pair),
        ]

    def _on_partial_snapshot(self, payload: dict[str, Any]) -> list[MarketEvent]:
        """Hold Gemini's top-N snapshot until the book has applied its update id.

        The snapshot usually arrives just before the `@depth` frame ending at
        the same id. A book that is not established, or that has already
        passed the id, cannot be compared at the same update and is skipped.
        """
        pair = normalize_gemini_symbol(str(payload["symbol"]))
        if pair not in self.expected_pairs():
            return []
        if self._last_received_ns is not None:
            self._last_verified_ns.setdefault(pair, self._last_received_ns)
        self._drop_verification(pair)
        target = int(payload["lastUpdateId"])
        current = self._last_exchange_update_id.get(pair)
        if pair not in self._initialized or current is None or current > target:
            self._count_unaligned(pair)
            return []
        self._pending_verification[pair] = (
            target,
            parse_levels(payload.get("bids", [])),
            parse_levels(payload.get("asks", [])),
        )
        return self._verification_due(pair)

    def _verification_due(self, pair: str) -> list[MarketEvent]:
        """Emit the pending check if the book is now at exactly its update id."""
        pending = self._pending_verification.get(pair)
        current = self._last_exchange_update_id.get(pair)
        if pending is None or current is None or current < pending[0]:
            return []
        del self._pending_verification[pair]
        if current > pending[0]:
            self._count_unaligned(pair)
            return []
        target, bids, asks = pending
        if self._last_received_ns is not None:
            self._last_verified_ns[pair] = self._last_received_ns
        return [
            MarketEvent(
                exchange=self.name,
                pair=pair,
                kind=EventKind.VERIFY,
                sequence=self._local_sequence[pair],
                timestamp_ns=time.time_ns(),
                bids=bids,
                asks=asks,
                exchange_last_sequence=target,
                verify_depth=VERIFY_DEPTH,
            )
        ]

    def _sequence_gap(self, pair: str, timestamp_ns: int) -> list[MarketEvent]:
        """Resubscribe only the gapped pair, as Gemini documents for resync.

        The RESET goes out first so the book manager stops trusting the pair
        before its replacement snapshot arrives; nothing else tells it.
        """
        self.gap_count += 1
        events = [self._reset_event(pair, timestamp_ns)] if pair in self._initialized else []
        self._begin_pair_resync(pair, trigger="sequence_gap")
        return events

    def _reset_event(self, pair: str, timestamp_ns: int) -> MarketEvent:
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.RESET,
            sequence=self._local_sequence[pair],
            timestamp_ns=timestamp_ns,
        )

    async def stream_events(self, websocket: Any) -> AsyncGenerator[MarketEvent, None]:
        """Read frames while sending any queued resubscription control frames."""
        iterator = websocket.__aiter__()
        read_task: asyncio.Task[Any] = asyncio.create_task(anext(iterator))
        wake_task: asyncio.Task[Any] | None = None
        try:
            while True:
                while self._outbox:
                    await websocket.send(self._outbox.pop(0))
                self._outbox_ready.clear()
                if wake_task is None:
                    wake_task = asyncio.create_task(self._outbox_ready.wait())
                done, _ = await asyncio.wait(
                    {read_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if wake_task in done:
                    wake_task = None
                if read_task not in done:
                    continue
                try:
                    message = read_task.result()
                except StopAsyncIteration:
                    return
                received_monotonic_ns = time.monotonic_ns()
                received_wall_ns = time.time_ns()
                self.last_message_ns = received_wall_ns
                text_message = message.decode() if isinstance(message, bytes) else message
                events = self._parse(text_message, received_monotonic_ns)
                if self._capture_sink is not None:
                    self._capture_sink.record_ws(
                        self.name,
                        text_message,
                        events,
                        wall_ns=received_wall_ns,
                        mono_ns=received_monotonic_ns,
                    )
                for event in events:
                    yield replace(event, received_monotonic_ns=received_monotonic_ns)
                    if self._reconnect_requested:
                        raise self.reconnect_request()
                if self._reconnect_requested:
                    raise self.reconnect_request()
                read_task = asyncio.create_task(anext(iterator))
        finally:
            tasks = [task for task in (read_task, wake_task) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        """Fetch a comparison snapshot for SnapshotReconciler, not stream recovery."""
        symbol = pair.replace("-", "").lower()
        payload = await self.client_get_json(
            f"{self.snapshot_url}/{symbol}?limit_bids=100&limit_asks=100"
        )
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=time.time_ns(),
            bids=parse_level_mappings(payload["bids"], price_key="price", size_key="amount"),
            asks=parse_level_mappings(payload["asks"], price_key="price", size_key="amount"),
        )
