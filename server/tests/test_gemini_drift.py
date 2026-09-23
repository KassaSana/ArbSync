from __future__ import annotations

import asyncio
import json

from arb.capture import CaptureFrame, ConnectionBoundary
from gemini_drift import analyze

SECOND = 1_000_000_000


def ws(at_seconds: float, raw: str) -> CaptureFrame:
    at = int(at_seconds * SECOND)
    return CaptureFrame("gemini", "ws", at, at, raw, None, None, ())


def connection(at_seconds: float, connected: bool, generation: int) -> CaptureFrame:
    at = int(at_seconds * SECOND)
    return CaptureFrame(
        "gemini",
        "connection",
        at,
        at,
        None,
        None,
        None,
        (),
        connection=ConnectionBoundary(connected=connected, generation=generation, reason=None),
    )


def depth(first: int, last: int, bids: list[list[str]], asks: list[list[str]]) -> str:
    return json.dumps(
        {"e": "depthUpdate", "E": 1, "s": "dotusd", "U": first, "u": last, "b": bids, "a": asks}
    )


def test_fresh_snapshot_exposes_levels_the_incremental_stream_lost() -> None:
    frames = [
        connection(0, True, 1),
        ws(1, depth(10, 10, [["1.10", "5"], ["1.09", "5"], ["1.08", "5"]], [["1.20", "5"]])),
        # The stream removes 1.09 at t=2 and never mentions it again.
        ws(2, depth(10, 11, [["1.09", "0"]], [])),
        ws(3, depth(11, 12, [["1.10", "6"]], [])),
        connection(60, False, 1),
        connection(61, True, 2),
        # Gemini's own fresh snapshot still has 1.09, and a size the stream
        # changed a moment ago agrees.
        ws(62, depth(90, 90, [["1.10", "6"], ["1.09", "5"], ["1.08", "5"]], [["1.20", "5"]])),
    ]

    report = asyncio.run(analyze(frames, ["dotusd"])).as_payload()

    assert report["rebuilds_compared"] == {"reconnect": 1}
    assert report["frames_repeating_a_price"] == 0
    assert report["update_id_chain"] == {"contiguous": 2, "first": 2}
    assert report["levels"] == {"agree": 3, "missing_from_incremental_stale": 1}


def test_recorded_pair_resync_is_compared_and_restarts_the_chain() -> None:
    frames = [
        connection(0, True, 1),
        ws(1, depth(10, 10, [["1.10", "5"]], [["1.20", "5"], ["1.21", "5"]])),
        ws(2, depth(10, 11, [], [["1.21", "3"]])),
        ws(3, json.dumps({"id": "resync:dotusd:unsubscribe:1", "status": 200})),
        ws(4, depth(50, 50, [["1.10", "5"]], [["1.20", "5"]])),
        ws(5, json.dumps({"id": "resync:dotusd:subscribe:2", "status": 200})),
    ]

    report = asyncio.run(analyze(frames, ["dotusd"])).as_payload()

    assert report["rebuilds_compared"] == {"pair_resync": 1}
    assert report["levels_by_rebuild"] == {"pair_resync": {"agree": 2, "ghost_in_incremental": 1}}
    assert "gap" not in report["update_id_chain"]
    assert report["levels"] == {"agree": 2, "ghost_in_incremental": 1}
