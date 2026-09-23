from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from arb.capture import CaptureFrame, ConnectionBoundary
from gemini_book_audit import audit, join_episodes

SECOND = 1_000_000_000


def ws(at_seconds: float, payload: dict[str, object]) -> CaptureFrame:
    at = int(at_seconds * SECOND)
    return CaptureFrame("gemini", "ws", at, at, json.dumps(payload), None, None, ())


def connected(at_seconds: float) -> CaptureFrame:
    at = int(at_seconds * SECOND)
    boundary = ConnectionBoundary(connected=True, generation=1, reason=None)
    return CaptureFrame("gemini", "connection", at, at, None, None, None, (), boundary)


def depth(first: int, last: int, bids: list[list[str]], asks: list[list[str]]) -> dict[str, object]:
    return {"e": "depthUpdate", "E": 1, "s": "dotusd", "U": first, "u": last, "b": bids, "a": asks}


def partial(update_id: int, bids: list[list[str]], asks: list[list[str]]) -> dict[str, object]:
    return {"lastUpdateId": update_id, "symbol": "dotusd", "bids": bids, "asks": asks}


TRUE_BIDS = [["1.10", "5"], ["1.09", "5"], ["1.08", "5"]]
ASKS = [["1.20", "5"]]


def frames() -> list[CaptureFrame]:
    return [
        connected(0),
        # The incremental book lacks the 1.08 bid Gemini's snapshots show.
        ws(1, depth(10, 10, [["1.10", "5"], ["1.09", "5"]], ASKS)),
        ws(1.1, partial(10, TRUE_BIDS, ASKS)),  # aligned now: mismatch
        ws(2, partial(11, TRUE_BIDS, ASKS)),  # waits for u == 11
        ws(2.1, depth(10, 11, [], [])),  # aligned: mismatch again
        # A trade prints at 1.08 on the resting bid side: real liquidity.
        ws(2.5, {"E": 1, "s": "dotusd", "t": 1, "p": "1.08", "q": "1", "m": True}),
        ws(3, partial(12, TRUE_BIDS, ASKS)),
        ws(3.1, depth(11, 12, [["1.08", "5"]], [])),  # repaired: match closes the run
        ws(4, partial(13, TRUE_BIDS, ASKS)),
        ws(4.1, depth(12, 14, [], [])),  # passed id 13: unaligned
    ]


def test_audit_compares_only_at_the_exact_update_id() -> None:
    report = asyncio.run(audit(frames(), ["dotusd"])).as_payload()

    assert report["comparisons"] == {"aligned": 3, "match": 1, "mismatch": 2, "unaligned": 1}
    assert report["discrepancies"] == {"missing:1-4": 2}
    assert report["runs"]["count"] == 1
    assert report["runs"]["healed_by_next_comparison"] == 0
    assert report["runs"]["median_seconds"] == 1.0
    assert report["trades"] == {"book=out": 1}


def test_trade_better_than_the_book_is_adjudicated_by_the_snapshot() -> None:
    trade_frames = [
        *frames()[:3],
        ws(1.5, {"E": 1, "s": "dotusd", "t": 2, "p": "1.11", "q": "1", "m": True}),
    ]

    report = asyncio.run(audit(trade_frames, ["dotusd"])).as_payload()

    assert report["trades"] == {
        "book=out": 1,
        "better_than_book_best": 1,
        "better_than_book_best:stream_silent": 1,
    }


def test_episode_join_flags_a_phantom_gemini_price() -> None:
    report = asyncio.run(audit(frames()[:3], ["dotusd"]))
    # Gemini's bid matched its snapshot, so selling on Gemini is confirmed;
    # an ask-side episode whose Gemini ask differs would be contradicted.
    report.history["DOT-USD"][-1].book_best["a"] = Decimal("1.18")
    episodes: list[dict[str, object]] = [
        {
            "pair": "DOT-USD",
            "buy_exchange": "coinbase",
            "sell_exchange": "gemini",
            "buy_price": "1.05",
            "sell_price": "1.10",
            "start_ns": str(int(1.5 * SECOND)),
        },
        {
            "pair": "DOT-USD",
            "buy_exchange": "gemini",
            "sell_exchange": "coinbase",
            "buy_price": "1.18",
            "sell_price": "1.1815",
            "start_ns": str(int(1.5 * SECOND)),
        },
        {
            "pair": "DOT-USD",
            "buy_exchange": "gemini",
            "sell_exchange": "coinbase",
            "buy_price": "1.18",
            "sell_price": "1.19",
            "start_ns": str(9 * SECOND),
        },
    ]

    join_episodes(report, episodes, Decimal("0.1"))

    assert report.as_payload()["episodes"] == {
        "gemini_leg": 3,
        "gemini_price_confirmed": 1,
        "gemini_price_contradicted": 1,
        "phantom_at_true_price": 1,
        "no_aligned_snapshot_within_2s": 1,
    }
