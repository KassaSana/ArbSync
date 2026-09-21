"""Structural interfaces between pipeline components.

Each protocol names the subset of a concrete component that its consumers
actually call, so replay and test doubles can stand in without casts or
``type: ignore`` and mypy still checks every call against the real
signature.
"""

from __future__ import annotations

from typing import Any, Protocol

from arb.capture import SnapshotProvenance
from arb.types import MarketEvent, OpportunityEpisode


class EpisodeSink(Protocol):
    """Where opened and closed episodes go; `OpportunityStore` is the live one."""

    async def enqueue(self, episode: OpportunityEpisode) -> bool: ...


class CaptureSink(Protocol):
    """Receiver of exact inbound exchange traffic; `CaptureWriter` is the live one.

    Every record call is synchronous and must never block ingestion.
    """

    def record_ws(
        self,
        exchange: str,
        raw: str,
        events: list[MarketEvent],
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> bool: ...

    def record_snapshot(
        self,
        exchange: str,
        url: str,
        payload: dict[str, Any],
        *,
        provenance: SnapshotProvenance | None = None,
    ) -> bool: ...

    def record_connection(
        self,
        exchange: str,
        connected: bool,
        generation: int,
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
        reason: str | None = None,
    ) -> bool: ...


class LiveSocket(Protocol):
    """The part of a WebSocket the broadcaster drives."""

    async def accept(self) -> None: ...

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...
