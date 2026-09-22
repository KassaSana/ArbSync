from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable, Iterable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketState

from arb.adapters.base import ExchangeAdapter
from arb.broadcast import LiveBroadcaster
from arb.history import (
    HISTORY_ORDER,
    MAX_SQLITE_INTEGER,
    CursorError,
    HistoryCursor,
    HistoryFilters,
    HistoryState,
)
from arb.metrics import book_metrics, render_metrics
from arb.orderbook import OrderBookManager
from arb.persistence import (
    HistoryBudgetExceeded,
    OpportunityStore,
    WindowStats,
    episode_wire_payload,
)
from arb.pricing import DepthSampler
from arb.types import EpisodeCloseReason, LiveMessage

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


NsQuery = Annotated[
    str | None, Query(pattern=r"^[0-9]{1,19}$", description="Unix nanoseconds, decimal")
]
PairQuery = Annotated[str | None, Query(pattern=r"^[A-Za-z0-9]{1,20}-[A-Za-z0-9]{1,20}$")]
ExchangeQuery = Annotated[str | None, Query(pattern=r"^[a-z0-9_]{1,32}$")]
CursorQuery = Annotated[str | None, Query(max_length=512)]

HISTORY_EXPORT_MAX_ROWS = 100_000


# A client that stops reading stalls `send` under transport flow control; after
# this long without progress the export is abandoned and its slot released.
HISTORY_EXPORT_SEND_TIMEOUT_SECONDS = 30.0


class ExportResponse(StreamingResponse):
    """A streaming export that releases its slot however the response ends.

    Starlette neither starts nor closes the body iterator when the client
    disconnects before the first chunk or the first send fails, so a release in
    the generator's own `finally` could never run and every later export would
    be refused. Releasing here covers completion, disconnects, send failures,
    cancellation, and a stalled reader.
    """

    def __init__(
        self,
        content: AsyncGenerator[bytes, None],
        lock: asyncio.Lock,
        *,
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._content = content
        self._lock = lock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def bounded_send(message: Message) -> None:
            await asyncio.wait_for(send(message), HISTORY_EXPORT_SEND_TIMEOUT_SECONDS)

        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            try:
                await self._content.aclose()
            finally:
                self._lock.release()


def history_filters(
    from_ns: NsQuery = None,
    to_ns: NsQuery = None,
    pair: PairQuery = None,
    buy_exchange: ExchangeQuery = None,
    sell_exchange: ExchangeQuery = None,
    close_reason: EpisodeCloseReason | None = None,
    state: HistoryState | None = None,
) -> HistoryFilters:
    """Shared filter parameters for history pages and export; 422 on invalid values."""
    bounds = [None if value is None else int(value) for value in (from_ns, to_ns)]
    if any(value is not None and value > MAX_SQLITE_INTEGER for value in bounds):
        raise HTTPException(status_code=422, detail="time bounds must fit a SQLite integer")
    try:
        return HistoryFilters(
            from_ns=bounds[0],
            to_ns=bounds[1],
            pair=pair,
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            close_reason=close_reason,
            state=state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def decode_cursor(token: str | None, filters: HistoryFilters) -> HistoryCursor | None:
    if token is None:
        return None
    try:
        return HistoryCursor.decode(token, filters)
    except CursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_app(
    store: OpportunityStore,
    book_manager: OrderBookManager,
    broadcaster: LiveBroadcaster,
    adapters: Iterable[ExchangeAdapter] = (),
    expected_pairs: Iterable[tuple[str, str]] = (),
    started_at_ns: int | None = None,
    background_failures: Callable[[], list[dict[str, str]]] = lambda: [],
    cors_allowed_origins: Iterable[str] = (),
    depth_sampler: DepthSampler | None = None,
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
    started_at: int = time.time_ns() if started_at_ns is None else started_at_ns
    # One export at a time: each streams at most HISTORY_EXPORT_MAX_ROWS rows in
    # bounded pages, and serializing them caps the read load an export adds.
    history_export_lock = asyncio.Lock()
    app.state.history_export_lock = history_export_lock

    @app.get("/api/opportunities/recent")
    async def recent_opportunities(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        rows = await store.recent(limit=limit)
        return [episode_wire_payload(row) for row in rows]

    @app.get("/api/opportunities")
    async def opportunity_history(
        filters: Annotated[HistoryFilters, Depends(history_filters)],
        cursor: CursorQuery = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        """Filtered episodes, newest first, with an opaque cursor for the next page."""
        start = decode_cursor(cursor, filters)
        try:
            page = await store.history_page(filters, start, limit)
        except HistoryBudgetExceeded as exc:
            raise HTTPException(
                status_code=503, detail="query budget exceeded; narrow the time range or filters"
            ) from exc
        return {
            "items": [episode_wire_payload(item) for item in page.items],
            "next_cursor": None if page.next_cursor is None else page.next_cursor.encode(),
            "order": HISTORY_ORDER,
        }

    @app.get("/api/opportunities/export")
    async def opportunity_export(
        filters: Annotated[HistoryFilters, Depends(history_filters)],
        cursor: CursorQuery = None,
        max_rows: Annotated[int, Query(ge=1, le=HISTORY_EXPORT_MAX_ROWS)] = 10_000,
    ) -> ExportResponse:
        """Stream filtered episodes as JSON Lines ending with a typed `end` record.

        A download without the `end` record was cut off. A truncated export
        (row cap or query budget) carries a `next_cursor` to resume from.
        """
        start = decode_cursor(cursor, filters)
        if history_export_lock.locked():
            raise HTTPException(status_code=429, detail="another export is in progress")
        await history_export_lock.acquire()

        async def lines() -> AsyncGenerator[bytes, None]:
            rows = 0
            resume = start
            error: str | None = None
            try:
                async for chunk in store.history_export_chunks(filters, start, max_rows):
                    rows += chunk.rows
                    resume = chunk.next_cursor
                    yield chunk.lines
            except HistoryBudgetExceeded:
                error = "query_budget_exceeded"
            end = {
                "type": "end",
                "order": HISTORY_ORDER,
                "rows": rows,
                "truncated": error is not None or resume is not None,
                "next_cursor": None if resume is None else resume.encode(),
                "error": error,
            }
            yield (json.dumps(end, separators=(",", ":")) + "\n").encode()

        return ExportResponse(
            lines(),
            history_export_lock,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="arbsync-opportunities.jsonl"'},
        )

    @app.get("/api/stats")
    async def stats(window: Window = "1h") -> WindowStats:
        return await store.stats(window_to_ns(window))

    @app.get("/api/system/overview")
    async def system_overview() -> dict[str, object]:
        all_time = await store.extended_stats(window_ns=None)
        return {
            "started_at_ns": str(started_at),
            "uptime_seconds": max(0, (time.time_ns() - started_at) // 1_000_000_000),
            "all_time_count": all_time["count"],
            "all_time_max_spread_pct": all_time["max_spread_pct"],
            "all_time_peak_minute": serialize_peak(await store.peak_minute(window_ns=None)),
            "open_count": await store.open_count(),
            "all_time_lifetime": await store.lifetimes(window_ns=None),
        }

    @app.get("/api/system/stats")
    async def system_stats(window: Window = "1h") -> dict[str, object]:
        window_ns = window_to_ns(window)
        extended = await store.extended_stats(window_ns=window_ns)
        peak = await store.peak_minute(window_ns=window_ns)
        lifetime = await store.lifetimes(window_ns=window_ns)
        return {
            "window": window,
            **extended,
            "peak_minute": serialize_peak(peak),
            "lifetime": lifetime,
        }

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

    @app.get("/api/pricing/depth")
    async def depth_pricing(pair: str | None = None) -> dict[str, object]:
        """Walk each eligible book now; an ineligible book simply has no quotes."""
        if depth_sampler is None:
            raise HTTPException(status_code=404, detail="depth pricing is not configured")
        if pair is None:
            quotes = [
                quote
                for exchange, known_pair in book_manager.known_pairs()
                for quote in depth_sampler.quote(exchange, known_pair)
            ]
        else:
            quotes = depth_sampler.quote_pair(pair)
        return {
            "notionals": [str(notional) for notional in depth_sampler.notionals],
            "quotes": [quote.as_payload() for quote in quotes],
            "routes": [
                route.as_payload()
                for known_pair in (
                    {pair} if pair is not None else {p for _, p in book_manager.known_pairs()}
                )
                for route in depth_sampler.route_prices(known_pair)
            ],
        }

    @app.get("/api/pricing/fill-rates")
    async def fill_rates(
        from_ns: NsQuery = None,
        to_ns: NsQuery = None,
        exchange: ExchangeQuery = None,
        pair: PairQuery = None,
    ) -> dict[str, object]:
        """How often each venue could fill each notional across periodic samples.

        Without a window this is the running session, including its open
        minute. With `from_ns` and `to_ns` it sums the persisted minute buckets
        of every session inside the whole minutes of `[from_ns, to_ns)`.
        """
        if depth_sampler is None:
            raise HTTPException(status_code=404, detail="depth pricing is not configured")
        if from_ns is None and to_ns is None:
            recorder = depth_sampler.fill_rates
            session = recorder.session
            rows = [
                row
                for row in recorder.rows()
                if (exchange is None or row["exchange"] == exchange)
                and (pair is None or row["pair"] == pair)
            ]
            return {
                "sample_interval_seconds": depth_sampler.interval_seconds,
                "samples": recorder.samples,
                "missed_samples": recorder.missed_samples,
                "notionals": [str(notional) for notional in depth_sampler.notionals],
                "session": None if session is None else session.payload(),
                "config": depth_sampler.fill_config.payload(),
                "rows": rows,
            }
        if from_ns is None or to_ns is None:
            raise HTTPException(status_code=422, detail="from_ns and to_ns must be given together")
        start, end = int(from_ns), int(to_ns)
        if max(start, end) > MAX_SQLITE_INTEGER:
            raise HTTPException(status_code=422, detail="time bounds must fit a SQLite integer")
        if start >= end:
            raise HTTPException(status_code=422, detail="from_ns must be before to_ns")
        try:
            window = await store.fill_rate_window(start, end, exchange=exchange, pair=pair)
        except HistoryBudgetExceeded as exc:
            raise HTTPException(
                status_code=503, detail="query budget exceeded; narrow the time range or filters"
            ) from exc
        return window.payload()

    @app.get("/api/pairs")
    async def pairs() -> list[dict[str, str]]:
        """Every tracked pair, whether or not a book for it has received data yet.

        Reporting only initialized books meant a dashboard opened before the
        first snapshots arrived saw an empty table and had no reason to ask
        again. Books seen but not configured are still included, so an
        unexpected symbol from an exchange remains visible.
        """
        tracked = sorted({*tracked_pairs, *book_manager.known_pairs()})
        return [{"exchange": exchange, "pair": pair} for exchange, pair in tracked]

    @app.get("/api/book-status")
    async def book_status() -> list[dict[str, object]]:
        return [status.as_payload() for status in book_manager.eligibility_for(tracked_pairs)]

    @app.get("/api/adapters")
    async def adapter_status() -> list[dict[str, str | int | bool | None]]:
        now_ns = time.time_ns()
        return [adapter.status_snapshot(now_ns).as_payload() for adapter in adapter_list]

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "Cross-Exchange Arbitrage Detector",
            "status": "ok",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/healthz",
            "readiness": "/readyz",
            "live_updates": "/ws/live",
        }

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
            metrics_for_book = book_metrics(status.exchange, status.pair)
            metrics_for_book.eligible.set(1 if status.eligible else 0)
            if status.age_ns is not None:
                metrics_for_book.staleness.set(status.age_ns / 1_000_000_000)
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
