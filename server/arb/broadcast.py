from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from arb.metrics import ws_client_queue_overflows_total, ws_clients, ws_sender_failures_total
from arb.types import BookEligibility, LiveMessage

logger = structlog.get_logger(__name__)

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
    pending_baseline: int
    sender_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class _PendingMessage:
    generation: int
    message: LiveMessage


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
        self._pending: dict[tuple[str, str, str], _PendingMessage] = {}
        self._pending_generation = 0
        self._last_sent: dict[tuple[str, str, str], tuple[object, ...]] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    async def connect(
        self,
        websocket: WebSocket,
        initial_state: Callable[[], LiveMessage] | None = None,
    ) -> None:
        await websocket.accept()
        async with self._lock:
            connection = _ClientConnection(
                asyncio.Queue(maxsize=self._queue_maxsize),
                pending_baseline=self._pending_generation,
            )
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
        self._pending[(message.type, exchange, pair)] = self._pending_message(message)
        self._ensure_flush_loop()

    async def broadcast_status(self, status: BookEligibility, *, immediate: bool) -> None:
        """Publish a book status, building its payload only if it will be sent.

        Status is unchanged for the overwhelming majority of events, and
        `as_payload` walks nine fields and divides two ages. Comparing the
        status object directly keeps that work off the path whose entire
        purpose is to discard the message.
        """
        key = ("book_status", status.exchange, status.pair)
        if immediate:
            self._pending.pop(("top_of_book", status.exchange, status.pair), None)
            self._pending.pop(key, None)
        signature = status.display_signature()
        if self._last_sent.get(key) == signature:
            return
        self._last_sent[key] = signature
        message = LiveMessage(type="book_status", payload=status.as_payload())
        if immediate or self._coalesce_interval <= 0:
            await self.broadcast(message)
            return
        self._pending[key] = self._pending_message(message)
        self._ensure_flush_loop()

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
        await self._deliver_pending(list(pending.values()))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._coalesce_interval)
            if not self._pending:
                continue
            await self.flush()

    def _ensure_flush_loop(self) -> None:
        # After aclose the loop must not be respawned: a late coalesced message
        # would otherwise leave a flush task running that nothing cancels.
        if self._closed:
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def aclose(self) -> None:
        """Stop coalescing and deliver anything still queued.

        Messages coalesced after this point are held in the pending map and
        delivered by the next explicit flush, never by a respawned loop.
        """
        self._closed = True
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

    async def _deliver_pending(self, messages: list[_PendingMessage]) -> None:
        """Deliver coalesced entries only to clients older than those entries.

        A new client's snapshot is an authoritative baseline. Entries already
        pending when that baseline was created are represented by the snapshot
        and must not be replayed after it. Existing clients still need those
        entries, while entries queued after connection go to every client.
        """
        dropped: list[tuple[WebSocket, _ClientConnection]] = []
        async with self._lock:
            if not self._clients:
                return
            payloads = [
                (pending.generation, self._envelope(pending.message)) for pending in messages
            ]
            for client, connection in list(self._clients.items()):
                try:
                    for generation, payload in payloads:
                        if generation > connection.pending_baseline:
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

    def _pending_message(self, message: LiveMessage) -> _PendingMessage:
        self._pending_generation += 1
        return _PendingMessage(self._pending_generation, message)

    async def _send_messages(self, websocket: WebSocket, connection: _ClientConnection) -> None:
        message_type = "unknown"
        stream_sequence: int | None = None
        try:
            while True:
                payload = await connection.queue.get()
                payload_type = payload.get("type")
                message_type = payload_type if isinstance(payload_type, str) else "unknown"
                payload_sequence = payload.get("stream_sequence")
                stream_sequence = payload_sequence if isinstance(payload_sequence, int) else None
                await websocket.send_json(payload)
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        except Exception as exc:
            ws_sender_failures_total.inc()
            client = getattr(websocket, "client", None)
            logger.error(
                "websocket_sender_failed",
                exception_type=type(exc).__name__,
                client_host=getattr(client, "host", None),
                client_port=getattr(client, "port", None),
                message_type=message_type,
                stream_sequence=stream_sequence,
            )
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
