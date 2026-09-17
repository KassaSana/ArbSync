from __future__ import annotations

import abc
import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from inspect import isawaitable
from typing import Any

import httpx
import structlog

from arb.capture import CaptureWriter
from arb.metrics import adapter_reconnects_total
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
        self._client: httpx.AsyncClient | None = None
        self._capture_sink: CaptureWriter | None = None

    def set_capture_sink(self, sink: CaptureWriter | None) -> None:
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

    async def reset_state(self) -> None:
        """Clear per-connection state. Called before each (re)subscribe so
        the next message after a reconnect re-fetches a fresh snapshot
        instead of applying deltas onto a stale book."""
        self._last_sequence_by_pair.clear()
        self._reconnect_requested = False

    def request_reconnect(self) -> None:
        self._reconnect_requested = True

    def request_pair_resync(self, pair: str) -> bool:
        """Resynchronize one pair without dropping the shared connection.

        The default adapter cannot do this, so callers must fall back to
        :meth:`request_reconnect`. Adapters that multiplex pairs on one
        socket override this to re-fetch only the affected pair and return
        ``True``. Implementations must not raise; recovery callers treat a
        ``False`` return as "fall back to a full reconnect".
        """
        return False

    async def stream_events(self, websocket: Any) -> AsyncIterator[MarketEvent]:
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
                    raise RuntimeError("adapter requested reconnect")
            if self._reconnect_requested:
                raise RuntimeError("adapter requested reconnect")

    async def connect(self) -> AsyncIterator[MarketEvent]:
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
                self.connected = False
                await self._report_connection_state(False)
                self.last_error = str(exc)
                self.reconnect_count += 1
                adapter_reconnects_total.labels(exchange=self.name, reason=type(exc).__name__).inc()
                logger.warning("adapter_reconnect", exchange=self.name, reason=str(exc))
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
        response = await self.http_client().get(url)
        response.raise_for_status()
        payload = response.json()
        if self._capture_sink is not None and isinstance(payload, dict):
            self._capture_sink.record_snapshot(self.name, url, payload)
        return payload  # type: ignore[no-any-return]

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
