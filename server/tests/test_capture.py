from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from arb import main as main_module
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

[fees]
gemini = { taker_pct = 0.40, maker_pct = 0.20 }
coinbase = { taker_pct = 0.60 }
binance = { taker_pct = 0.60 }
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


def test_capture_open_writes_and_close_run_off_event_loop(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    async def tracked_to_thread(function, /, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)

    asyncio.run(_write(tmp_path / "capture.jsonl.gz", {"gemini": ["btcusd"]}))

    assert calls[0] == "_open_capture"
    assert "writelines" in calls
    assert calls[-1] == "close"


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90s", 90.0),
        ("10m", 600.0),
        ("1h", 3600.0),
        ("1.5m", 90.0),
        ("60", 60.0),
    ],
)
def test_parse_duration_accepts_suffixes(value: str, expected: float) -> None:
    assert main_module.parse_duration(value) == expected


@pytest.mark.parametrize(
    "value",
    ["nan", "inf", "-inf", "0", "0m", "-5s", "soon", "10x", ""],
)
def test_parse_duration_rejects_bad_values(value: str) -> None:
    with pytest.raises(ConfigError, match="duration|invalid duration"):
        main_module.parse_duration(value)


def test_capture_command_dispatches_to_run_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    output = tmp_path / "capture.jsonl"
    received: list[tuple[object, float, Path]] = []

    async def fake_run_capture(
        path: str | Path,
        duration_seconds: float,
        destination: Path,
        **kwargs: object,
    ) -> None:
        received.append((path, duration_seconds, destination))

    monkeypatch.setattr(main_module, "run_capture", fake_run_capture)
    main_module.main(
        ["--config", str(config_path), "capture", "--duration", "10m", "--output", str(output)]
    )

    assert received == [(config_path, 600.0, output)]


def test_capture_command_rejects_bad_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    monkeypatch.setenv("ARB_CONFIG", str(config_path))

    with pytest.raises(SystemExit, match="2"):
        main_module.main(["capture", "--duration", "soon", "--output", str(tmp_path / "o.jsonl")])

    assert "invalid duration" in capsys.readouterr().err


def test_run_capture_writes_valid_empty_capture(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    output = tmp_path / "capture.jsonl"

    asyncio.run(main_module.run_capture(config_path, 0.05, output, adapter_types=[]))

    header, frames = read_capture(output)
    assert header.exchanges == {
        "gemini": ["btcusd", "ethusd"],
        "coinbase": ["BTC-USD"],
        "binance": ["BTCUSD"],
    }
    assert frames == []


@pytest.mark.parametrize(("value", "expected"), [("2", 2.0), ("0.5", 0.5), ("10", 10.0)])
def test_parse_speed_accepts_positive_numbers(value: str, expected: float) -> None:
    assert main_module.parse_speed(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "fast", "", "nan", "inf"])
def test_parse_speed_rejects_bad_values(value: str) -> None:
    with pytest.raises(ConfigError, match="speed"):
        main_module.parse_speed(value)


def test_replay_command_runs_offline_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    capture_path = tmp_path / "capture.jsonl"
    capture_path.touch()
    received: list[tuple[object, Path, object]] = []

    async def fake_run_replay_offline(path: str | Path, capture: Path, speed: float | None) -> Mock:
        received.append((path, capture, speed))
        return Mock(transitions=[], opportunities=[], snapshots_consumed=0, digest="abc")

    monkeypatch.setattr(main_module, "run_replay_offline", fake_run_replay_offline)
    main_module.main(["--config", str(config_path), "replay", str(capture_path), "--speed", "2"])

    assert received == [(config_path, capture_path, 2.0)]


def test_replay_command_uses_max_speed_offline_unless_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    capture_path = tmp_path / "capture.jsonl"
    capture_path.touch()

    async def fake_run_replay_offline(path: str | Path, capture: Path, speed: float | None) -> Mock:
        assert speed is None
        return Mock(transitions=[], opportunities=[], snapshots_consumed=0, digest="abc")

    monkeypatch.setattr(main_module, "run_replay_offline", fake_run_replay_offline)
    main_module.main(["--config", str(config_path), "replay", str(capture_path)])

    assert json.loads(capsys.readouterr().out) == {
        "transitions": 0,
        "opportunities": 0,
        "snapshots_consumed": 0,
        "digest": "abc",
    }


def test_replay_serve_defaults_to_realtime_and_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    capture_path = tmp_path / "capture.jsonl"
    capture_path.touch()
    received: list[tuple[object, Path, object]] = []

    async def fake_run_replay_serve(path: str | Path, capture: Path, speed: float | None) -> None:
        received.append((path, capture, speed))

    monkeypatch.setattr(main_module, "run_replay_serve", fake_run_replay_serve)
    main_module.main(["--config", str(config_path), "replay", str(capture_path), "--serve"])

    assert received == [(config_path, capture_path, 1.0)]


def test_replay_command_rejects_bad_speed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)
    capture_path = tmp_path / "capture.jsonl"
    capture_path.touch()

    with pytest.raises(SystemExit, match="2"):
        main_module.main(
            ["--config", str(config_path), "replay", str(capture_path), "--speed", "fast"]
        )

    assert "invalid speed" in capsys.readouterr().err
