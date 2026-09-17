from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from arb.capture import (
    CaptureError,
    CaptureWriter,
    read_capture,
    summarize_event,
)
from arb.config import ConfigError, load_config
from arb.types import EventKind, MarketEvent, PriceLevel

VALID_CONFIG = """
[detector]
threshold_pct = 0.25

[exchanges]
gemini = ["btcusd", "ethusd"]
coinbase = ["BTC-USD"]
binance = ["BTCUSD"]

[server]
host = "0.0.0.0"
port = 8000
database_path = "arb.sqlite3"
cors_allowed_origins = ["https://dashboard.example.test"]

[persistence]
batch_size = 500
flush_interval_seconds = 1.0
queue_maxsize = 1000
"""


def _event() -> MarketEvent:
    return MarketEvent(
        exchange="gemini",
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=123,
        bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
        asks=(PriceLevel(price=Decimal("101"), size=Decimal("2")),),
        exchange_first_sequence=10,
        exchange_last_sequence=12,
    )


async def _write(path: Path, exchanges: dict[str, list[str]]) -> CaptureWriter:
    writer = CaptureWriter(path, exchanges)
    task = asyncio.create_task(writer.run())
    assert writer.record_ws("gemini", '{"e":"depthUpdate"}', [_event()])
    assert writer.record_snapshot(
        "binance", "https://example.test/depth?symbol=BTCUSD", {"lastUpdateId": 7}
    )
    await writer.close()
    await task
    return writer


def test_capture_round_trip_preserves_raw_text_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write(path, {"gemini": ["btcusd"], "binance": ["BTCUSD"]}))

    header, frames = read_capture(path)

    assert header.exchanges == {"gemini": ["btcusd"], "binance": ["BTCUSD"]}
    assert [frame.kind for frame in frames] == ["ws", "snapshot"]
    ws_frame = frames[0]
    assert ws_frame.raw == '{"e":"depthUpdate"}'
    assert ws_frame.events[0].pair == "BTC-USD"
    assert ws_frame.events[0].first_sequence == 10
    assert ws_frame.events[0].last_sequence == 12
    snapshot_frame = frames[1]
    assert snapshot_frame.url == "https://example.test/depth?symbol=BTCUSD"
    assert snapshot_frame.payload == {"lastUpdateId": 7}
    assert snapshot_frame.mono_ns >= ws_frame.mono_ns


def test_capture_gzip_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl.gz"
    asyncio.run(_write(path, {"gemini": ["btcusd"], "binance": ["BTCUSD"]}))

    _, frames = read_capture(path)

    assert [frame.kind for frame in frames] == ["ws", "snapshot"]
    assert frames[0].raw == '{"e":"depthUpdate"}'


def test_full_capture_queue_drops_frames_without_blocking(tmp_path: Path) -> None:
    writer = CaptureWriter(tmp_path / "capture.jsonl", {"gemini": ["btcusd"]}, queue_maxsize=1)

    assert writer.record_ws("gemini", "one", []) is True
    assert writer.record_ws("gemini", "two", []) is False


def test_recording_after_close_is_dropped(tmp_path: Path) -> None:
    async def scenario() -> bool:
        writer = CaptureWriter(tmp_path / "capture.jsonl", {"gemini": ["btcusd"]})
        task = asyncio.create_task(writer.run())
        await writer.close()
        await task
        return writer.record_ws("gemini", "late", [])

    assert asyncio.run(scenario()) is False


def test_truncated_capture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write(path, {"gemini": ["btcusd"], "binance": ["BTCUSD"]}))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")

    with pytest.raises(CaptureError, match="truncated"):
        read_capture(path)


def test_footer_frame_count_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write(path, {"gemini": ["btcusd"], "binance": ["BTCUSD"]}))
    lines = path.read_text().splitlines()
    footer = json.loads(lines[-1])
    footer["frame_count"] += 1
    lines[-1] = json.dumps(footer)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(CaptureError, match="footer expects"):
        read_capture(path)


def test_unknown_format_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write(path, {"gemini": ["btcusd"], "binance": ["BTCUSD"]}))
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["version"] += 1
    lines[0] = json.dumps(header)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(CaptureError, match="header is invalid"):
        read_capture(path)


def test_empty_capture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text("")

    with pytest.raises(CaptureError, match="empty"):
        read_capture(path)


def test_missing_capture_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="not found"):
        read_capture(tmp_path / "missing.jsonl")


def test_event_summary_preserves_exchange_sequence_identifiers() -> None:
    summary = summarize_event(_event())

    assert summary.first_sequence == 10
    assert summary.last_sequence == 12
    assert summary.timestamp_ns == 123


def _load(text: str, tmp_path: Path):  # type: ignore[no-untyped-def]
    path = tmp_path / "config.toml"
    path.write_text(text)
    return load_config(path)


def test_capture_config_defaults_when_section_missing(tmp_path: Path) -> None:
    config = _load(VALID_CONFIG, tmp_path)

    assert config.capture.queue_maxsize == 10_000


def test_capture_config_parses_queue_maxsize(tmp_path: Path) -> None:
    config = _load(VALID_CONFIG + "\n[capture]\nqueue_maxsize = 500\n", tmp_path)

    assert config.capture.queue_maxsize == 500


@pytest.mark.parametrize("value", ["0", "-5"])
def test_capture_config_rejects_non_positive_queue_maxsize(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="capture.queue_maxsize must be greater than zero"):
        _load(VALID_CONFIG + f"\n[capture]\nqueue_maxsize = {value}\n", tmp_path)


def test_capture_config_rejects_non_table(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="capture must be a table"):
        _load(VALID_CONFIG.replace("[detector]", "capture = 5\n[detector]"), tmp_path)
