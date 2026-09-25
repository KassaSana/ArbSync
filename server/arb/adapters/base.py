from __future__ import annotations

import abc
import asyncio
import contextvars
import json
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from inspect import isawaitable
from typing import Any

import httpx
import structlog

from arb.capture import SnapshotProvenance
from arb.metrics import adapter_reconnects_total
from arb.ports import CaptureSink
from arb.types import MarketEvent, PriceLevel

logger = structlog.get_logger(__name__)


def parse_levels(rows: Iterable[Sequence[Any]]) -> tuple[PriceLevel, ...]:
    """Decode [price, size, ...] rows, keeping the exchange's decimal strings exact."""
    return tuple(PriceLevel(price=Decimal(row[0]), size=Decimal(row[1])) for row in rows)


def parse_level_mappings(
    rows: Iterable[Mapping[str, Any]], *, price_key: str, size_key: str
) -> tuple[PriceLevel, ...]:
    """Decode {price_key: ..., size_key: ...} rows, keeping the decimal strings exact."""
    return tuple(
        PriceLevel(price=Decimal(row[price_key]), size=Decimal(row[size_key])) for row in rows
    )


@dataclass(frozen=True)
class AdapterStatusSnapshot:
    exchange: str
    connected: bool
    last_message_age_ms: int | None
    gap_count: int
    reconnect_count: int
    last_error: str | None

    def as_payload(self) -> dict[str, str | int | bool | None]:
        return {
            "exchange": self.exchange,
            "connected": self.connected,
            "last_message_age_ms": self.last_message_age_ms,
            "gap_count": self.gap_count,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class SnapshotRequestContext:
    purpose: str
    pair: str
    connection_generation: int
    request_wall_ns: int
    request_mono_ns: int


@dataclass(frozen=True)
class SnapshotRequest:
    """One REST snapshot the adapter needs started for `pair` right now.

    The adapter's recovery state machine emits these instead of fetching
    itself, so the live stream shell and offline replay decide *when* the
    response lands while the adapter alone decides what to do with it.
    """

    pair: str
    purpose: str
    attempt: int


@dataclass(frozen=True)
class IngestResult:
    """Normalized events plus the snapshot fetches one input made necessary."""

    events: list[MarketEvent]
    snapshot_requests: list[SnapshotRequest]


# Bounded label values for `arb_adapter_reconnects_total{reason=...}`: each
# names why a connection was dropped, never an exception class.
TRANSPORT_ERROR = "transport_error"


class AdapterReconnectRequested(RuntimeError):
    """Raised by a stream loop to drop a connection the adapter asked to replace."""

    def __init__(self, cause: str, detail: str | None = None) -> None:
        super().__init__(detail or f"adapter requested reconnect ({cause})")
        self.cause = cause


class ExchangeAdapter(abc.ABC):
    name: str
    ws_url: str
    snapshot_url: str

    def __init__(self, pairs: list[str]) -> None:
        self.pairs = pairs
        self.connected = False
        self.last_message_ns: int | None = None
        self.gap_count = 0
        self.reconnect_count = 0
        self.last_error: str | None = None
        self._last_sequence_by_pair: dict[str, int] = {}
        self._connection_state_callback: Callable[[str, bool], Awaitable[None] | None] | None = None
        self._reconnect_requested = False
        self._reconnect_cause: str | None = None
        self.connection_generation = 0
        self._snapshot_context: contextvars.ContextVar[SnapshotRequestContext | None] = (
            contextvars.ContextVar("snapshot_context", default=None)
        )
        self._client: httpx.AsyncClient | None = None
        self._capture_sink: CaptureSink | None = None

    def set_capture_sink(self, sink: CaptureSink | None) -> None:
        """Attach the capture writer that receives exact inbound traffic.

        The sink's record calls never block, so capturing cannot slow down
        ingestion; a full writer drops frames and counts them instead.
        """
        self._capture_sink = sink

    def set_connection_state_callback(
        self, callback: Callable[[str, bool], Awaitable[None] | None]
    ) -> None:
        self._connection_state_callback = callback

    async def _report_connection_state(self, connected: bool) -> None:
        if connected:
            self.connection_generation += 1
        if self._capture_sink is not None:
            self._capture_sink.record_connection(
                self.name,
                connected,
                self.connection_generation,
                wall_ns=time.time_ns(),
                mono_ns=time.monotonic_ns(),
                reason=self.last_error,
            )
        if self._connection_state_callback is not None:
            result = self._connection_state_callback(self.name, connected)
            if isawaitable(result):
                await result

    @staticmethod
    @abc.abstractmethod
    def normalize_symbol(symbol: str) -> str:
        """Map one exchange-native symbol to its normalized `BASE-QUOTE` name."""
        raise NotImplementedError

    def expected_pairs(self) -> list[str]:
        """Normalized names of the pairs this adapter is configured to track.

        Callers build their venue and pair rosters from the adapters they were
        given, so a new exchange cannot be half-registered by adding an adapter
        and forgetting a list somewhere else.
        """
        return [self.normalize_symbol(symbol) for symbol in self.pairs]

    @abc.abstractmethod
    async def subscribe(self, websocket: Any) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def parse_message(self, message: str) -> list[MarketEvent]:
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        raise NotImplementedError

    async def ingest_message(self, text: str, received_monotonic_ns: int) -> IngestResult:
        """Advance the adapter's sequence state by one inbound message.

        Performs no network or clock waits: any REST snapshot the message
        makes necessary is returned as a :class:`SnapshotRequest` for the
        caller to start, and its result comes back through
        :meth:`complete_snapshot`. Venues whose stream carries its own
        snapshots never request one, so the default simply parses.
        """
        return IngestResult(await self.parse_message(text), [])

    def complete_snapshot(self, pair: str, snapshot: MarketEvent) -> IngestResult:
        """Deliver a fetched snapshot to the adapter's recovery state machine.

        Returns the events it unlocks, or a retry request when the snapshot
        predates the buffered updates it must align with. Raises
        ``RuntimeError`` once bounded recovery is exhausted, after requesting
        a full reconnect.
        """
        raise NotImplementedError(f"{self.name} never synchronizes from REST snapshots")

    def snapshot_failed(self, pair: str) -> None:
        """Record that a requested snapshot fetch raised instead of returning."""
        self.request_reconnect("snapshot_failed")

    async def reset_state(self) -> None:
        """Clear per-connection state. Called before each (re)subscribe so
        the next message after a reconnect re-fetches a fresh snapshot
        instead of applying deltas onto a stale book."""
        self._last_sequence_by_pair.clear()
        self._reconnect_requested = False
        self._reconnect_cause = None

    async def fetch_snapshot_with_context(
        self, pair: str, trigger_sequence: int, *, purpose: str
    ) -> MarketEvent:
        """Fetch a snapshot while preserving its recovery provenance."""
        token = self._snapshot_context.set(
            SnapshotRequestContext(
                purpose=purpose,
                pair=pair,
                connection_generation=self.connection_generation,
                request_wall_ns=time.time_ns(),
                request_mono_ns=time.monotonic_ns(),
            )
        )
        try:
            return await self.fetch_snapshot(pair, trigger_sequence)
        finally:
            self._snapshot_context.reset(token)

    # True when the adapter checks every book against the exchange's own
    # snapshots at the same update id (EventKind.VERIFY). Such venues are left
    # out of REST reconciliation, whose non-atomic comparison would only add
    # false recoveries on top of an exact check.
    verifies_continuously: bool = False

    # Levels the venue's subscription can hold at most, or None for a full
    # book. Depth pricing reports it with every quote because "could not fill"
    # on a capped book is not comparable with the same result on a full one.
    subscribed_depth_levels: int | None = None

    def request_reconnect(self, cause: str) -> None:
        """Ask the stream loop to drop and rebuild the whole connection.

        ``cause`` labels the reconnect metric, so it must come from a small
        fixed vocabulary (``sequence_gap``, ``confirmed_drift``, ...). The
        first cause wins until the connection is replaced.
        """
        if not self._reconnect_requested:
            self._reconnect_cause = cause
        self._reconnect_requested = True

    def reconnect_request(self) -> AdapterReconnectRequested:
        """The exception a stream loop raises for the pending reconnect request."""
        return AdapterReconnectRequested(self._reconnect_cause or "unspecified")

    def request_pair_resync(self, pair: str) -> bool:
        """Resynchronize one pair without dropping the shared connection.

        The default adapter cannot do this, so callers must fall back to
        :meth:`request_reconnect`. Adapters that multiplex pairs on one
        socket override this to re-fetch only the affected pair and return
        ``True``. Implementations must not raise; recovery callers treat a
        ``False`` return as "fall back to a full reconnect".
        """
        return False

    async def stream_events(self, websocket: Any) -> AsyncGenerator[MarketEvent, None]:
        """Normalize one connection's messages, preserving their receipt time."""
        async for message in websocket:
            received_monotonic_ns = time.monotonic_ns()
            received_wall_ns = time.time_ns()
            self.last_message_ns = received_wall_ns
            text_message = message.decode() if isinstance(message, bytes) else message
            events = await self.parse_message(text_message)
            if self._capture_sink is not None:
                self._capture_sink.record_ws(
                    self.name,
                    text_message,
                    events,
                    wall_ns=received_wall_ns,
                    mono_ns=received_monotonic_ns,
                )
            for event in events:
                yield (
                    event
                    if event.received_monotonic_ns is not None
                    else replace(event, received_monotonic_ns=received_monotonic_ns)
                )
                if self._reconnect_requested:
                    raise self.reconnect_request()
            if self._reconnect_requested:
                raise self.reconnect_request()

    async def connect(self) -> AsyncGenerator[MarketEvent, None]:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, max_size=10_000_000) as websocket:
                    self.connected = True
                    await self._report_connection_state(True)
                    self.last_error = None
                    await self.reset_state()
                    await self.subscribe(websocket)
                    backoff = 1.0
                    async for event in self.stream_events(websocket):
                        yield event
            except Exception as exc:  # pragma: no cover - reconnect path
                self.last_error = str(exc)
                self.connected = False
                await self._report_connection_state(False)
                self.reconnect_count += 1
                cause = exc.cause if isinstance(exc, AdapterReconnectRequested) else TRANSPORT_ERROR
                adapter_reconnects_total.labels(exchange=self.name, reason=cause).inc()
                logger.warning(
                    "adapter_reconnect",
                    exchange=self.name,
                    cause=cause,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, 30.0)
            finally:
                if self.connected:
                    self.connected = False
                    await self._report_connection_state(False)

    @staticmethod
    def encode(payload: dict[str, Any]) -> str:
        return json.dumps(payload)

    async def client_get_json(self, url: str) -> dict[str, Any]:
        """Fetch one REST payload over this adapter's reused connection pool.

        Snapshot fetches happen on the recovery path, where a fresh client per
        call means a full TCP and TLS handshake before every resync.
        """
        context = self._snapshot_context.get()
        try:
            response = await self.http_client().get(url)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            # Replay must reproduce a failed fetch, so it is recorded with the
            # same provenance a response would carry.
            if self._capture_sink is not None:
                self._capture_sink.record_snapshot_failure(
                    self.name, url, repr(exc), provenance=self._provenance(context)
                )
            raise
        if self._capture_sink is not None and isinstance(payload, dict):
            self._capture_sink.record_snapshot(
                self.name,
                url,
                payload,
                provenance=self._provenance(context),
            )
        return payload  # type: ignore[no-any-return]

    def _provenance(self, context: SnapshotRequestContext | None) -> SnapshotProvenance:
        return SnapshotProvenance(
            purpose="unknown" if context is None else context.purpose,
            pair="" if context is None else context.pair,
            connection_generation=(
                self.connection_generation if context is None else context.connection_generation
            ),
            request_wall_ns=(None if context is None else context.request_wall_ns),
            request_mono_ns=(None if context is None else context.request_mono_ns),
            response_wall_ns=time.time_ns(),
            response_mono_ns=time.monotonic_ns(),
        )

    def http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        """Release the shared REST connection pool. Safe to call more than once."""
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    def status_snapshot(self, now_ns: int | None = None) -> AdapterStatusSnapshot:
        current_ns = time.time_ns() if now_ns is None else now_ns
        age_ms = None
        if self.last_message_ns is not None:
            age_ms = max(0, (current_ns - self.last_message_ns) // 1_000_000)
        return AdapterStatusSnapshot(
            exchange=self.name,
            connected=self.connected,
            last_message_age_ms=age_ms,
            gap_count=self.gap_count,
            reconnect_count=self.reconnect_count,
            last_error=self.last_error,
        )


def request_scoped_resync(adapter: ExchangeAdapter, pair: str) -> bool:
    """Prefer a single-pair resync; return whether the connection was kept.

    Returns ``True`` when the adapter handled ``pair`` without dropping its
    shared socket, in which case the caller must not request a full
    reconnect. The lookup is type-level so test doubles without a real
    override (including ``Mock`` adapters) fall back to ``False`` instead of
    accidentally claiming scoped recovery.
    """
    hook = getattr(type(adapter), "request_pair_resync", None)
    if hook is None or hook is ExchangeAdapter.request_pair_resync:
        return False
    return bool(adapter.request_pair_resync(pair))
