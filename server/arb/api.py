from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.websockets import WebSocketState

from arb.adapters.base import ExchangeAdapter
from arb.metrics import (
    book_eligible,
    book_staleness_seconds,
    render_metrics,
    ws_client_queue_overflows_total,
    ws_clients,
)
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.types import LiveMessage

Window = Literal["1h", "4h", "24h", "1d", "72h", "1w"]


def window_to_ns(window: str) -> int:
    values: dict[str, int] = {
        "1h": 3_600_000_000_000,
        "4h": 14_400_000_000_000,
        "24h": 86_400_000_000_000,
        "1d": 86_400_000_000_000,
        "72h": 259_200_000_000_000,
        "1w": 604_800_000_000_000,
    }
    return values.get(window, values["1h"])


def serialize_peak(peak: dict[str, int] | None) -> dict[str, int | str] | None:
    if peak is None:
        return None
    return {**peak, "minute_start_ns": str(peak["minute_start_ns"])}


READINESS_WINDOW_NS = 30_000_000_000


# Fields that decide what a book update actually shows. A status also carries
# `age_ms`, which changes on every event even when nothing about the book has;
# comparing it would defeat suppression entirely. The dashboard re-polls
# authoritative book status every two seconds, so a suppressed repeat costs a
# stale age reading at most.
_STATUS_FIELDS = ("initialized", "continuous", "connected", "eligible", "reason")
_QUOTE_FIELDS = ("best_bid_price", "best_bid_size", "best_ask_price", "best_ask_size")


def _display_signature(message: LiveMessage) -> tuple[object, ...]:
    fields = _STATUS_FIELDS if message.type == "book_status" else _QUOTE_FIELDS
    return tuple(message.payload.get(field) for field in fields)


@dataclass
class _ClientConnection:
    queue: asyncio.Queue[dict[str, object]]
    sender_task: asyncio.Task[None] | None = None


class LiveBroadcaster:
    def __init__(self, queue_maxsize: int = 256, coalesce_interval: float = 0.05) -> None:
        if queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be positive")
        if coalesce_interval < 0:
            raise ValueError("coalesce_interval must not be negative")
        self._clients: dict[WebSocket, _ClientConnection] = {}
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()
        self._stream_sequence = 0
        self._coalesce_interval = coalesce_interval
        self._pending: dict[tuple[str, str, str], LiveMessage] = {}
        self._last_sent: dict[tuple[str, str, str], tuple[object, ...]] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def connect(
        self,
        websocket: WebSocket,
        initial_state: Callable[[], LiveMessage] | None = None,
    ) -> None:
        await websocket.accept()
        async with self._lock:
            connection = _ClientConnection(asyncio.Queue(maxsize=self._queue_maxsize))
            if initial_state is not None:
                connection.queue.put_nowait(self._envelope(initial_state()))
            self._clients[websocket] = connection
            connection.sender_task = asyncio.create_task(self._send_messages(websocket, connection))
            ws_clients.set(len(self._clients))
            # A joining client has its own baseline, so nothing may be withheld
            # from it on the grounds that an earlier client already saw it.
            self._last_sent.clear()

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            connection = self._clients.pop(websocket, None)
            ws_clients.set(len(self._clients))
        if connection is not None and connection.sender_task is not None:
            connection.sender_task.cancel()
            await asyncio.gather(connection.sender_task, return_exceptions=True)

    async def broadcast(self, message: LiveMessage) -> None:
        """Deliver one message to every client immediately."""
        await self._deliver([message])

    async def broadcast_book(self, exchange: str, pair: str, message: LiveMessage) -> None:
        """Queue a per-book display update, keeping only the newest per book.

        Book state is level-triggered: a client that receives the latest
        top-of-book and status for a book has everything an earlier update
        carried. Collapsing them bounds dashboard traffic by book count and
        flush rate instead of by ingestion rate. Opportunity traffic still uses
        the bounded client queue. Detection is unaffected: it reads
        the book manager directly and never waits on delivery.
        """
        if self._superseded(exchange, pair, message):
            return
        if self._coalesce_interval <= 0:
            await self.broadcast(message)
            return
        self._pending[(message.type, exchange, pair)] = message
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def broadcast_book_now(self, exchange: str, pair: str, message: LiveMessage) -> None:
        """Send a book update immediately, discarding queued updates it supersedes.

        Connection-state changes must not be reordered behind a coalesced
        update captured before them, which would show a dropped book as live.
        """
        for key in [
            ("top_of_book", exchange, pair),
            ("book_status", exchange, pair),
        ]:
            self._pending.pop(key, None)
        if self._superseded(exchange, pair, message):
            return
        await self.broadcast(message)

    def _superseded(self, exchange: str, pair: str, message: LiveMessage) -> bool:
        """Report whether this update shows nothing the client was not already told.

        A rejected event repeats the same status for as long as the condition
        lasts: after a sequence gap every delta is refused as `book_stale`
        until a snapshot arrives. Sending one message per refused event is what
        filled client queues and evicted the dashboard, so an unchanged update
        is dropped rather than queued.
        """
        key = (message.type, exchange, pair)
        signature = _display_signature(message)
        if self._last_sent.get(key) == signature:
            return True
        self._last_sent[key] = signature
        return False

    async def flush(self) -> None:
        """Deliver every queued per-book update now."""
        if not self._pending:
            return
        # Swap without awaiting so updates arriving during delivery are kept.
        pending = self._pending
        self._pending = {}
        await self._deliver(list(pending.values()))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._coalesce_interval)
            if not self._pending:
                continue
            await self.flush()

    async def aclose(self) -> None:
        """Stop coalescing and deliver anything still queued."""
        task = self._flush_task
        self._flush_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.flush()

    async def _deliver(self, messages: list[LiveMessage]) -> None:
        dropped: list[tuple[WebSocket, _ClientConnection]] = []
        async with self._lock:
            if not self._clients:
                return
            payloads = [self._envelope(message) for message in messages]
            for client, connection in list(self._clients.items()):
                try:
                    for payload in payloads:
                        connection.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    dropped.append((client, connection))
                    del self._clients[client]
                    ws_client_queue_overflows_total.inc()
            if dropped:
                ws_clients.set(len(self._clients))
        for client, connection in dropped:
            if connection.sender_task is not None:
                connection.sender_task.cancel()
            asyncio.create_task(self._close_slow_client(client))

    async def _send_messages(self, websocket: WebSocket, connection: _ClientConnection) -> None:
        try:
            while True:
                await websocket.send_json(await connection.queue.get())
        except Exception:
            pass
        finally:
            async with self._lock:
                if self._clients.get(websocket) is connection:
                    del self._clients[websocket]
                    ws_clients.set(len(self._clients))

    @staticmethod
    async def _close_slow_client(websocket: WebSocket) -> None:
        try:
            await websocket.close(code=1013, reason="outgoing queue full")
        except Exception:
            pass

    def _envelope(self, message: LiveMessage) -> dict[str, object]:
        self._stream_sequence += 1
        return {
            "type": message.type,
            "payload": message.payload,
            "stream_sequence": self._stream_sequence,
        }


def create_app(
    store: OpportunityStore,
    book_manager: OrderBookManager,
    broadcaster: LiveBroadcaster,
    adapters: Iterable[ExchangeAdapter] = (),
    expected_pairs: Iterable[tuple[str, str]] = (),
    started_at_holder: list[int] | None = None,
    background_failures: Callable[[], list[dict[str, str]]] = lambda: [],
    cors_allowed_origins: Iterable[str] = (),
) -> FastAPI:
    app = FastAPI(title="Cross-Exchange Arbitrage Detector")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_allowed_origins),
        allow_methods=["GET"],
        allow_headers=[],
    )
    adapter_list = list(adapters)
    tracked_pairs = list(expected_pairs)
    started_at: list[int] = started_at_holder if started_at_holder is not None else [time.time_ns()]

    @app.get("/api/opportunities/recent")
    async def recent_opportunities(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        rows = await store.recent(limit=limit)
        return [{**row, "timestamp_ns": str(row["timestamp_ns"])} for row in rows]

    @app.get("/api/stats")
    async def stats(window: Window = "1h") -> dict[str, object]:
        return await store.stats(window_to_ns(window))

    @app.get("/api/system/overview")
    async def system_overview() -> dict[str, object]:
        all_time = await store.extended_stats(window_ns=None)
        return {
            "started_at_ns": str(started_at[0]),
            "uptime_seconds": max(0, (time.time_ns() - started_at[0]) // 1_000_000_000),
            "all_time_count": all_time["count"],
            "all_time_max_spread_pct": all_time["max_spread_pct"],
            "all_time_peak_minute": serialize_peak(await store.peak_minute(window_ns=None)),
        }

    @app.get("/api/system/stats")
    async def system_stats(window: Window = "1h") -> dict[str, object]:
        window_ns = window_to_ns(window)
        extended = await store.extended_stats(window_ns=window_ns)
        peak = await store.peak_minute(window_ns=window_ns)
        return {"window": window, **extended, "peak_minute": serialize_peak(peak)}

    @app.get("/api/system/timeseries")
    async def system_timeseries(
        window: Window = "1h",
        bucket_seconds: Annotated[int, Query(ge=1, le=86_400)] = 60,
    ) -> dict[str, object]:
        window_ns = window_to_ns(window)
        points = await store.timeseries(window_ns=window_ns, bucket_seconds=bucket_seconds)
        serialized_points = [
            {**point, "bucket_start_ns": str(point["bucket_start_ns"])} for point in points
        ]
        return {
            "window": window,
            "bucket_seconds": bucket_seconds,
            "points": serialized_points,
        }

    @app.get("/api/pairs")
    async def pairs() -> list[dict[str, str]]:
        return [
            {"exchange": exchange, "pair": pair} for exchange, pair in book_manager.known_pairs()
        ]

    @app.get("/api/book-status")
    async def book_status() -> list[dict[str, object]]:
        return [status.as_payload() for status in book_manager.eligibility_for(tracked_pairs)]

    @app.get("/api/adapters")
    async def adapter_status() -> list[dict[str, str | int | bool | None]]:
        now_ns = time.time_ns()
        return [adapter.status_snapshot(now_ns).as_payload() for adapter in adapter_list]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        disconnected = [adapter.name for adapter in adapter_list if not adapter.connected]
        task_failures = background_failures()
        stale_pairs: list[dict[str, object]] = []
        for status in book_manager.eligibility_for(tracked_pairs):
            if not status.eligible:
                stale_pairs.append(status.as_payload())

        ready = not disconnected and not stale_pairs and not task_failures
        payload: dict[str, object] = {
            "status": "ready" if ready else "not_ready",
            "disconnected_adapters": disconnected,
            "stale_pairs": stale_pairs,
            "background_task_failures": task_failures,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        for status in book_manager.eligibility_for(tracked_pairs):
            book_eligible.labels(exchange=status.exchange, pair=status.pair).set(
                1 if status.eligible else 0
            )
            if status.age_ns is not None:
                book_staleness_seconds.labels(exchange=status.exchange, pair=status.pair).set(
                    status.age_ns / 1_000_000_000
                )
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.websocket("/ws/live")
    async def live_updates(websocket: WebSocket) -> None:
        def current_state() -> LiveMessage:
            statuses = book_manager.eligibility_for(tracked_pairs)
            books = [
                top.as_payload()
                for status in statuses
                if status.eligible
                if (top := book_manager.top_of_book(status.exchange, status.pair)) is not None
            ]
            return LiveMessage(
                type="state_snapshot",
                payload={
                    "books": books,
                    "statuses": [status.as_payload() for status in statuses],
                },
            )

        await broadcaster.connect(websocket, current_state)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # A queue overflow can close the socket from its sender task while
            # this receiver is still running. Unexpected runtime errors surface.
            if websocket.application_state is not WebSocketState.DISCONNECTED:
                raise
        finally:
            await broadcaster.disconnect(websocket)

    return app
