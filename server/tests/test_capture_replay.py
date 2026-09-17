from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from arb import main
from arb.capture import CaptureWriter
from arb.replay import ReplayError, replay_file, replay_frames


def _gemini_snapshot() -> str:
    return (
        '{"e":"depthUpdate","s":"BTCUSD","U":1,"u":1,"E":1000,"b":[["100","1"]],"a":[["102","1"]]}'
    )


def _gemini_delta() -> str:
    return (
        '{"e":"depthUpdate","s":"BTCUSD","U":2,"u":2,"E":2000,"b":[["101","1"]],"a":[["103","1"]]}'
    )


def _coinbase_snapshot() -> str:
    return (
        '{"type":"snapshot","product_id":"BTC-USD","sequence_num":10,"updates":['
        '{"side":"bid","price_level":"99","new_quantity":"2"},'
        '{"side":"offer","price_level":"104","new_quantity":"2"}]}'
    )


def _coinbase_update() -> str:
    return (
        '{"type":"update","product_id":"BTC-USD","sequence_num":11,"updates":['
        '{"side":"offer","price_level":"100.5","new_quantity":"3"}]}'
    )


def _binance_depth() -> str:
    return '{"s":"BTCUSD","U":95,"u":99,"E":1500,"b":[["100","1"]],"a":[["105","1"]]}'


def _binance_delta() -> str:
    return '{"s":"BTCUSD","U":100,"u":105,"E":1600,"b":[["99.6","1"]],"a":[]}'


def _binance_snapshot_payload() -> dict[str, object]:
    return {"lastUpdateId": 100, "bids": [["99.5", "1"]], "asks": [["106", "1"]]}


EXCHANGES = {"gemini": ["btcusd"], "coinbase": ["BTC-USD"], "binance": ["BTCUSD"]}


async def _write_three_venue_capture(path: Path) -> None:
    writer = CaptureWriter(path, EXCHANGES)
    task = asyncio.create_task(writer.run())
    assert writer.record_ws("gemini", _gemini_snapshot(), [])
    assert writer.record_ws("coinbase", _coinbase_snapshot(), [])
    assert writer.record_ws("binance", _binance_depth(), [])
    assert writer.record_snapshot(
        "binance",
        "https://api.binance.us/api/v3/depth?symbol=BTCUSD&limit=5000",
        _binance_snapshot_payload(),
    )
    assert writer.record_ws("gemini", _gemini_delta(), [])
    assert writer.record_ws("coinbase", _coinbase_update(), [])
    assert writer.record_ws("binance", _binance_delta(), [])
    await writer.close()
    await task


def test_replay_twice_produces_identical_digest(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write_three_venue_capture(path))

    first = asyncio.run(replay_file(path))
    second = asyncio.run(replay_file(path))

    assert first.digest == second.digest
    assert first.transitions == second.transitions
    assert [opp.as_payload() for opp in first.opportunities] == [
        opp.as_payload() for opp in second.opportunities
    ]
    # Six events: two Gemini, two Coinbase, one Binance snapshot whose buffered
    # depth update it fully covers, and one Binance delta.
    assert len(first.transitions) == 6
    assert first.snapshots_consumed == 1


def test_replay_runs_detector_on_the_real_path(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write_three_venue_capture(path))

    report = asyncio.run(replay_file(path))

    pairs = {(opp.buy_exchange, opp.sell_exchange) for opp in report.opportunities}
    assert ("coinbase", "gemini") in pairs
    assert all(opp.pair == "BTC-USD" for opp in report.opportunities)


def test_replay_without_snapshot_data_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "capture.jsonl"
        writer = CaptureWriter(path, {"binance": ["BTCUSD"]})
        task = asyncio.create_task(writer.run())
        assert writer.record_ws("binance", _binance_depth(), [])
        await writer.close()
        await task
        await replay_file(path)

    with pytest.raises(ReplayError, match="no snapshot data"):
        asyncio.run(scenario())


def test_replay_unknown_exchange_fails(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    header = {
        "type": "header",
        "format": "arbsync-capture",
        "version": 1,
        "exchanges": {"kraken": ["BTCUSD"]},
        "started_wall_ns": 1,
    }
    footer = {"type": "footer", "frame_count": 0, "counts": {}, "clean": True}
    path.write_text(json.dumps(header) + "\n" + json.dumps(footer) + "\n")

    with pytest.raises(ReplayError, match="unknown exchange"):
        asyncio.run(replay_file(path))


def test_replay_backwards_timeline_fails(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    header = {
        "type": "header",
        "format": "arbsync-capture",
        "version": 1,
        "exchanges": {"gemini": ["btcusd"]},
        "started_wall_ns": 1,
    }
    frames = [
        {
            "exchange": "gemini",
            "kind": "ws",
            "wall_ns": 200,
            "mono_ns": 200,
            "raw": _gemini_snapshot(),
            "events": [],
        },
        {
            "exchange": "gemini",
            "kind": "ws",
            "wall_ns": 100,
            "mono_ns": 100,
            "raw": _gemini_delta(),
            "events": [],
        },
    ]
    footer = {"type": "footer", "frame_count": 2, "counts": {"gemini": 2}, "clean": True}
    path.write_text(
        "\n".join(
            [json.dumps(header)] + [json.dumps(frame) for frame in frames] + [json.dumps(footer)]
        )
        + "\n"
    )

    with pytest.raises(ReplayError, match="backwards in monotonic time"):
        asyncio.run(replay_file(path))


def test_replay_rejects_non_positive_speed(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    asyncio.run(_write_three_venue_capture(path))

    async def scenario() -> None:
        from arb.capture import read_capture

        header, frames = read_capture(path)
        await replay_frames(header, frames, speed=0)

    with pytest.raises(ReplayError, match="speed must be greater than zero"):
        asyncio.run(scenario())


@pytest.mark.asyncio
async def test_process_market_event_honors_recorded_timestamps(monkeypatch) -> None:
    from arb.orderbook import OrderBookManager
    from arb.types import EventKind, MarketEvent, PriceLevel

    event = MarketEvent(
        exchange="gemini",
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=1,
        bids=(PriceLevel(Decimal("100"), Decimal("1")),),
        asks=(PriceLevel(Decimal("101"), Decimal("1")),),
    )
    detector = Mock()
    detector.detect_for_pair.return_value = []
    for name in ("book_metrics", "detection_latency_seconds", "opportunity_counter"):
        monkeypatch.setattr(main, name, Mock())

    manager = OrderBookManager()
    await main.process_market_event(
        event,
        book_manager=manager,
        detector=detector,
        store=Mock(enqueue=AsyncMock()),
        broadcaster=Mock(
            broadcast=AsyncMock(),
            broadcast_book=AsyncMock(),
            broadcast_status=AsyncMock(),
        ),
        detected_at_ns=999,
        now_monotonic_ns=1_000,
    )

    detector.detect_for_pair.assert_called_once_with("BTC-USD", [], 999)
    assert manager.eligibility("gemini", "BTC-USD", 2_000).age_ns == 1_000
