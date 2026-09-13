from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Annotated, Literal

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.websockets import WebSocketState

from arb.adapters.base import ExchangeAdapter
from arb.broadcast import LiveBroadcaster
from arb.metrics import book_metrics, render_metrics
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


def create_app(
    store: OpportunityStore,
    book_manager: OrderBookManager,
    broadcaster: LiveBroadcaster,
    adapters: Iterable[ExchangeAdapter] = (),
    expected_pairs: Iterable[tuple[str, str]] = (),
    started_at_ns: int | None = None,
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
    started_at: int = time.time_ns() if started_at_ns is None else started_at_ns

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
            "started_at_ns": str(started_at),
            "uptime_seconds": max(0, (time.time_ns() - started_at) // 1_000_000_000),
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
