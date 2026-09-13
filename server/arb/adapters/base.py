from __future__ import annotations

import abc
import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from inspect import isawaitable
from typing import Any

import httpx
import structlog

from arb.metrics import adapter_reconnects_total
from arb.types import MarketEvent

logger = structlog.get_logger(__name__)


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

    async def stream_events(self, websocket: Any) -> AsyncIterator[MarketEvent]:
        """Normalize one connection's messages, preserving their receipt time."""
        async for message in websocket:
            received_monotonic_ns = time.monotonic_ns()
            self.last_message_ns = time.time_ns()
            text_message = message.decode() if isinstance(message, bytes) else message
            for event in await self.parse_message(text_message):
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
        return response.json()  # type: ignore[no-any-return]

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
