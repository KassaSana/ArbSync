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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


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
        lines = [
            f"# Live soak report - {self.started_at[:10]}",
            "",
            f"- Started: `{self.started_at}`",
            f"- Ended: `{self.ended_at}`",
            f"- Requested duration: `{self.duration_requested_seconds:.1f}s`",
            f"- Actual duration: `{self.actual_duration_seconds:.1f}s`",
            f"- Sample interval: `{self.sample_interval_seconds:.1f}s`",
            f"- Status: `{'complete' if self.completed else 'in progress'}`",
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
        lines.extend(["", "## Operational counters", ""])
        lines.append(
            "Deltas retain metric labels; resets or process restarts invalidate simple subtraction. "
            "The observer does not create WebSocket clients; delivery counters only cover independently connected clients."
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
        "arb_persistence_queue_drops_total",
        "arb_ws_client_queue_overflows_total",
        "arb_ws_sender_failures_total",
        "arb_background_task_failures_total",
        "arb_reconcile_mismatches_total",
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
) -> SoakReport:
    report = SoakReport(utc_now(), duration_seconds, sample_interval_seconds)
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
    started = loop.time()
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        while True:
            elapsed = loop.time() - started
            evidence: dict[str, Any] = {"sampled_at": utc_now(), "elapsed_seconds": elapsed}
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
                failure = f"{utc_now()}: {type(exc).__name__}: {exc}"
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
                break
            await asyncio.sleep(min(sample_interval_seconds, remaining))

    report.actual_duration_seconds = loop.time() - started
    report.ended_at = utc_now()
    report.completed = True
    write_report(output, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe a running ArbSync backend soak test.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration-seconds", type=float, default=86_400)
    parser.add_argument("--sample-seconds", type=float, default=300)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, help="Configuration to fingerprint and check book coverage"
    )
    parser.add_argument(
        "--samples-output", type=Path, help="New JSONL file for raw sampling evidence"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.duration_seconds <= 0 or args.sample_seconds <= 0:
        raise SystemExit("duration and sample interval must be positive")
    report = asyncio.run(
        run_soak(
            args.base_url,
            args.duration_seconds,
            args.sample_seconds,
            args.pid,
            args.output,
            config=args.config,
            samples_output=args.samples_output,
        )
    )
    print(json.dumps({"output": str(args.output), "samples": report.samples}))


if __name__ == "__main__":
    main()
