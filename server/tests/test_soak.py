from __future__ import annotations

import os
from pathlib import Path

from soak import SoakReport, parse_event_counts, process_rss_bytes, write_report


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


def test_markdown_labels_an_unfinished_run_as_in_progress() -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 86_400, 60)

    assert "- Status: `in progress`" in report.markdown()

    report.completed = True

    assert "- Status: `complete`" in report.markdown()


def test_write_report_creates_missing_directories(tmp_path: Path) -> None:
    report = SoakReport("2026-09-05T00:00:00+00:00", 86_400, 60)
    output = tmp_path / "nested" / "soak.md"

    write_report(output, report)

    assert output.read_text(encoding="utf-8").startswith("# Live soak report - 2026-09-05")

    report.completed = True
    write_report(output, report)

    assert "- Status: `complete`" in output.read_text(encoding="utf-8")
