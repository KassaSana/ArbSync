from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import platform
import re
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect


def describe_error(exc: BaseException) -> str:
    """Name the exception type, and its message only when it has one."""
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def process_rss_bytes(pid: int) -> int | None:
    if sys.platform == "win32":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        query_information = 0x0400
        process = ctypes.windll.kernel32.OpenProcess(query_information, False, pid)
        if not process:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            success = ctypes.windll.psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb
            )
            return int(counters.WorkingSetSize) if success else None
        finally:
            ctypes.windll.kernel32.CloseHandle(process)

    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        for line in status_path.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return None


@dataclass
class AdapterObservation:
    first_reconnects: int | None = None
    last_reconnects: int = 0
    first_gaps: int | None = None
    last_gaps: int = 0
    max_message_age_ms: int | None = None
    last_error: str | None = None
    first_events: int | None = None
    last_events: int = 0

    def observe(self, payload: dict[str, Any], event_count: int | None) -> None:
        reconnects = int(payload["reconnect_count"])
        gaps = int(payload["gap_count"])
        if self.first_reconnects is None:
            self.first_reconnects = reconnects
        if self.first_gaps is None:
            self.first_gaps = gaps
        self.last_reconnects = reconnects
        self.last_gaps = gaps
        age = payload.get("last_message_age_ms")
        if age is not None:
            self.max_message_age_ms = max(self.max_message_age_ms or 0, int(age))
        self.last_error = payload.get("last_error")
        if event_count is not None:
            if self.first_events is None:
                self.first_events = event_count
            self.last_events = event_count


@dataclass
class BookObservation:
    eligible_samples: int = 0
    ineligible_reasons: Counter[str] = field(default_factory=Counter)
    ages_ms: list[int] = field(default_factory=list)
    last_eligible: bool | None = None
    ineligible_since: float | None = None
    recovery_seconds: list[float] = field(default_factory=list)

    def observe(self, payload: dict[str, Any], elapsed_seconds: float) -> None:
        eligible = bool(payload["eligible"])
        if eligible:
            self.eligible_samples += 1
        else:
            self.ineligible_reasons[str(payload.get("reason") or "unknown")] += 1
        age = payload.get("age_ms")
        if age is not None:
            self.ages_ms.append(int(age))

        if self.last_eligible is True and not eligible:
            self.ineligible_since = elapsed_seconds
        elif self.last_eligible is False and eligible and self.ineligible_since is not None:
            self.recovery_seconds.append(elapsed_seconds - self.ineligible_since)
            self.ineligible_since = None
        self.last_eligible = eligible


@dataclass(frozen=True)
class SamplingGap:
    started_at: str
    ended_at: str
    duration_seconds: float


LIVE_MESSAGE_TYPES = {"state_snapshot", "top_of_book", "book_status", "opportunity"}


@dataclass
class WebSocketObservation:
    url: str
    connections: int = 0
    disconnects: int = 0
    frames: int = 0
    frames_by_type: Counter[str] = field(default_factory=Counter)
    invalid_frames: int = 0
    sequence_gaps: int = 0
    max_disconnected_seconds: float = 0
    connected: bool = False
    disconnected_since: float | None = None
    last_error: str | None = None

    def disconnected_seconds(self, now: float) -> float:
        if self.connected or self.disconnected_since is None:
            return 0
        return max(0, now - self.disconnected_since)


def live_websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
    if scheme is None or not parsed.netloc:
        raise ValueError(f"base URL must be an absolute HTTP(S) URL; got {base_url!r}")
    base_path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, f"{base_path}/ws/live", "", ""))


def validate_live_frame(
    raw: str | bytes, *, first_frame: bool, previous_sequence: int | None
) -> tuple[str, int, bool]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("live frame must be a JSON object")
    message_type = payload.get("type")
    if message_type not in LIVE_MESSAGE_TYPES:
        raise ValueError(f"unsupported live message type {message_type!r}")
    if first_frame and message_type != "state_snapshot":
        raise ValueError("first live frame must be state_snapshot")
    if not isinstance(payload.get("payload"), dict):
        raise ValueError("live frame payload must be an object")
    sequence = payload.get("stream_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("live frame stream_sequence must be a positive integer")
    sequence_gap = previous_sequence is not None and sequence != previous_sequence + 1
    return str(message_type), sequence, sequence_gap


async def observe_websocket(
    observation: WebSocketObservation,
    ready: asyncio.Event,
    *,
    connector: Callable[..., Any] = websocket_connect,
) -> None:
    loop = asyncio.get_running_loop()
    backoff = 1.0
    while True:
        try:
            async with connector(
                observation.url,
                open_timeout=10,
                close_timeout=5,
                max_size=10_000_000,
            ) as websocket:
                observation.connections += 1
                if observation.disconnected_since is not None:
                    observation.max_disconnected_seconds = max(
                        observation.max_disconnected_seconds,
                        loop.time() - observation.disconnected_since,
                    )
                observation.connected = True
                observation.disconnected_since = None
                observation.last_error = None
                backoff = 1.0
                first_frame = True
                previous_sequence: int | None = None
                async for raw in websocket:
                    try:
                        message_type, sequence, sequence_gap = validate_live_frame(
                            raw,
                            first_frame=first_frame,
                            previous_sequence=previous_sequence,
                        )
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                        observation.invalid_frames += 1
                        observation.last_error = describe_error(exc)
                        continue
                    if sequence_gap:
                        observation.sequence_gaps += 1
                    observation.frames += 1
                    observation.frames_by_type[message_type] += 1
                    previous_sequence = sequence
                    first_frame = False
                    ready.set()
                observation.disconnects += 1
                observation.last_error = "connection closed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            observation.disconnects += 1
            observation.last_error = describe_error(exc)
        finally:
            observation.connected = False
            observation.disconnected_since = observation.disconnected_since or loop.time()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


@dataclass
class SoakReport:
    started_at: str
    duration_requested_seconds: float
    sample_interval_seconds: float
    samples: int = 0
    ready_samples: int = 0
    http_failures: list[str] = field(default_factory=list)
    adapters: dict[str, AdapterObservation] = field(default_factory=dict)
    books: dict[str, BookObservation] = field(default_factory=dict)
    rss_bytes: list[int] = field(default_factory=list)
    opportunity_start: int | None = None
    opportunity_end: int | None = None
    background_failures: dict[str, str] = field(default_factory=dict)
    ended_at: str | None = None
    actual_duration_seconds: float = 0
    completed: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    counter_start: dict[str, int] = field(default_factory=dict)
    counter_last: dict[str, int] = field(default_factory=dict)
    counter_resets: int = 0
    process_restarts: int = 0
    process_started_at: str | None = None
    missing_rss_samples: int = 0
    expected_books: set[str] = field(default_factory=set)
    missing_book_samples: Counter[str] = field(default_factory=Counter)
    max_sample_gap_seconds: float | None = None
    sampling_gaps: list[SamplingGap] = field(default_factory=list)
    interrupted: bool = False
    interruption_reasons: list[str] = field(default_factory=list)
    websocket: WebSocketObservation | None = None

    def observe(
        self,
        *,
        adapters: list[dict[str, Any]],
        books: list[dict[str, Any]],
        readiness: dict[str, Any],
        overview: dict[str, Any],
        event_counts: dict[str, int],
        elapsed_seconds: float,
        rss: int | None,
        counters: dict[str, int] | None = None,
    ) -> None:
        self.samples += 1
        observed_books = {f"{book['exchange']}:{book['pair']}" for book in books}
        self.missing_book_samples.update(self.expected_books - observed_books)
        if readiness.get("status") == "ready":
            self.ready_samples += 1
        for failure in readiness.get("background_task_failures", []):
            self.background_failures[str(failure["task"])] = str(failure["error"])
        count = int(overview["all_time_count"])
        if self.opportunity_start is None:
            self.opportunity_start = count
        self.opportunity_end = count
        if rss is not None:
            self.rss_bytes.append(rss)
        else:
            self.missing_rss_samples += 1
        process_started = overview.get("started_at_ns")
        if process_started is not None:
            if (
                self.process_started_at is not None
                and str(process_started) != self.process_started_at
            ):
                self.process_restarts += 1
            self.process_started_at = str(process_started)
        for name, value in (counters or {}).items():
            # Labeled counters may first appear on their first failure, so their
            # baseline is zero if they were absent from the first sample.
            if name not in self.counter_start:
                self.counter_start[name] = value if self.samples == 1 else 0
            if value < self.counter_last.get(name, 0):
                self.counter_resets += 1
            self.counter_last[name] = value

        for payload in adapters:
            exchange = str(payload["exchange"])
            self.adapters.setdefault(exchange, AdapterObservation()).observe(
                payload, event_counts.get(exchange)
            )
        for payload in books:
            key = f"{payload['exchange']}:{payload['pair']}"
            self.books.setdefault(key, BookObservation()).observe(payload, elapsed_seconds)

    def markdown(self) -> str:
        opportunity_delta = (self.opportunity_end or 0) - (self.opportunity_start or 0)
        ready_pct = 0 if self.samples == 0 else self.ready_samples / self.samples * 100
        status = (
            "interrupted" if self.interrupted else "complete" if self.completed else "in progress"
        )
        lines = [
            f"# Live soak report - {self.started_at[:10]}",
            "",
            f"- Started: `{self.started_at}`",
            f"- Ended: `{self.ended_at}`",
            f"- Requested duration: `{self.duration_requested_seconds:.1f}s`",
            f"- Actual duration: `{self.actual_duration_seconds:.1f}s`",
            f"- Sample interval: `{self.sample_interval_seconds:.1f}s`",
            f"- Status: `{status}`",
            "- Status describes observer completion only; reliability requires review of failures and coverage.",
            f"- Successful samples: `{self.samples}`",
            f"- Ready samples: `{self.ready_samples}/{self.samples}` ({ready_pct:.1f}%)",
            f"- HTTP failures: `{len(self.http_failures)}`",
            f"- Opportunities observed: `{opportunity_delta}`",
            f"- Background task failures: `{len(self.background_failures)}`",
            f"- Observed process restarts: `{self.process_restarts}`",
            f"- Counter resets (invalidate deltas): `{self.counter_resets}`",
            f"- Missing RSS samples: `{self.missing_rss_samples}`",
            f"- Expected books: `{len(self.expected_books)}` (zero means configuration not supplied)",
            f"- Missing configured book observations: `{sum(self.missing_book_samples.values())}`",
            f"- Maximum allowed sample gap: `{'-' if self.max_sample_gap_seconds is None else f'{self.max_sample_gap_seconds:.1f}s'}`",
            f"- Excessive sample gaps: `{len(self.sampling_gaps)}`",
            "",
            "## Adapters",
            "",
            "| Exchange | Events during run | Reconnects | Gaps | Max message age | Last error |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for exchange, observation in sorted(self.adapters.items()):
            reconnect_delta = observation.last_reconnects - (observation.first_reconnects or 0)
            gap_delta = observation.last_gaps - (observation.first_gaps or 0)
            event_delta = observation.last_events - (observation.first_events or 0)
            deltas = (
                "invalid | invalid | invalid"
                if self.process_restarts
                else f"{event_delta} | {reconnect_delta} | {gap_delta}"
            )
            age = (
                "-"
                if observation.max_message_age_ms is None
                else f"{observation.max_message_age_ms} ms"
            )
            lines.append(f"| {exchange} | {deltas} | {age} | {observation.last_error or '-'} |")

        lines.extend(
            [
                "",
                "## Book eligibility",
                "",
                "| Book | Eligible samples | Ineligible reasons | p95 age | Max age | Recoveries |",
                "| --- | ---: | --- | ---: | ---: | --- |",
            ]
        )
        for key, book_observation in sorted(self.books.items()):
            reasons = (
                ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(book_observation.ineligible_reasons.items())
                )
                or "-"
            )
            p95_age = percentile(book_observation.ages_ms, 0.95)
            max_age = max(book_observation.ages_ms) if book_observation.ages_ms else None
            recoveries = (
                ", ".join(f"{value:.1f}s" for value in book_observation.recovery_seconds) or "-"
            )
            lines.append(
                f"| {key} | {book_observation.eligible_samples}/{self.samples} | {reasons} | "
                f"{'-' if p95_age is None else f'{p95_age} ms'} | "
                f"{'-' if max_age is None else f'{max_age} ms'} | {recoveries} |"
            )

        lines.extend(["", "## Process memory", ""])
        if self.rss_bytes:
            mib = [value / 1024 / 1024 for value in self.rss_bytes]
            lines.extend(
                [
                    f"- Samples: `{len(mib)}`",
                    f"- Minimum RSS: `{min(mib):.2f} MiB`",
                    f"- Maximum RSS: `{max(mib):.2f} MiB`",
                    f"- Mean RSS: `{statistics.fmean(mib):.2f} MiB`",
                    f"- Start-to-end change: `{mib[-1] - mib[0]:+.2f} MiB`",
                ]
            )
        else:
            lines.append("- RSS unavailable (pass `--pid` for the backend process).")

        if self.background_failures:
            lines.extend(["", "## Background failures", ""])
            lines.extend(
                f"- `{task}`: `{error}`" for task, error in sorted(self.background_failures.items())
            )
        if self.http_failures:
            lines.extend(["", "## Sampling failures", ""])
            lines.extend(f"- {failure}" for failure in self.http_failures)
        if self.sampling_gaps:
            lines.extend(["", "## Sampling interruptions", ""])
            lines.extend(
                f"- `{gap.started_at}` to `{gap.ended_at}`: `{gap.duration_seconds:.1f}s`"
                for gap in self.sampling_gaps
            )
        if self.interruption_reasons:
            lines.extend(["", "## Interruption reasons", ""])
            lines.extend(f"- {reason}" for reason in self.interruption_reasons)
        if self.websocket is not None:
            websocket = self.websocket
            lines.extend(
                [
                    "",
                    "## WebSocket delivery",
                    "",
                    f"- URL: `{websocket.url}`",
                    f"- Connections: `{websocket.connections}`",
                    f"- Reconnects: `{max(0, websocket.connections - 1)}`",
                    f"- Disconnects: `{websocket.disconnects}`",
                    f"- Frames: `{websocket.frames}`",
                    f"- Invalid frames: `{websocket.invalid_frames}`",
                    f"- Sequence gaps: `{websocket.sequence_gaps}`",
                    f"- Maximum disconnected time: `{websocket.max_disconnected_seconds:.1f}s`",
                    f"- Last error: `{websocket.last_error or '-'}`",
                ]
            )
            for message_type, count in sorted(websocket.frames_by_type.items()):
                lines.append(f"- `{message_type}` frames: `{count}`")
        lines.extend(["", "## Operational counters", ""])
        lines.append(
            "Deltas retain metric labels; resets or process restarts invalidate simple subtraction. "
            "Delivery counters include the observer's built-in WebSocket consumer and any "
            "independently connected clients."
        )
        for name, value in sorted(self.counter_last.items()):
            delta = (
                "invalid"
                if self.counter_resets or self.process_restarts
                else str(value - self.counter_start[name])
            )
            lines.append(f"- `{name}`: `{delta}`")
        if not self.counter_last:
            lines.append("- No operational counter series observed.")
        lines.extend(["", "## Run provenance", ""])
        lines.extend(f"- {name}: `{value}`" for name, value in sorted(self.metadata.items()))
        if self.missing_book_samples:
            lines.extend(["", "## Missing configured books", ""])
            lines.extend(
                f"- `{book}`: `{count}` samples"
                for book, count in sorted(self.missing_book_samples.items())
            )
        lines.append("")
        return "\n".join(lines)


def write_report(output: Path, report: SoakReport) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.markdown(), encoding="utf-8")


async def fetch_json(client: httpx.AsyncClient, path: str) -> dict[str, Any] | list[dict[str, Any]]:
    response = await client.get(path)
    if path != "/readyz":
        response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def parse_event_counts(metrics: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    pattern = re.compile(
        r'^arb_events_ingested_total\{[^}]*exchange="([^"]+)"[^}]*\}\s+([0-9.eE+-]+)$'
    )
    for line in metrics.splitlines():
        if match := pattern.match(line):
            counts[match.group(1)] = int(float(match.group(2)))
    return counts


async def fetch_event_counts(client: httpx.AsyncClient) -> dict[str, int]:
    response = await client.get("/metrics")
    response.raise_for_status()
    return parse_event_counts(response.text)


def parse_operational_counters(metrics: str) -> dict[str, int]:
    prefixes = (
        "arb_adapter_reconnects_total",
        "arb_adapter_pair_resyncs_total",
        "arb_persistence_queue_drops_total",
        "arb_ws_client_queue_overflows_total",
        "arb_ws_sender_failures_total",
        "arb_background_task_failures_total",
        "arb_reconcile_mismatches_total",
        "arb_reconcile_evidence_total",
        "arb_reconcile_confirmations_total",
        "arb_reconcile_recoveries_total",
        "arb_reconcile_failures_total",
    )
    result = {}
    for line in metrics.splitlines():
        if line.startswith(prefixes):
            name, value = line.rsplit(maxsplit=1)
            if name.split("{", 1)[0] not in prefixes:
                continue
            result[name] = int(float(value))
    return result


async def run_soak(
    base_url: str,
    duration_seconds: float,
    sample_interval_seconds: float,
    pid: int | None,
    output: Path,
    *,
    config: Path | None = None,
    samples_output: Path | None = None,
    max_sample_gap_seconds: float | None = None,
    websocket_url: str | None = None,
    observe_websocket_delivery: bool = True,
) -> SoakReport:
    report = SoakReport(utc_now(), duration_seconds, sample_interval_seconds)
    report.max_sample_gap_seconds = (
        sample_interval_seconds * 2 if max_sample_gap_seconds is None else max_sample_gap_seconds
    )
    report.metadata = {
        "platform": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "backend_pid": str(pid),
        "base_url": base_url,
    }
    if config is not None:
        from arb.config import SYMBOL_NORMALIZERS, load_config

        settings = load_config(config)
        report.expected_books = {
            f"{exchange}:{SYMBOL_NORMALIZERS[exchange](symbol)}"
            for exchange, symbols in settings.exchanges.items()
            for symbol in symbols
        }
        report.metadata["config_sha256"] = hashlib.sha256(config.read_bytes()).hexdigest()
        report.metadata["config_path"] = str(config.resolve())
    if samples_output is not None:
        if samples_output.resolve() == output.resolve():
            raise ValueError("sample evidence and Markdown report must use different paths")
        samples_output.parent.mkdir(parents=True, exist_ok=True)
        # Never silently combine separate runs or overwrite existing evidence.
        with samples_output.open("x", encoding="utf-8"):
            pass
        report.metadata["samples_path"] = str(samples_output.resolve())
    try:
        report.metadata["observer_checkout_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            timeout=10,
        ).strip()
        report.metadata["observer_checkout_dirty"] = str(
            bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain", "--untracked-files=normal"],
                    cwd=Path(__file__).resolve().parents[1],
                    text=True,
                    timeout=10,
                ).strip()
            )
        ).lower()
    except (OSError, subprocess.SubprocessError):
        report.metadata.setdefault("observer_checkout_commit", "unavailable")
        report.metadata["observer_checkout_dirty"] = "unavailable"
    loop = asyncio.get_running_loop()
    websocket_task: asyncio.Task[None] | None = None
    websocket_ready = asyncio.Event()
    if observe_websocket_delivery:
        report.websocket = WebSocketObservation(websocket_url or live_websocket_url(base_url))
        websocket_task = asyncio.create_task(observe_websocket(report.websocket, websocket_ready))
        try:
            await asyncio.wait_for(websocket_ready.wait(), timeout=15)
        except TimeoutError:
            report.interrupted = True
            report.interruption_reasons.append(
                "WebSocket did not provide an initial state within 15s"
            )
            report.ended_at = utc_now()
            write_report(output, report)
            websocket_task.cancel()
            await asyncio.gather(websocket_task, return_exceptions=True)
            return report
    started = loop.time()
    next_sample_started = started
    previous_sample_started: float | None = None
    previous_sampled_at: str | None = None
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        while True:
            elapsed = loop.time() - started
            sampled_at = utc_now()
            if report.websocket is not None:
                if report.websocket.invalid_frames or report.websocket.sequence_gaps:
                    report.interrupted = True
                    report.interruption_reasons.append(
                        "WebSocket delivery produced "
                        f"{report.websocket.invalid_frames} invalid frame(s) and "
                        f"{report.websocket.sequence_gaps} sequence gap(s)"
                    )
                    report.actual_duration_seconds = elapsed
                    report.ended_at = sampled_at
                    write_report(output, report)
                    break
                disconnected_seconds = report.websocket.disconnected_seconds(loop.time())
                report.websocket.max_disconnected_seconds = max(
                    report.websocket.max_disconnected_seconds, disconnected_seconds
                )
                if disconnected_seconds > report.max_sample_gap_seconds:
                    report.interrupted = True
                    report.interruption_reasons.append(
                        "WebSocket delivery was disconnected for "
                        f"{disconnected_seconds:.1f}s (maximum "
                        f"{report.max_sample_gap_seconds:.1f}s)"
                    )
                    report.actual_duration_seconds = elapsed
                    report.ended_at = sampled_at
                    write_report(output, report)
                    break
            if previous_sample_started is not None:
                gap_seconds = loop.time() - previous_sample_started
                assert previous_sampled_at is not None
                if gap_seconds > report.max_sample_gap_seconds:
                    gap = SamplingGap(previous_sampled_at, sampled_at, gap_seconds)
                    report.sampling_gaps.append(gap)
                    report.interrupted = True
                    evidence = {
                        "sampled_at": sampled_at,
                        "elapsed_seconds": elapsed,
                        "interruption": {
                            "previous_sampled_at": previous_sampled_at,
                            "duration_seconds": gap_seconds,
                            "maximum_seconds": report.max_sample_gap_seconds,
                        },
                    }
                    if samples_output is not None:
                        with samples_output.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(evidence) + "\n")
                    report.actual_duration_seconds = elapsed
                    report.ended_at = sampled_at
                    write_report(output, report)
                    break

            previous_sample_started = loop.time()
            previous_sampled_at = sampled_at
            evidence = {"sampled_at": sampled_at, "elapsed_seconds": elapsed}
            try:
                adapters = await fetch_json(client, "/api/adapters")
                books = await fetch_json(client, "/api/book-status")
                readiness = await fetch_json(client, "/readyz")
                overview = await fetch_json(client, "/api/system/overview")
                metrics = await client.get("/metrics")
                metrics.raise_for_status()
                event_counts = parse_event_counts(metrics.text)
                assert isinstance(adapters, list)
                assert isinstance(books, list)
                assert isinstance(readiness, dict)
                assert isinstance(overview, dict)
                assert isinstance(event_counts, dict)
                rss = None if pid is None else process_rss_bytes(pid)
                evidence.update(
                    adapters=adapters,
                    books=books,
                    readiness=readiness,
                    overview=overview,
                    metrics=metrics.text,
                    rss_bytes=rss,
                )
                report.observe(
                    adapters=adapters,
                    books=books,
                    readiness=readiness,
                    overview=overview,
                    event_counts=event_counts,
                    elapsed_seconds=elapsed,
                    rss=rss,
                    counters=parse_operational_counters(metrics.text),
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError, AssertionError) as exc:
                failure = f"{utc_now()}: {describe_error(exc)}"
                report.http_failures.append(failure)
                evidence["error"] = failure

            if samples_output is not None:
                with samples_output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(evidence) + "\n")

            report.actual_duration_seconds = loop.time() - started
            report.ended_at = utc_now()
            write_report(output, report)

            remaining = duration_seconds - (loop.time() - started)
            if remaining <= 0:
                report.completed = True
                break
            next_sample_started += sample_interval_seconds
            await asyncio.sleep(max(0, min(next_sample_started - loop.time(), remaining)))

    report.actual_duration_seconds = loop.time() - started
    report.ended_at = utc_now()
    if websocket_task is not None:
        websocket_task.cancel()
        await asyncio.gather(websocket_task, return_exceptions=True)
    write_report(output, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe a running ArbSync backend soak test.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration-seconds", type=float, default=86_400)
    parser.add_argument("--sample-seconds", type=float, default=300)
    parser.add_argument(
        "--max-sample-gap-seconds",
        type=float,
        help="Fail if sample starts are farther apart (default: twice --sample-seconds)",
    )
    parser.add_argument("--pid", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, help="Configuration to fingerprint and check book coverage"
    )
    parser.add_argument(
        "--samples-output", type=Path, help="New JSONL file for raw sampling evidence"
    )
    parser.add_argument(
        "--websocket-url", help="Live WebSocket URL (default: derived from base URL)"
    )
    parser.add_argument(
        "--no-websocket",
        action="store_true",
        help="Disable the built-in live delivery consumer (not valid for release evidence)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.duration_seconds <= 0 or args.sample_seconds <= 0:
        raise SystemExit("duration and sample interval must be positive")
    if args.max_sample_gap_seconds is not None and args.max_sample_gap_seconds <= 0:
        raise SystemExit("maximum sample gap must be positive")
    report = asyncio.run(
        run_soak(
            args.base_url,
            args.duration_seconds,
            args.sample_seconds,
            args.pid,
            args.output,
            config=args.config,
            samples_output=args.samples_output,
            max_sample_gap_seconds=args.max_sample_gap_seconds,
            websocket_url=args.websocket_url,
            observe_websocket_delivery=not args.no_websocket,
        )
    )
    print(json.dumps({"output": str(args.output), "samples": report.samples}))
    if report.interrupted:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
