from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

from arb.adapters.base import ExchangeAdapter, parse_levels
from arb.metrics import adapter_pair_resyncs_total
from arb.types import EventKind, MarketEvent, PriceLevel

logger = structlog.get_logger(__name__)

# Longer quote assets first: "BTCBUSD" is BTC/BUSD, not BTCB/USD.
_BINANCE_QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "USD")


def normalize_binance_symbol(symbol: str) -> str:
    upper = symbol.upper()
    for quote in _BINANCE_QUOTE_ASSETS:
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[: -len(quote)]}-{quote}"
    return upper


@dataclass(frozen=True)
class _DepthUpdate:
    pair: str
    first_id: int
    last_id: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    received_monotonic_ns: int | None = None


class BinanceAdapter(ExchangeAdapter):
    name = "binance"
    ws_url = "wss://stream.binance.us:9443/ws"
    snapshot_url = "https://api.binance.us/api/v3/depth"
    max_buffered_updates = 1_000
    normalize_symbol = staticmethod(normalize_binance_symbol)

    def __init__(self, pairs: list[str]) -> None:
        super().__init__(pairs)
        self._initialized: set[str] = set()
        self._local_seq: dict[str, int] = {}
        self._last_exchange_update_id: dict[str, int] = {}
        self._buffers: dict[str, list[_DepthUpdate]] = {}

    async def reset_state(self) -> None:
        await super().reset_state()
        self._initialized.clear()
        self._local_seq.clear()
        self._last_exchange_update_id.clear()
        self._buffers.clear()

    def request_pair_resync(self, pair: str) -> bool:
        """Re-fetch one pair's book without dropping the combined stream.

        External recovery (reconciliation drift, an invalid book) discards
        only this pair's sync state; the next update for the pair takes the
        normal uninitialized path and re-aligns from REST while every other
        pair keeps streaming. Buffered updates are deliberately kept: an
        initialized pair never holds any, and a pair with a snapshot fetch
        already in flight needs them for the alignment that rebuilds its
        book. No snapshot is fetched here: waking the read loop from another
        task would race its in-flight snapshot tasks, and a thin pair with
        no imminent update is safer left ineligible until traffic resumes
        than rebuilt from a snapshot with nothing to align.
        """
        self._begin_pair_resync(pair, trigger="external")
        return True

    def _begin_pair_resync(self, pair: str, *, trigger: str) -> None:
        """Drop one pair's sync state while keeping the shared connection.

        Unlike :meth:`_restart_sync`, this never sets
        ``_reconnect_requested``, so the socket and every other pair's
        sequence tracking survive. Callers that exhaust the bounded recovery
        (buffer overflow, repeated snapshot misalignment) still fall back to
        :meth:`_restart_sync`.
        """
        self._initialized.discard(pair)
        self._local_seq.pop(pair, None)
        self._last_exchange_update_id.pop(pair, None)
        self._last_sequence_by_pair.pop(pair, None)
        adapter_pair_resyncs_total.labels(exchange=self.name, trigger=trigger).inc()
        logger.info("adapter_pair_resync", exchange=self.name, pair=pair, trigger=trigger)

    async def subscribe(self, websocket: Any) -> None:
        params = [f"{pair.lower()}@depth" for pair in self.pairs]
        await websocket.send(self.encode({"method": "SUBSCRIBE", "params": params, "id": 1}))

    async def parse_message(self, message: str) -> list[MarketEvent]:
        """Parse one message directly; production streaming uses stream_events."""
        payload = json.loads(message)
        if "b" in payload and "a" in payload and "s" in payload:
            return await self._emit(self._decode_depth_update(payload))

        if "bids" in payload and "asks" in payload:
            pair = normalize_binance_symbol(payload.get("symbol", self.pairs[0]))
            bids = parse_levels(payload["bids"])
            asks = parse_levels(payload["asks"])
            seq = int(payload["lastUpdateId"])
            self._initialized.add(pair)
            self._local_seq[pair] = seq
            self._last_exchange_update_id[pair] = seq
            self._last_sequence_by_pair[pair] = seq
            return [
                MarketEvent(
                    exchange=self.name,
                    pair=pair,
                    kind=EventKind.SNAPSHOT,
                    sequence=seq,
                    timestamp_ns=time.time_ns(),
                    bids=bids,
                    asks=asks,
                    exchange_last_sequence=seq,
                )
            ]

        return []

    async def stream_events(self, websocket: Any) -> AsyncIterator[MarketEvent]:
        """Keep reading and buffering depth updates while snapshots are in flight."""
        iterator = websocket.__aiter__()
        read_task: asyncio.Task[Any] | None = asyncio.create_task(anext(iterator))
        snapshot_tasks: dict[str, asyncio.Task[MarketEvent]] = {}
        snapshot_attempts: dict[str, int] = {}
        try:
            while read_task is not None or snapshot_tasks:
                waiting: set[asyncio.Task[Any]] = set(snapshot_tasks.values())
                if read_task is not None:
                    waiting.add(read_task)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)

                if read_task is not None and read_task in done:
                    try:
                        message = read_task.result()
                    except StopAsyncIteration:
                        read_task = None
                    else:
                        received_monotonic_ns = time.monotonic_ns()
                        received_wall_ns = time.time_ns()
                        self.last_message_ns = received_wall_ns
                        text_message = message.decode() if isinstance(message, bytes) else message
                        payload = json.loads(text_message)
                        if payload.get("e") == "serverShutdown":
                            self.request_reconnect()
                        elif "b" in payload and "a" in payload and "s" in payload:
                            update = self._decode_depth_update(payload, received_monotonic_ns)
                            emitted: list[MarketEvent] = []
                            if update.pair in self._initialized:
                                emitted = self._emit_initialized(update)
                                if (
                                    update.pair not in self._initialized
                                    and update.pair not in snapshot_tasks
                                    and not self._reconnect_requested
                                ):
                                    # A sequence gap just demoted this pair.
                                    # Re-fetch only it; the socket and the
                                    # other pairs keep flowing.
                                    snapshot_attempts[update.pair] = 1
                                    snapshot_tasks[update.pair] = asyncio.create_task(
                                        self.fetch_snapshot(update.pair, trigger_sequence=0)
                                    )
                            else:
                                self._buffer(update)
                                if update.pair not in snapshot_tasks:
                                    snapshot_attempts[update.pair] = 1
                                    snapshot_tasks[update.pair] = asyncio.create_task(
                                        self.fetch_snapshot(update.pair, trigger_sequence=0)
                                    )
                            if self._capture_sink is not None:
                                self._capture_sink.record_ws(
                                    self.name,
                                    text_message,
                                    emitted,
                                    wall_ns=received_wall_ns,
                                    mono_ns=received_monotonic_ns,
                                )
                            for event in emitted:
                                yield event
                        else:
                            parsed = await self.parse_message(text_message)
                            if self._capture_sink is not None:
                                self._capture_sink.record_ws(
                                    self.name,
                                    text_message,
                                    parsed,
                                    wall_ns=received_wall_ns,
                                    mono_ns=received_monotonic_ns,
                                )
                            for event in parsed:
                                yield event
                                if self._reconnect_requested:
                                    raise RuntimeError("adapter requested reconnect")

                        if self._reconnect_requested:
                            raise RuntimeError("adapter requested reconnect")
                        read_task = asyncio.create_task(anext(iterator))

                for pair, task in list(snapshot_tasks.items()):
                    if task not in done:
                        continue
                    del snapshot_tasks[pair]
                    try:
                        snapshot = task.result()
                    except Exception as exc:
                        self._restart_sync(pair)
                        raise RuntimeError(f"snapshot retrieval failed for {pair}") from exc

                    events = self._align_snapshot(pair, snapshot)
                    if events is None:
                        if snapshot_attempts[pair] >= 3:
                            self._restart_sync(pair)
                            raise RuntimeError(f"snapshot did not catch up for {pair}")
                        snapshot_attempts[pair] += 1
                        snapshot_tasks[pair] = asyncio.create_task(
                            self.fetch_snapshot(pair, trigger_sequence=0)
                        )
                        continue
                    snapshot_attempts.pop(pair, None)
                    for event in events:
                        yield event
                        if self._reconnect_requested:
                            raise RuntimeError("adapter requested reconnect")
                    if self._reconnect_requested:
                        raise RuntimeError("adapter requested reconnect")
        finally:
            tasks = [*snapshot_tasks.values()]
            if read_task is not None:
                tasks.append(read_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _decode_depth_update(
        self, payload: dict[str, Any], received_monotonic_ns: int | None = None
    ) -> _DepthUpdate:
        return _DepthUpdate(
            pair=normalize_binance_symbol(payload["s"]),
            first_id=int(payload["U"]),
            last_id=int(payload["u"]),
            bids=parse_levels(payload["b"]),
            asks=parse_levels(payload["a"]),
            received_monotonic_ns=received_monotonic_ns,
        )

    def _buffer(self, update: _DepthUpdate) -> None:
        buffer = self._buffers.setdefault(update.pair, [])
        buffer.append(update)
        if len(buffer) > self.max_buffered_updates:
            self._restart_sync(update.pair)

    async def _emit(self, update: _DepthUpdate) -> list[MarketEvent]:
        if update.pair not in self._initialized:
            self._buffer(update)
            if self._reconnect_requested:
                return []
            return await self._synchronize(update.pair)
        return self._emit_initialized(update)

    def _emit_initialized(self, update: _DepthUpdate) -> list[MarketEvent]:
        last_id = self._last_exchange_update_id[update.pair]
        if update.last_id <= last_id:
            return []
        if update.first_id > last_id + 1:
            self.gap_count += 1
            # A gap demotes only this pair: the triggering update is kept so
            # the snapshot alignment has something to span, and the stream
            # loop (or the next parse_message call) re-fetches just this pair.
            # Buffer overflow inside _buffer still escalates to a full
            # reconnect, which is the bounded fallback.
            self._begin_pair_resync(update.pair, trigger="sequence_gap")
            self._buffer(update)
            return []
        return [self._delta_event(update)]

    async def _synchronize(self, pair: str) -> list[MarketEvent]:
        for _ in range(3):
            snapshot = await self.fetch_snapshot(pair, trigger_sequence=0)
            events = self._align_snapshot(pair, snapshot)
            if events is None:
                continue
            return events
        self._restart_sync(pair)
        return []

    def _align_snapshot(self, pair: str, snapshot: MarketEvent) -> list[MarketEvent] | None:
        buffer = self._buffers.get(pair, [])
        if not buffer:
            self._restart_sync(pair)
            return []
        snapshot_id = snapshot.exchange_last_sequence or snapshot.sequence
        if snapshot_id < buffer[0].first_id:
            return None

        pending = [update for update in buffer if update.last_id > snapshot_id]
        if pending and not (pending[0].first_id <= snapshot_id <= pending[0].last_id):
            self.gap_count += 1
            self._restart_sync(pair)
            return []

        self._initialized.add(pair)
        self._local_seq[pair] = snapshot.sequence
        self._last_exchange_update_id[pair] = snapshot_id
        self._last_sequence_by_pair[pair] = snapshot.sequence
        self._buffers.pop(pair, None)
        events = [snapshot]
        for update in pending:
            last_id = self._last_exchange_update_id[pair]
            if update.last_id <= last_id:
                continue
            if update.first_id > last_id + 1:
                self.gap_count += 1
                self._restart_sync(pair)
                return []
            events.append(self._delta_event(update))
        return events

    def _delta_event(self, update: _DepthUpdate) -> MarketEvent:
        sequence = self._local_seq[update.pair] + 1
        self._local_seq[update.pair] = sequence
        self._last_exchange_update_id[update.pair] = update.last_id
        self._last_sequence_by_pair[update.pair] = sequence
        return MarketEvent(
            exchange=self.name,
            pair=update.pair,
            kind=EventKind.DELTA,
            sequence=sequence,
            timestamp_ns=time.time_ns(),
            bids=update.bids,
            asks=update.asks,
            exchange_first_sequence=update.first_id,
            exchange_last_sequence=update.last_id,
            received_monotonic_ns=update.received_monotonic_ns,
        )

    def _restart_sync(self, pair: str) -> None:
        """Fall back to a full-venue reconnect.

        Only ``pair``'s sync state is discarded, but the reconnect request
        drops the combined stream, so every pair rebuilds. Reserved for
        bounded-failure paths: buffer overflow, repeated snapshot
        misalignment, snapshot failure, and server shutdown.
        """
        self._initialized.discard(pair)
        self._local_seq.pop(pair, None)
        self._last_exchange_update_id.pop(pair, None)
        self._last_sequence_by_pair.pop(pair, None)
        self._buffers.pop(pair, None)
        self.request_reconnect()

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        symbol = pair.replace("-", "")
        payload = await self.client_get_json(f"{self.snapshot_url}?symbol={symbol}&limit=5000")
        received_monotonic_ns = time.monotonic_ns()
        sequence = int(payload.get("lastUpdateId", trigger_sequence))
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=max(sequence, trigger_sequence),
            timestamp_ns=time.time_ns(),
            bids=parse_levels(payload["bids"]),
            asks=parse_levels(payload["asks"]),
            exchange_last_sequence=sequence,
            received_monotonic_ns=received_monotonic_ns,
        )
