"""Bounded, non-blocking capture of real exchange traffic to disk.

Each received WebSocket text and each REST snapshot payload is stored as one
JSON object per line, with the local wall-clock and monotonic receive stamps
recorded alongside it. The writer mirrors `OpportunityStore`: a bounded queue
drained off the ingestion path, so a slow disk drops capture frames instead of
blocking book updates. Captures live in files, never in SQLite.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, cast

import structlog

from arb.metrics import (
    capture_drops_total,
    capture_frames_total,
    capture_unflushed_frames,
)
from arb.types import MarketEvent

logger = structlog.get_logger(__name__)

CAPTURE_FORMAT = "arbsync-capture"
CAPTURE_VERSION = 2
WRITE_BATCH_SIZE = 256

FrameKind = Literal["ws", "snapshot", "connection"]


class CaptureError(ValueError):
    """A capture file cannot provide trustworthy replay input."""

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class EventSummary:
    """What production parsing produced for one captured message.

    Recorded so the capture documents exchange timestamps and sequence
    identifiers explicitly; replay still re-parses the raw text through the
    real adapters rather than trusting this summary.
    """

    pair: str
    kind: str
    sequence: int
    timestamp_ns: int
    first_sequence: int | None
    last_sequence: int | None


def summarize_event(event: MarketEvent) -> EventSummary:
    return EventSummary(
        pair=event.pair,
        kind=event.kind.value,
        sequence=event.sequence,
        timestamp_ns=event.timestamp_ns,
        first_sequence=event.exchange_first_sequence,
        last_sequence=event.exchange_last_sequence,
    )


@dataclass(frozen=True)
class CaptureHeader:
    exchanges: dict[str, list[str]]
    started_wall_ns: int
    version: int = CAPTURE_VERSION
    integrity: Literal["lossless", "lossy", "legacy_unknown", "unknown"] = "unknown"
    provenance: Literal["complete", "legacy_v1_unavailable", "unknown"] = "unknown"


@dataclass(frozen=True)
class ConnectionBoundary:
    connected: bool
    generation: int
    reason: str | None = None


@dataclass(frozen=True)
class SnapshotProvenance:
    purpose: str
    pair: str
    connection_generation: int
    request_wall_ns: int | None
    request_mono_ns: int | None
    response_wall_ns: int
    response_mono_ns: int


@dataclass(frozen=True)
class CaptureFrame:
    exchange: str
    kind: FrameKind
    wall_ns: int
    mono_ns: int
    raw: str | None
    payload: dict[str, Any] | None
    url: str | None
    events: tuple[EventSummary, ...]
    connection: ConnectionBoundary | None = None
    snapshot_provenance: SnapshotProvenance | None = None


def _open_capture(path: Path, mode: str) -> IO[str]:
    if path.suffix == ".gz":
        return cast("IO[str]", gzip.open(path, mode + "t", encoding="utf-8"))
    return path.open(mode, encoding="utf-8")


class CaptureWriter:
    """Append capture frames to a JSONL file from a bounded queue.

    `record_ws` and `record_snapshot` are synchronous and never block: a full
    or closed writer drops the frame and counts it by reason. `run` owns the
    file and must be driven as a background task; `close` stops it and writes
    the footer that marks the file complete.
    """

    def __init__(
        self,
        path: str | Path,
        exchanges: dict[str, list[str]],
        *,
        queue_maxsize: int = 10_000,
    ) -> None:
        if queue_maxsize <= 0:
            raise ValueError(f"queue_maxsize must be greater than zero; got {queue_maxsize!r}")
        self._path = Path(path)
        self._exchanges = dict(exchanges)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._closed = False
        self._failed = False
        self._worker_started = False
        self._attempted_count = 0
        self._accepted_count = 0
        self._flushed_count = 0
        self._frame_count = 0
        self._counts: dict[str, int] = {}
        self._attempted_by_kind: dict[str, int] = {}
        self._accepted_by_kind: dict[str, int] = {}
        self._dropped_by_reason: dict[str, dict[str, int]] = {}
        capture_unflushed_frames.set(0)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def record_ws(
        self,
        exchange: str,
        raw: str,
        events: list[MarketEvent],
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> bool:
        summaries = [summarize_event(event) for event in events]
        return self._enqueue(
            {
                "exchange": exchange,
                "kind": "ws",
                "wall_ns": time.time_ns() if wall_ns is None else wall_ns,
                "mono_ns": time.monotonic_ns() if mono_ns is None else mono_ns,
                "raw": raw,
                "events": [
                    {
                        "pair": summary.pair,
                        "kind": summary.kind,
                        "sequence": summary.sequence,
                        "timestamp_ns": summary.timestamp_ns,
                        "first_sequence": summary.first_sequence,
                        "last_sequence": summary.last_sequence,
                    }
                    for summary in summaries
                ],
            }
        )

    def record_snapshot(
        self,
        exchange: str,
        url: str,
        payload: dict[str, Any],
        *,
        provenance: SnapshotProvenance | None = None,
    ) -> bool:
        return self._enqueue(
            {
                "exchange": exchange,
                "kind": "snapshot",
                "wall_ns": time.time_ns(),
                "mono_ns": time.monotonic_ns(),
                "url": url,
                "payload": payload,
                "snapshot_provenance": (
                    None
                    if provenance is None
                    else {
                        "purpose": provenance.purpose,
                        "pair": provenance.pair,
                        "connection_generation": provenance.connection_generation,
                        "request_wall_ns": provenance.request_wall_ns,
                        "request_mono_ns": provenance.request_mono_ns,
                        "response_wall_ns": provenance.response_wall_ns,
                        "response_mono_ns": provenance.response_mono_ns,
                    }
                ),
            }
        )

    def record_connection(
        self,
        exchange: str,
        connected: bool,
        generation: int,
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
        reason: str | None = None,
    ) -> bool:
        return self._enqueue(
            {
                "exchange": exchange,
                "kind": "connection",
                "wall_ns": time.time_ns() if wall_ns is None else wall_ns,
                "mono_ns": time.monotonic_ns() if mono_ns is None else mono_ns,
                "connected": connected,
                "connection_generation": generation,
                "reason": reason,
            }
        )

    def _drop(self, reason: str, kind: str) -> None:
        by_kind = self._dropped_by_reason.setdefault(reason, {})
        by_kind[kind] = by_kind.get(kind, 0) + 1
        capture_drops_total.labels(reason=reason).inc()

    def _enqueue(self, frame: dict[str, Any]) -> bool:
        kind = str(frame["kind"])
        self._attempted_count += 1
        self._attempted_by_kind[kind] = self._attempted_by_kind.get(kind, 0) + 1
        if self._failed:
            self._drop("writer_failed", kind)
            return False
        if self._closed:
            self._drop("writer_closed", kind)
            return False
        try:
            self._queue.put_nowait(json.dumps(frame))
        except asyncio.QueueFull:
            self._drop("queue_full", kind)
            return False
        except (TypeError, ValueError):
            self._drop("serialization_error", kind)
            return False
        self._accepted_count += 1
        self._frame_count += 1
        exchange = str(frame["exchange"])
        self._counts[exchange] = self._counts.get(exchange, 0) + 1
        self._accepted_by_kind[kind] = self._accepted_by_kind.get(kind, 0) + 1
        capture_frames_total.labels(exchange=exchange, kind=kind).inc()
        capture_unflushed_frames.set(self._accepted_count - self._flushed_count)
        return True

    def _footer(self, *, status: str, failure: str | None = None) -> dict[str, Any]:
        footer: dict[str, Any] = {
            "type": "footer",
            "frame_count": self._frame_count,
            "counts": self._counts,
            "clean": status == "clean",
            "status": status,
            "frame_counts": {
                "attempted": self._attempted_count,
                "accepted": self._accepted_count,
                "flushed": self._flushed_count,
                "attempted_by_kind": self._attempted_by_kind,
                "accepted_by_kind": self._accepted_by_kind,
                "dropped": self._dropped_by_reason,
            },
        }
        if failure is not None:
            footer["failure"] = failure
        return footer

    async def run(self) -> None:
        self._worker_started = True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "header",
            "format": CAPTURE_FORMAT,
            "version": CAPTURE_VERSION,
            "exchanges": self._exchanges,
            "started_wall_ns": time.time_ns(),
        }
        handle: IO[str] | None = None
        try:
            handle = await asyncio.to_thread(_open_capture, self._path, "w")
            await asyncio.to_thread(handle.write, json.dumps(header) + "\n")
            stopped = False
            while not stopped:
                line = await self._queue.get()
                if line is None:
                    break
                lines = [line]
                while len(lines) < WRITE_BATCH_SIZE:
                    try:
                        next_line = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if next_line is None:
                        stopped = True
                        break
                    lines.append(next_line)
                await asyncio.to_thread(handle.writelines, (item + "\n" for item in lines))
                self._flushed_count += len(lines)
                capture_unflushed_frames.set(self._accepted_count - self._flushed_count)
            await asyncio.to_thread(
                handle.write,
                json.dumps(self._footer(status="clean")) + "\n",
            )
        except Exception as exc:
            self._failed = True
            failure = f"{type(exc).__name__}: {exc}"
            logger.exception("capture_writer_failed", path=str(self._path), failure=failure)
            if handle is not None:
                try:
                    await asyncio.to_thread(
                        handle.write,
                        json.dumps(self._footer(status="writer_failed", failure=failure)) + "\n",
                    )
                except Exception:
                    logger.exception("capture_failure_footer_failed", path=str(self._path))
            raise
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            capture_unflushed_frames.set(self._accepted_count - self._flushed_count)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # The sentinel is always enqueued, even if the worker has not been
        # scheduled yet: it waits in the queue and a later `run` still drains
        # to a clean footer. Returning early here would deadlock a `run` that
        # starts after `close`, with no sentinel ever arriving.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            if not self._worker_started:
                logger.error("capture_close_no_worker", path=str(self._path))
                return
            try:
                await asyncio.wait_for(self._queue.put(None), timeout=5.0)
            except TimeoutError:
                logger.error("capture_close_timed_out", path=str(self._path))


def read_capture(
    path: str | Path, *, allow_lossy: bool = False
) -> tuple[CaptureHeader, list[CaptureFrame]]:
    """Read and validate a capture file, rejecting untrustworthy artifacts."""
    location = Path(path)
    try:
        with _open_capture(location, "r") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError as exc:
        raise CaptureError(f"capture file not found: {location}") from exc
    except OSError as exc:
        raise CaptureError(f"cannot read capture file {location}: {exc}") from exc
    if not lines:
        raise CaptureError(f"capture file is empty: {location}")
    try:
        header_raw: Any = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise CaptureError(
            f"capture header is not valid JSON: {location}: {exc}", reason="invalid_header"
        ) from exc
    version = header_raw.get("version") if isinstance(header_raw, dict) else None
    if (
        not isinstance(header_raw, dict)
        or header_raw.get("type") != "header"
        or header_raw.get("format") != CAPTURE_FORMAT
        or version not in (1, CAPTURE_VERSION)
    ):
        raise CaptureError(f"capture header is invalid: {location}", reason="unsupported_version")
    exchanges = header_raw.get("exchanges")
    if not isinstance(exchanges, dict) or not exchanges:
        raise CaptureError(
            f"capture header names no exchanges: {location}", reason="invalid_header"
        )
    try:
        footer_raw: Any = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise CaptureError(
            f"capture missing footer (truncated): {location}: {exc}", reason="missing_footer"
        ) from exc
    if not isinstance(footer_raw, dict) or footer_raw.get("type") != "footer":
        raise CaptureError(
            f"capture missing footer (truncated): {location}", reason="missing_footer"
        )
    if footer_raw.get("status") == "writer_failed":
        raise CaptureError(
            f"capture writer failure: {footer_raw.get('failure', 'unknown failure')}: {location}",
            reason="writer_failure",
        )
    if footer_raw.get("clean") is not True:
        raise CaptureError(f"capture footer is not clean: {location}", reason="writer_failure")
    frame_counts = footer_raw.get("frame_counts")
    dropped_total = 0
    if version == CAPTURE_VERSION and not isinstance(frame_counts, dict):
        raise CaptureError(
            f"capture footer is missing frame counts: {location}", reason="count_mismatch"
        )
    if isinstance(frame_counts, dict):
        dropped = frame_counts.get("dropped", {})
        if isinstance(dropped, dict):
            for counts in dropped.values():
                if isinstance(counts, dict):
                    dropped_total += sum(
                        int(value) for value in counts.values() if isinstance(value, int)
                    )
        accepted = frame_counts.get("accepted")
        attempted = frame_counts.get("attempted")
        flushed = frame_counts.get("flushed")
        if (
            version == CAPTURE_VERSION
            and isinstance(attempted, int)
            and isinstance(accepted, int)
            and attempted != accepted + dropped_total
        ):
            raise CaptureError(
                f"capture footer count mismatch: attempted={attempted} accepted={accepted} dropped={dropped_total}",
                reason="count_mismatch",
            )
        if (
            version == CAPTURE_VERSION
            and isinstance(accepted, int)
            and accepted != footer_raw.get("frame_count")
        ):
            raise CaptureError(
                f"capture footer count mismatch: accepted={accepted} frame_count={footer_raw.get('frame_count')}",
                reason="count_mismatch",
            )
        if isinstance(flushed, int) and flushed > int(accepted or 0):
            raise CaptureError(
                f"capture footer count mismatch: flushed={flushed} accepted={accepted}",
                reason="count_mismatch",
            )
    if dropped_total and not allow_lossy:
        raise CaptureError(
            f"capture declares {dropped_total} dropped frames: {location}",
            reason="declared_frame_loss",
        )
    frames: list[CaptureFrame] = []
    for index, line in enumerate(lines[1:-1], start=2):
        try:
            raw_frame: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptureError(
                f"capture line {index} is not valid JSON: {location}: {exc}"
            ) from exc
        frames.append(_parse_frame(raw_frame, index, location))
    expected = footer_raw.get("frame_count")
    if expected != len(frames):
        raise CaptureError(
            f"capture footer expects {expected} frames but found {len(frames)}: {location}",
            reason="count_mismatch",
        )
    integrity: Literal["lossless", "lossy", "legacy_unknown"] = (
        "legacy_unknown" if version == 1 else "lossy" if dropped_total else "lossless"
    )
    provenance: Literal["complete", "legacy_v1_unavailable", "unknown"]
    if version == 1:
        provenance = "legacy_v1_unavailable"
    else:
        provenance = (
            "complete"
            if all(
                frame.kind != "snapshot" or frame.snapshot_provenance is not None
                for frame in frames
            )
            else "unknown"
        )
    return (
        CaptureHeader(
            exchanges={str(k): [str(s) for s in v] for k, v in exchanges.items()},
            started_wall_ns=int(header_raw.get("started_wall_ns", 0)),
            version=version,
            integrity=integrity,
            provenance=provenance,
        ),
        frames,
    )


def _parse_frame(raw_frame: Any, index: int, location: Path) -> CaptureFrame:
    if not isinstance(raw_frame, dict):
        raise CaptureError(f"capture line {index} is not an object: {location}")
    kind = raw_frame.get("kind")
    if kind not in ("ws", "snapshot", "connection"):
        raise CaptureError(f"capture line {index} has unknown kind {kind!r}: {location}")
    raw = raw_frame.get("raw")
    payload = raw_frame.get("payload")
    if kind == "ws" and not isinstance(raw, str):
        raise CaptureError(f"capture line {index} has no raw message: {location}")
    if kind == "snapshot" and not isinstance(payload, dict):
        raise CaptureError(f"capture line {index} has no snapshot payload: {location}")
    if kind == "connection" and not isinstance(raw_frame.get("connected"), bool):
        raise CaptureError(f"capture line {index} has invalid connection state: {location}")
    summaries: list[EventSummary] = []
    events = raw_frame.get("events", [])
    if not isinstance(events, list):
        raise CaptureError(f"capture line {index} has invalid events: {location}")
    for entry in events:
        if not isinstance(entry, dict):
            raise CaptureError(f"capture line {index} has invalid event entry: {location}")
        try:
            summaries.append(
                EventSummary(
                    pair=str(entry["pair"]),
                    kind=str(entry["kind"]),
                    sequence=int(entry["sequence"]),
                    timestamp_ns=int(entry["timestamp_ns"]),
                    first_sequence=(
                        None
                        if entry.get("first_sequence") is None
                        else int(entry["first_sequence"])
                    ),
                    last_sequence=(
                        None if entry.get("last_sequence") is None else int(entry["last_sequence"])
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError(
                f"capture line {index} has invalid event entry: {location}: {exc}"
            ) from exc
    try:
        wall_ns = int(raw_frame["wall_ns"])
        mono_ns = int(raw_frame["mono_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureError(
            f"capture line {index} has invalid timestamps: {location}: {exc}"
        ) from exc
    url = raw_frame.get("url")
    connection: ConnectionBoundary | None = None
    if kind == "connection":
        try:
            connection = ConnectionBoundary(
                connected=bool(raw_frame["connected"]),
                generation=int(raw_frame["connection_generation"]),
                reason=(None if raw_frame.get("reason") is None else str(raw_frame["reason"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError(
                f"capture line {index} has invalid connection metadata: {location}: {exc}"
            ) from exc
    snapshot_provenance: SnapshotProvenance | None = None
    raw_provenance = raw_frame.get("snapshot_provenance")
    if raw_provenance is not None:
        if not isinstance(raw_provenance, dict):
            raise CaptureError(f"capture line {index} has invalid snapshot provenance: {location}")
        try:
            snapshot_provenance = SnapshotProvenance(
                purpose=str(raw_provenance["purpose"]),
                pair=str(raw_provenance["pair"]),
                connection_generation=int(raw_provenance["connection_generation"]),
                request_wall_ns=(
                    None
                    if raw_provenance.get("request_wall_ns") is None
                    else int(raw_provenance["request_wall_ns"])
                ),
                request_mono_ns=(
                    None
                    if raw_provenance.get("request_mono_ns") is None
                    else int(raw_provenance["request_mono_ns"])
                ),
                response_wall_ns=int(raw_provenance["response_wall_ns"]),
                response_mono_ns=int(raw_provenance["response_mono_ns"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError(
                f"capture line {index} has invalid snapshot provenance: {location}: {exc}"
            ) from exc
    return CaptureFrame(
        exchange=str(raw_frame.get("exchange")),
        kind=kind,
        wall_ns=wall_ns,
        mono_ns=mono_ns,
        raw=raw if isinstance(raw, str) else None,
        payload=payload if isinstance(payload, dict) else None,
        url=str(url) if url is not None else None,
        events=tuple(summaries),
        connection=connection,
        snapshot_provenance=snapshot_provenance,
    )
