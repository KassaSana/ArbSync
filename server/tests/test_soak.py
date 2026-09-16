from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest
import soak
from soak import (
    SamplingGap,
    SoakReport,
    WebSocketObservation,
    live_websocket_url,
    observe_websocket,
    parse_event_counts,
    parse_operational_counters,
    process_rss_bytes,
    write_report,
)


def test_soak_report_tracks_recovery_and_counters() -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 60, 5)
    adapter = {
        "exchange": "gemini",
        "reconnect_count": 2,
        "gap_count": 1,
        "last_message_age_ms": 50,
        "last_error": None,
    }
    eligible = {
        "exchange": "gemini",
        "pair": "BTC-USD",
        "eligible": True,
        "reason": None,
        "age_ms": 25,
    }
    ineligible = {**eligible, "eligible": False, "reason": "too_old", "age_ms": 31_000}
    readiness = {"status": "ready", "background_task_failures": []}
    report.observe(
        adapters=[adapter],
        books=[eligible],
        readiness=readiness,
        overview={"all_time_count": 10},
        event_counts={"gemini": 100},
        elapsed_seconds=0,
        rss=100,
    )
    report.observe(
        adapters=[{**adapter, "reconnect_count": 3, "gap_count": 2}],
        books=[ineligible],
        readiness={"status": "not_ready", "background_task_failures": []},
        overview={"all_time_count": 12},
        event_counts={"gemini": 140},
        elapsed_seconds=5,
        rss=110,
    )
    report.observe(
        adapters=[{**adapter, "reconnect_count": 3, "gap_count": 2}],
        books=[eligible],
        readiness=readiness,
        overview={"all_time_count": 13},
        event_counts={"gemini": 175},
        elapsed_seconds=10,
        rss=105,
    )
    report.ended_at = "2026-09-05T00:01:00+00:00"
    report.actual_duration_seconds = 60

    markdown = report.markdown()

    assert "Opportunities observed: `3`" in markdown
    assert "| gemini | 75 | 1 | 1 | 50 ms | - |" in markdown
    assert "too_old=1" in markdown
    assert "5.0s" in markdown
    assert "Start-to-end change: `+0.00 MiB`" in markdown


def test_process_rss_reads_current_process() -> None:
    rss = process_rss_bytes(os.getpid())
    assert rss is not None
    assert rss > 0


def test_parse_event_counts_reads_prometheus_labels() -> None:
    metrics = """
# HELP arb_events_ingested_total Market events ingested
arb_events_ingested_total{exchange="gemini"} 123.0
arb_events_ingested_total{exchange="coinbase"} 4.2e+01
arb_book_eligible{exchange="gemini",pair="BTC-USD"} 1.0
"""

    assert parse_event_counts(metrics) == {"gemini": 123, "coinbase": 42}


def test_live_websocket_url_preserves_base_path_and_selects_secure_scheme() -> None:
    assert live_websocket_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/ws/live"
    assert live_websocket_url("https://example.test/api/") == "wss://example.test/api/ws/live"
    with pytest.raises(ValueError, match="absolute HTTP"):
        live_websocket_url("localhost:8000")


@pytest.mark.asyncio
async def test_websocket_observer_validates_initial_state_sequences_and_frames() -> None:
    class FakeWebSocket:
        async def __aenter__(self) -> FakeWebSocket:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield json.dumps({"type": "state_snapshot", "payload": {}, "stream_sequence": 10})
            yield json.dumps({"type": "top_of_book", "payload": {}, "stream_sequence": 12})
            yield "not json"
            await asyncio.Future()

    observation = WebSocketObservation("ws://example.test/ws/live")
    ready = asyncio.Event()
    task = asyncio.create_task(
        observe_websocket(observation, ready, connector=lambda *args, **kwargs: FakeWebSocket())
    )
    await asyncio.wait_for(ready.wait(), timeout=1)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert observation.connections == 1
    assert observation.frames == 2
    assert observation.frames_by_type == {"state_snapshot": 1, "top_of_book": 1}
    assert observation.sequence_gaps == 1
    assert observation.invalid_frames == 1


def test_markdown_labels_an_unfinished_run_as_in_progress() -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 86_400, 60)

    assert "- Status: `in progress`" in report.markdown()

    report.completed = True

    assert "- Status: `complete`" in report.markdown()


def test_markdown_labels_an_excessive_gap_as_interrupted() -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 86_400, 60)
    report.max_sample_gap_seconds = 120
    report.sampling_gaps.append(
        SamplingGap(
            "2026-09-05T01:00:00+00:00",
            "2026-09-05T10:30:00+00:00",
            34_200,
        )
    )
    report.interrupted = True

    markdown = report.markdown()

    assert "- Status: `interrupted`" in markdown
    assert "- Maximum allowed sample gap: `120.0s`" in markdown
    assert "- Excessive sample gaps: `1`" in markdown
    assert "`34200.0s`" in markdown


def test_markdown_describes_built_in_websocket_counter_context() -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 86_400, 60)
    report.websocket = WebSocketObservation("ws://example.test/ws/live")

    markdown = report.markdown()

    assert "observer's built-in WebSocket consumer" in markdown
    assert "does not create WebSocket clients" not in markdown


def test_write_report_creates_missing_directories(tmp_path: Path) -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 86_400, 60)
    output = tmp_path / "nested" / "soak.md"

    write_report(output, report)

    assert output.read_text(encoding="utf-8").startswith("# Live soak report - 2026-09-05")

    report.completed = True
    write_report(output, report)

    assert "- Status: `complete`" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("last_started", "last_value", "restarts", "resets"),
    [("2", 1, 1, 1), ("2", 5, 1, 0), ("1", 1, 0, 1)],
)
def test_operational_counters_first_failure_and_process_restart(
    last_started: str, last_value: int, restarts: int, resets: int
) -> None:
    report = SoakReport("2026-09-12T00:00:00Z", 86400, 60)
    metric = 'arb_persistence_queue_drops_total{reason="queue_full"}'
    for started, counters in [("1", {}), ("1", {metric: 3}), (last_started, {metric: last_value})]:
        report.observe(
            adapters=[],
            books=[],
            readiness={"status": "ready"},
            overview={"all_time_count": 0, "started_at_ns": started},
            event_counts={},
            elapsed_seconds=0,
            rss=None,
            counters=counters,
        )
    assert report.counter_start[metric] == 0
    assert report.process_restarts == restarts
    assert report.counter_resets == resets
    assert report.missing_rss_samples == 3
    assert f"- `{metric}`: `invalid`" in report.markdown()
    assert parse_operational_counters(f"{metric} 3.0\n# ignored\narb_events_ingested_total 2") == {
        metric: 3
    }


def test_operational_counter_baselines_and_exact_metric_names() -> None:
    report = SoakReport("2026-09-12T00:00:00Z", 60, 5)
    metric = "arb_ws_sender_failures_total"
    late_metric = 'arb_reconcile_failures_total{exchange="gemini",pair="BTC-USD"}'
    for counters in [{metric: 4}, {metric: 6, late_metric: 3}]:
        report.observe(
            adapters=[],
            books=[],
            readiness={"status": "ready"},
            overview={"all_time_count": 0, "started_at_ns": "1"},
            event_counts={},
            elapsed_seconds=0,
            rss=100,
            counters=counters,
        )
    markdown = report.markdown()
    assert f"- `{metric}`: `2`" in markdown
    assert f"- `{late_metric}`: `3`" in markdown
    assert parse_operational_counters(
        f"{metric}\t6.0\n{metric}_created 123\n{late_metric} 3e0\n"
    ) == {metric: 6, late_metric: 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("metrics_fail", [False, True])
async def test_soak_samples_metrics_and_checkpoints_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metrics_fail: bool
) -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/metrics":
            return httpx.Response(
                503 if metrics_fail else 200,
                text="arb_ws_sender_failures_total 2\n",
            )
        payloads: dict[str, object] = {
            "/api/adapters": [],
            "/api/book-status": [],
            "/readyz": {
                "status": "not_ready",
                "background_task_failures": [{"task": "persistence", "error": "failed"}],
            },
            "/api/system/overview": {"all_time_count": 0, "started_at_ns": "1"},
        }
        return httpx.Response(
            503 if request.url.path == "/readyz" else 200,
            json=payloads[request.url.path],
        )

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(soak.httpx, "AsyncClient", lambda **kwargs: client)
    output = tmp_path / "report.md"
    evidence = tmp_path / "raw" / "samples.jsonl"
    config = Path(__file__).resolve().parents[2] / "config.toml"
    report = await soak.run_soak(
        "http://test",
        0,
        1,
        None,
        output,
        config=config,
        samples_output=evidence,
        observe_websocket_delivery=False,
    )
    assert paths.count("/metrics") == 1
    assert report.completed
    assert output.read_text(encoding="utf-8") == report.markdown()
    assert report.samples == (0 if metrics_fail else 1)
    assert len(report.http_failures) == int(metrics_fail)
    if not metrics_fail:
        assert report.counter_last == {"arb_ws_sender_failures_total": 2}
        assert report.background_failures == {"persistence": "failed"}
    assert "observer_checkout_commit" in report.metadata
    assert "observer_checkout_dirty" in report.metadata
    sample = json.loads(evidence.read_text(encoding="utf-8"))
    assert ("error" in sample) == metrics_fail
    assert len(report.expected_books) == 27
    assert len(report.metadata["config_sha256"]) == 64
    if not metrics_fail:
        assert len(report.missing_book_samples) == 27
        assert sample["metrics"] == "arb_ws_sender_failures_total 2\n"


@pytest.mark.asyncio
async def test_soak_refuses_to_overwrite_or_mix_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "samples.jsonl"
    evidence.write_text("previous run\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        await soak.run_soak(
            "http://unused",
            0,
            1,
            None,
            tmp_path / "report.md",
            samples_output=evidence,
            observe_websocket_delivery=False,
        )
    assert evidence.read_text(encoding="utf-8") == "previous run\n"
    with pytest.raises(ValueError, match="different paths"):
        await soak.run_soak(
            "http://unused",
            0,
            1,
            None,
            evidence,
            samples_output=evidence,
            observe_websocket_delivery=False,
        )


@pytest.mark.asyncio
async def test_soak_fails_fast_when_sample_start_gap_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        time.sleep(0.003)
        if request.url.path == "/metrics":
            return httpx.Response(200, text="")
        payloads: dict[str, object] = {
            "/api/adapters": [],
            "/api/book-status": [],
            "/readyz": {"status": "ready", "background_task_failures": []},
            "/api/system/overview": {"all_time_count": 0, "started_at_ns": "1"},
        }
        return httpx.Response(200, json=payloads[request.url.path])

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(soak.httpx, "AsyncClient", lambda **kwargs: client)
    output = tmp_path / "report.md"
    evidence = tmp_path / "samples.jsonl"

    report = await soak.run_soak(
        "http://test",
        1,
        0.001,
        None,
        output,
        samples_output=evidence,
        max_sample_gap_seconds=0.005,
        observe_websocket_delivery=False,
    )

    assert report.interrupted
    assert not report.completed
    assert len(report.sampling_gaps) == 1
    assert "- Status: `interrupted`" in output.read_text(encoding="utf-8")
    records = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()]
    assert "interruption" in records[-1]


def test_describe_error_omits_an_empty_message() -> None:
    from soak import describe_error

    assert describe_error(TimeoutError()) == "TimeoutError"
    assert describe_error(ValueError("bad value")) == "ValueError: bad value"
