from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import binance_snapshot_audit
import pytest
from arb.capture import CaptureFrame, CaptureHeader, ConnectionBoundary, SnapshotProvenance
from arb.types import PriceLevel
from binance_snapshot_audit import compare


def _side(*levels: tuple[str, str]) -> list[PriceLevel]:
    return [PriceLevel(Decimal(price), Decimal(size)) for price, size in levels]


def test_exact_snapshot_comparison_distinguishes_missing_and_size_levels() -> None:
    book = (_side(("100", "1"), ("99", "2")), _side(("101", "1")))
    rest = (_side(("101", "1"), ("99", "3")), _side(("101", "1"), ("102", "1")))

    result = compare(book, rest)

    assert result["bids_rest_only_best"] == 1
    assert result["bids_stream_only_best"] == 1
    assert result["bids_size_differs"] == 1
    assert result["asks_rest_only"] == 1
    assert result["mismatch"] == 1


def test_historical_audit_judges_only_exact_update_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    second = 1_000_000_000

    def frame(tick: int, raw: dict[str, object]) -> CaptureFrame:
        return CaptureFrame(
            "binance", "ws", tick * second, tick * second, json.dumps(raw), None, None, ()
        )

    def diff(tick: int, first: int, last: int, bid: str) -> CaptureFrame:
        return frame(
            tick,
            {"s": "BTCUSD", "U": first, "u": last, "E": 1, "b": [[bid, "1"]], "a": [["102", "1"]]},
        )

    def snapshot(tick: int, update_id: int, bid: str, purpose: str) -> CaptureFrame:
        bids = (
            [[bid, "1"]]
            if purpose == "initial_sync"
            else [["101", "1"], ["100", "1" if bid == "101" else "2"], ["99", "1"]]
        )
        return CaptureFrame(
            "binance",
            "snapshot",
            tick * second,
            tick * second,
            None,
            {"lastUpdateId": update_id, "bids": bids, "asks": [["102", "1"]]},
            "https://api.binance.us/api/v3/depth?symbol=BTCUSD&limit=5000",
            (),
            snapshot_provenance=SnapshotProvenance(
                purpose, "BTC-USD", 1, second, second, tick * second, tick * second
            ),
        )

    frames = [
        CaptureFrame(
            "binance", "connection", 0, 0, None, None, None, (), ConnectionBoundary(True, 1)
        ),
        diff(1, 95, 99, "99"),
        diff(2, 100, 101, "100"),
        snapshot(3, 100, "99", "initial_sync"),
        diff(4, 102, 102, "101"),
        snapshot(5, 102, "101", "reconciliation"),
        snapshot(6, 102, "100", "reconciliation"),
        snapshot(7, 103, "101", "reconciliation"),
    ]
    header = CaptureHeader({"binance": ["BTCUSD"]}, 0, provenance="complete")
    monkeypatch.setattr(binance_snapshot_audit, "read_capture", lambda _path: (header, frames))

    result = asyncio.run(binance_snapshot_audit.analyze(Path("synthetic.jsonl")))
    pair = result["by_pair"]["binance:BTC-USD"]
    assert pair["aligned"] == 2
    assert pair["exact_match"] == 1
    assert pair["mismatch"] == 1
    assert pair["unaligned"] == 1
