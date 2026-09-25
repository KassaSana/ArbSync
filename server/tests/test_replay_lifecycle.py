"""ARB-038: replay drives recovery, disconnects, expiry, and snapshots on recorded time.

Every scenario builds a capture-v2 frame list by hand so the recorded instants
are exact, replays it twice, and checks that the canonical transitions and
lifecycle boundaries land where the recording says they happened.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from arb.capture import (
    CaptureFrame,
    CaptureHeader,
    ConnectionBoundary,
    SnapshotProvenance,
)
from arb.orderbook import OrderBookManager
from arb.replay import ReplayError, ReplayReport, _digest, replay_frames

SECOND = 1_000_000_000
BASE_WALL = 1_700_000_000 * SECOND
BASE_MONO = 5_000 * SECOND

BINANCE_URL = "https://api.binance.us/api/v3/depth?symbol={symbol}&limit=5000"


def _at(tick: float) -> tuple[int, int]:
    offset = int(tick * SECOND)
    return BASE_WALL + offset, BASE_MONO + offset


def ws(tick: float, exchange: str, raw: str) -> CaptureFrame:
    wall, mono = _at(tick)
    return CaptureFrame(
        exchange=exchange,
        kind="ws",
        wall_ns=wall,
        mono_ns=mono,
        raw=raw,
        payload=None,
        url=None,
        events=(),
    )


def connection(
    tick: float, exchange: str, connected: bool, generation: int, reason: str | None = None
) -> CaptureFrame:
    wall, mono = _at(tick)
    return CaptureFrame(
        exchange=exchange,
        kind="connection",
        wall_ns=wall,
        mono_ns=mono,
        raw=None,
        payload=None,
        url=None,
        events=(),
        connection=ConnectionBoundary(connected=connected, generation=generation, reason=reason),
    )


def snapshot(
    requested_tick: float,
    responded_tick: float,
    symbol: str,
    last_update_id: int,
    bid: str,
    ask: str,
    *,
    purpose: str = "initial_sync",
    generation: int = 1,
    recorded_tick: float | None = None,
    provenance: bool = True,
) -> CaptureFrame:
    request_wall, request_mono = _at(requested_tick)
    response_wall, response_mono = _at(responded_tick)
    record_wall, record_mono = _at(responded_tick if recorded_tick is None else recorded_tick)
    pair = f"{symbol[:-3]}-{symbol[-3:]}"
    return CaptureFrame(
        exchange="binance",
        kind="snapshot",
        wall_ns=record_wall,
        mono_ns=record_mono,
        raw=None,
        payload={"lastUpdateId": last_update_id, "bids": [[bid, "1"]], "asks": [[ask, "1"]]},
        url=BINANCE_URL.format(symbol=symbol),
        events=(),
        snapshot_provenance=(
            SnapshotProvenance(
                purpose=purpose,
                pair=pair,
                connection_generation=generation,
                request_wall_ns=request_wall,
                request_mono_ns=request_mono,
                response_wall_ns=response_wall,
                response_mono_ns=response_mono,
            )
            if provenance
            else None
        ),
    )


def binance(symbol: str, first: int, last: int, bid: str, ask: str) -> str:
    return (
        f'{{"s":"{symbol}","U":{first},"u":{last},"E":1,"b":[["{bid}","1"]],"a":[["{ask}","1"]]}}'
    )


def gemini(u: int, bid: str, ask: str) -> str:
    return (
        f'{{"e":"depthUpdate","s":"BTCUSD","U":{u},"u":{u},"E":1,'
        f'"b":[["{bid}","1"]],"a":[["{ask}","1"]]}}'
    )


def coinbase_snapshot(bid: str, ask: str) -> str:
    return (
        '{"type":"snapshot","product_id":"BTC-USD","sequence_num":10,"updates":['
        f'{{"side":"bid","price_level":"{bid}","new_quantity":"1"}},'
        f'{{"side":"offer","price_level":"{ask}","new_quantity":"1"}}]}}'
    )


def coinbase_bid(seq: int, price: str, size: str) -> str:
    return (
        f'{{"type":"update","product_id":"BTC-USD","sequence_num":{seq},"updates":['
        f'{{"side":"bid","price_level":"{price}","new_quantity":"{size}"}}]}}'
    )


def header(exchanges: dict[str, list[str]], *, strict: bool = True) -> CaptureHeader:
    return CaptureHeader(
        exchanges=exchanges,
        started_wall_ns=BASE_WALL,
        version=2,
        integrity="lossless",
        provenance="complete" if strict else "unknown",
    )


def replay_twice(head: CaptureHeader, frames: list[CaptureFrame], **kwargs: Any) -> ReplayReport:
    first = asyncio.run(replay_frames(head, frames, **kwargs))
    # A caller-supplied manager is intentionally mutated so the scenario can
    # inspect its final canonical state.  Do not feed that state into the
    # determinism check: a replay starts from a fresh pipeline in production.
    second_kwargs = dict(kwargs)
    second_kwargs.pop("book_manager", None)
    second = asyncio.run(replay_frames(head, frames, **second_kwargs))
    assert first.digest == second.digest, "replay must be deterministic"
    return first


def ticks(report: ReplayReport, kind: str) -> list[tuple[str | None, float]]:
    return [
        (event.pair, (event.mono_ns - BASE_MONO) / SECOND)
        for event in report.lifecycle
        if event.kind == kind
    ]


def test_snapshot_recorded_later_cannot_affect_earlier_frames() -> None:
    """A REST response lands at its recorded completion, never at request time.

    Three diffs arrive while the snapshot is in flight. Live, Binance's rule
    buffers them and splices the post-snapshot ranges once the response
    lands; replay used to apply the snapshot at the first diff.
    """
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        ws(2, "binance", binance("BTCUSD", 100, 105, "100.1", "101")),
        ws(3, "binance", binance("BTCUSD", 106, 110, "100.2", "101")),
        snapshot(1, 4, "BTCUSD", 101, "99", "102"),
        ws(5, "binance", binance("BTCUSD", 111, 115, "100.3", "101")),
    ]

    report = replay_twice(header({"binance": ["BTCUSD"]}), frames)

    assert report.timing_fidelity == "recorded"
    kinds_at = [(t.kind, (t.mono_ns - BASE_MONO) / SECOND) for t in report.transitions]
    assert kinds_at == [("snapshot", 4.0), ("delta", 4.0), ("delta", 4.0), ("delta", 5.0)]
    assert report.transitions[0].sequence == 101
    # The spliced diffs are the recorded ranges, in order, none applied early.
    assert [t.bids[0][0] for t in report.transitions[1:]] == ["100.1", "100.2", "100.3"]
    assert ticks(report, "snapshot_requested") == [("BTC-USD", 1.0)]
    assert ticks(report, "snapshot_completed") == [("BTC-USD", 4.0)]
    assert report.observations[0].mono_ns == BASE_MONO + 4 * SECOND
    assert report.snapshots_consumed == 1
    assert report.snapshots_unmatched == 0


def test_interleaved_binance_pairs_complete_on_their_own_recorded_times() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        ws(2, "binance", binance("ETHUSD", 50, 55, "200", "201")),
        snapshot(2, 3, "ETHUSD", 60, "199", "202"),
        ws(4, "binance", binance("BTCUSD", 100, 105, "100.1", "101")),
        ws(5, "binance", binance("ETHUSD", 61, 65, "200.1", "201")),
        snapshot(1, 6, "BTCUSD", 100, "99", "102"),
        ws(7, "binance", binance("BTCUSD", 106, 110, "100.2", "101")),
    ]

    report = replay_twice(header({"binance": ["BTCUSD", "ETHUSD"]}), frames)

    by_pair: dict[str, list[tuple[str, int, float]]] = {}
    for t in report.transitions:
        by_pair.setdefault(t.pair, []).append(
            (t.kind, t.sequence, (t.mono_ns - BASE_MONO) / SECOND)
        )
    assert by_pair["ETH-USD"] == [("snapshot", 60, 3.0), ("delta", 61, 5.0)]
    assert by_pair["BTC-USD"] == [("snapshot", 100, 6.0), ("delta", 101, 6.0), ("delta", 102, 7.0)]
    assert ticks(report, "snapshot_completed") == [("ETH-USD", 3.0), ("BTC-USD", 6.0)]


def test_disconnect_and_reconnect_generations_drive_canonical_invalidation() -> None:
    """A recorded disconnect clears the venue's books and closes its episodes.

    The route reopens only once the new generation has rebuilt the book.
    Frames recorded in the gap (none should exist live) are skipped, not
    applied to a book the live process no longer trusted.
    """
    frames = [
        connection(0, "gemini", True, 1),
        connection(0, "coinbase", True, 1),
        ws(1, "gemini", gemini(1, "100", "102")),
        ws(2, "coinbase", coinbase_snapshot("99", "104")),
        ws(3, "coinbase", coinbase_bid(11, "103", "1")),  # open: buy gemini, sell coinbase
        connection(5, "gemini", False, 1, reason="socket closed"),
        ws(6, "gemini", gemini(2, "100", "102")),  # recorded in the gap: skipped
        connection(7, "gemini", True, 2),
        ws(8, "gemini", gemini(1, "100", "102")),  # fresh snapshot: route reopens
    ]

    manager = OrderBookManager(max_age_seconds=60.0)
    report = replay_twice(
        header({"gemini": ["btcusd"], "coinbase": ["BTC-USD"]}), frames, book_manager=manager
    )

    assert [(e.is_open, e.close_reason) for e in report.opportunities] == [
        (True, None),
        (False, "book_ineligible"),
        (True, None),
        (False, "shutdown"),
    ]
    opened, closed, reopened, _ = report.opportunities
    assert opened.start_ns == BASE_WALL + 3 * SECOND
    assert closed.end_ns == BASE_WALL + 5 * SECOND
    assert reopened.start_ns == BASE_WALL + 8 * SECOND
    assert ticks(report, "disconnected") == [(None, 5.0)]
    assert [e.detail for e in report.lifecycle if e.kind == "connected"] == [
        "generation=1",
        "generation=1",
        "generation=2",
    ]
    assert report.frames_skipped_disconnected == 1
    assert [t.mono_ns for t in report.transitions if t.exchange == "gemini"] == [
        BASE_MONO + 1 * SECOND,
        BASE_MONO + 8 * SECOND,
    ]


def test_snapshot_from_another_generation_is_a_provenance_mismatch() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(1, 3, "BTCUSD", 100, "99", "102", generation=2),
    ]

    with pytest.raises(ReplayError, match="provenance mismatch.*generation 2"):
        asyncio.run(replay_frames(header({"binance": ["BTCUSD"]}), frames))


def test_sequence_gap_closes_episode_at_the_gap_and_recovers_only_that_pair() -> None:
    frames = [
        ws(0, "gemini", gemini(1, "100", "102")),
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 1, 5, "103", "105")),
        snapshot(1, 2, "BTCUSD", 3, "103", "105"),  # open: buy gemini, sell binance
        ws(3, "binance", binance("ETHUSD", 1, 3, "200", "201")),
        snapshot(3, 4, "ETHUSD", 2, "200", "201"),
        ws(5, "binance", binance("BTCUSD", 6, 6, "103", "105")),
        ws(6, "binance", binance("ETHUSD", 4, 4, "200", "201")),
        ws(7, "binance", binance("BTCUSD", 20, 20, "103", "105")),  # gap: reset
        ws(8, "binance", binance("ETHUSD", 5, 5, "200", "201")),
        snapshot(7, 9, "BTCUSD", 21, "103", "105", purpose="sequence_gap"),
        ws(10, "binance", binance("BTCUSD", 22, 25, "103", "105")),
    ]

    manager = OrderBookManager(max_age_seconds=60.0)
    report = replay_twice(
        header({"gemini": ["btcusd"], "binance": ["BTCUSD", "ETHUSD"]}),
        frames,
        book_manager=manager,
    )

    assert [
        (e.is_open, e.close_reason, (e.end_ns or e.start_ns) - BASE_WALL)
        for e in report.opportunities
    ] == [
        (True, None, 2 * SECOND),
        (False, "book_ineligible", 7 * SECOND),
        (True, None, 9 * SECOND),
        (False, "shutdown", 10 * SECOND),
    ]
    eth = [t for t in report.transitions if t.pair == "ETH-USD"]
    # The first delta was buffered while ETH's snapshot was in flight, then
    # the two later stream updates applied normally.
    assert [t.kind for t in eth] == ["snapshot", "delta", "delta", "delta"]
    assert all(t.accepted for t in eth)
    assert [
        (e.pair, e.detail.split()[0]) for e in report.lifecycle if e.kind == "snapshot_completed"
    ] == [
        ("BTC-USD", "purpose=initial_sync"),
        ("ETH-USD", "purpose=initial_sync"),
        ("BTC-USD", "purpose=sequence_gap"),
    ]
    assert manager.eligibility("binance", "ETH-USD", BASE_MONO + 10 * SECOND).eligible
    assert manager.eligibility("binance", "BTC-USD", BASE_MONO + 10 * SECOND).eligible
    assert not any(e.kind == "reconnect_requested" for e in report.lifecycle)


def test_quiet_book_expires_at_the_deterministic_age_boundary() -> None:
    """No frame arrives for 98 s; the episode still closes at max_age + 1 ns."""
    frames = [
        ws(0, "gemini", gemini(1, "100", "102")),
        ws(1, "coinbase", coinbase_snapshot("99", "104")),
        ws(2, "coinbase", coinbase_bid(11, "103", "1")),  # open
        ws(100, "coinbase", coinbase_bid(12, "103", "1")),
    ]
    head = header({"gemini": ["btcusd"], "coinbase": ["BTC-USD"]})

    report = replay_twice(head, frames, max_age_seconds=30.0)

    assert [(e.is_open, e.close_reason) for e in report.opportunities] == [
        (True, None),
        (False, "book_ineligible"),
    ]
    closed = report.opportunities[1]
    assert closed.end_ns == BASE_WALL + 30 * SECOND + 1
    assert closed.duration_ns == 28 * SECOND + 1
    assert [
        (e.pair, e.mono_ns - BASE_MONO) for e in report.lifecycle if e.kind == "book_expired"
    ] == [
        ("BTC-USD", 30 * SECOND + 1),  # gemini, last received at t=0
        ("BTC-USD", 32 * SECOND + 1),  # coinbase, last received at t=2
    ]
    # At t=100 coinbase is fresh again but gemini is still too old: no reopen.
    assert report.observations[-1].eligible is True
    assert report.observations[-1].exchange == "coinbase"

    wider = asyncio.run(replay_frames(head, frames, max_age_seconds=40.0))
    assert wider.digest != report.digest
    assert wider.opportunities[1].end_ns == BASE_WALL + 40 * SECOND + 1


def test_expiry_never_runs_past_the_last_recorded_instant() -> None:
    frames = [
        ws(0, "gemini", gemini(1, "100", "102")),
        ws(1, "coinbase", coinbase_snapshot("99", "104")),
        ws(2, "coinbase", coinbase_bid(11, "103", "1")),  # open
        ws(20, "coinbase", coinbase_bid(12, "103", "1")),
    ]

    report = replay_twice(
        header({"gemini": ["btcusd"], "coinbase": ["BTC-USD"]}), frames, max_age_seconds=30.0
    )

    assert [(e.is_open, e.close_reason) for e in report.opportunities] == [
        (True, None),
        (False, "shutdown"),
    ]
    assert report.opportunities[1].end_ns == BASE_WALL + 20 * SECOND
    assert not any(e.kind == "book_expired" for e in report.lifecycle)


def test_rejected_snapshot_requests_scoped_recovery_and_consumes_its_snapshot() -> None:
    """A crossed REST snapshot is rejected by the book manager.

    Live, `consume_adapter` asks Binance.US for a scoped resync; the next
    update for that pair re-fetches under `scoped_recovery` while the other
    pair keeps streaming. Replay must take the same path or the recorded
    scoped-recovery response would never be matched.
    """
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 1, 5, "100", "101")),
        snapshot(1, 2, "BTCUSD", 3, "105", "100"),  # crossed: rejected
        ws(3, "binance", binance("ETHUSD", 1, 3, "200", "201")),
        snapshot(3, 4, "ETHUSD", 2, "200", "201"),
        ws(5, "binance", binance("BTCUSD", 6, 6, "100", "101")),
        snapshot(5, 6, "BTCUSD", 5, "100", "101", purpose="scoped_recovery"),
        ws(7, "binance", binance("ETHUSD", 4, 4, "200", "201")),
    ]

    manager = OrderBookManager(max_age_seconds=60.0)
    report = replay_twice(header({"binance": ["BTCUSD", "ETHUSD"]}), frames, book_manager=manager)

    crossed = report.transitions[0]
    assert (crossed.kind, crossed.accepted, crossed.reason) == (
        "snapshot",
        False,
        "snapshot_crossed",
    )
    assert ticks(report, "scoped_resync") == [("BTC-USD", 2.0)]
    assert [
        (e.pair, e.detail.split()[0], (e.mono_ns - BASE_MONO) / SECOND)
        for e in report.lifecycle
        if e.kind == "snapshot_requested"
    ] == [
        ("BTC-USD", "purpose=initial_sync", 1.0),
        ("ETH-USD", "purpose=initial_sync", 3.0),
        ("BTC-USD", "purpose=scoped_recovery", 5.0),
    ]
    assert not any(e.kind == "reconnect_requested" for e in report.lifecycle)
    assert all(t.accepted for t in report.transitions if t.pair == "ETH-USD")
    assert manager.eligibility("binance", "BTC-USD", BASE_MONO + 7 * SECOND).eligible
    assert report.snapshots_consumed == 3
    assert report.snapshots_unmatched == 0


def test_reconciliation_snapshots_are_never_sync_candidates() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(1, 3, "BTCUSD", 100, "99", "102", purpose="reconciliation"),
    ]

    with pytest.raises(ReplayError, match="no snapshot data for binance BTC-USD"):
        asyncio.run(replay_frames(header({"binance": ["BTCUSD"]}), frames))


def test_snapshot_completed_before_request_is_a_provenance_mismatch() -> None:
    frames = [
        connection(0, "binance", True, 1),
        snapshot(0.2, 0.5, "BTCUSD", 100, "99", "102", recorded_tick=0.5),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
    ]

    with pytest.raises(ReplayError, match="completed .* before replay requested it"):
        asyncio.run(replay_frames(header({"binance": ["BTCUSD"]}), frames))


def test_legacy_capture_replays_in_url_order_with_flagged_fidelity() -> None:
    """Without provenance the record time is the completion time, and an
    earlier record completes as soon as it is requested."""
    frames = [
        snapshot(0, 0, "BTCUSD", 100, "99", "102", provenance=False),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        ws(2, "binance", binance("BTCUSD", 100, 105, "100.1", "101")),
        snapshot(2, 4, "BTCUSD", 106, "99", "102", provenance=False),
        ws(3, "binance", binance("BTCUSD", 106, 106, "100.2", "101")),
        ws(5, "binance", binance("BTCUSD", 107, 107, "100.3", "101")),
    ]

    report = replay_twice(header({"binance": ["BTCUSD"]}, strict=False), frames)

    assert report.timing_fidelity == "legacy_snapshot_order"
    kinds_at = [(t.kind, (t.mono_ns - BASE_MONO) / SECOND) for t in report.transitions]
    assert kinds_at == [("snapshot", 1.0), ("delta", 2.0), ("delta", 3.0), ("delta", 5.0)]
    assert report.snapshots_consumed == 1
    assert report.snapshots_unmatched == 1


def test_unmatched_recovery_snapshots_are_counted_not_fatal() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(1, 2, "BTCUSD", 100, "99", "102"),
        # A reconciler-triggered recovery the replay does not reproduce.
        snapshot(3, 4, "BTCUSD", 120, "99", "102", purpose="scoped_recovery"),
        snapshot(5, 6, "BTCUSD", 130, "99", "102", purpose="reconciliation"),
    ]

    report = replay_twice(header({"binance": ["BTCUSD"]}), frames)

    assert report.snapshots_consumed == 1
    assert report.snapshots_unmatched == 1
    assert report.snapshots_skipped_reconciliation == 1


def test_adapter_reconnect_request_waits_for_the_recorded_boundary() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(1, 2, "BTCUSD", 100, "99", "102"),
        ws(3, "binance", '{"e":"serverShutdown"}'),
        ws(4, "binance", binance("BTCUSD", 100, 105, "100.1", "101")),  # after the request
        connection(5, "binance", False, 1, reason="adapter requested reconnect"),
        connection(7, "binance", True, 2),
        ws(8, "binance", binance("BTCUSD", 200, 205, "100", "101")),
        snapshot(8, 9, "BTCUSD", 201, "99", "102", generation=2),
    ]

    manager = OrderBookManager(max_age_seconds=60.0)
    report = replay_twice(header({"binance": ["BTCUSD"]}), frames, book_manager=manager)

    assert ticks(report, "reconnect_requested") == [(None, 3.0)]
    assert report.frames_skipped_disconnected == 1
    assert report.legacy_immediate_reconnects == 0
    assert [(t.kind, (t.mono_ns - BASE_MONO) / SECOND) for t in report.transitions] == [
        ("snapshot", 2.0),
        ("snapshot", 9.0),
        ("delta", 9.0),
    ]
    # The book stood until the recorded disconnect, then was cleared.
    assert manager.eligibility("binance", "BTC-USD", BASE_MONO + 9 * SECOND).eligible
    assert [e.detail for e in report.lifecycle if e.kind == "connected"][-1] == "generation=2"


def test_digest_covers_lifecycle_boundaries() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(1, 2, "BTCUSD", 100, "99", "102"),
        connection(3, "binance", False, 1, reason="socket closed"),
    ]

    report = replay_twice(header({"binance": ["BTCUSD"]}), frames)

    assert report.lifecycle, "scenario must produce lifecycle boundaries"
    report.lifecycle.pop()
    assert _digest(report) != report.digest


def test_legacy_reconnect_without_connection_frames_resets_immediately() -> None:
    frames = [
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(1, 2, "BTCUSD", 100, "99", "102", provenance=False),
        ws(3, "binance", '{"e":"serverShutdown"}'),
        ws(4, "binance", binance("BTCUSD", 100, 105, "100.1", "101")),
        snapshot(4, 5, "BTCUSD", 101, "99", "102", provenance=False),
    ]

    report = replay_twice(header({"binance": ["BTCUSD"]}, strict=False), frames)

    assert report.legacy_immediate_reconnects == 1
    assert report.frames_skipped_disconnected == 0
    assert [t.kind for t in report.transitions] == ["snapshot", "snapshot", "delta"]


def test_replay_rejects_non_positive_speed_and_keeps_threshold() -> None:
    frames = [ws(0, "gemini", gemini(1, "100", "102"))]
    with pytest.raises(ReplayError, match="speed"):
        asyncio.run(replay_frames(header({"gemini": ["btcusd"]}), frames, speed=0))
    report = asyncio.run(
        replay_frames(header({"gemini": ["btcusd"]}), frames, threshold_pct=Decimal("5"))
    )
    assert report.opportunities == []


def failed_fetch(requested_tick: float, failed_tick: float, symbol: str) -> CaptureFrame:
    request_wall, request_mono = _at(requested_tick)
    failed_wall, failed_mono = _at(failed_tick)
    return CaptureFrame(
        exchange="binance",
        kind="snapshot",
        wall_ns=failed_wall,
        mono_ns=failed_mono,
        raw=None,
        payload=None,
        url=BINANCE_URL.format(symbol=symbol),
        events=(),
        snapshot_provenance=SnapshotProvenance(
            purpose="initial_sync",
            pair=f"{symbol[:-3]}-{symbol[-3:]}",
            connection_generation=1,
            request_wall_ns=request_wall,
            request_mono_ns=request_mono,
            response_wall_ns=failed_wall,
            response_mono_ns=failed_mono,
        ),
        snapshot_error="ConnectTimeout('timed out')",
    )


def failed_connection_frames(*, record_failure: bool) -> list[CaptureFrame]:
    # Live: BTCUSD's initial snapshot fetch raised, which dropped the socket;
    # ETHUSD's fetch was still in flight and died with it. Generation 2 then
    # synchronized normally.
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        ws(1, "binance", binance("ETHUSD", 45, 49, "200", "201")),
        *([failed_fetch(1, 2, "BTCUSD")] if record_failure else []),
        connection(2.1, "binance", False, 1, reason="snapshot retrieval failed for BTC-USD"),
        connection(3, "binance", True, 2),
        ws(4, "binance", binance("BTCUSD", 200, 205, "100", "101")),
        snapshot(4, 5, "BTCUSD", 201, "99", "102", generation=2),
    ]
    return frames


@pytest.mark.parametrize("record_failure", [True, False])
def test_failed_snapshot_fetch_replays_the_recorded_reconnect(record_failure: bool) -> None:
    # ARB-051: a fetch that raised left no response. Recorded failures replay
    # exactly; older captures infer it from the disconnect reason, and the
    # other request cancelled in flight is abandoned rather than matched to
    # the next connection's snapshot.
    head = header({"binance": ["BTCUSD", "ETHUSD"]})
    report = replay_twice(head, failed_connection_frames(record_failure=record_failure))

    failed_at = 2.0 if record_failure else 2.1 - 1e-9
    assert [pair for pair, _ in ticks(report, "snapshot_failed")] == ["BTC-USD"]
    assert ticks(report, "snapshot_failed")[0][1] == pytest.approx(failed_at)
    assert [pair for pair, _ in ticks(report, "reconnect_requested")] == [None]
    assert report.snapshots_failed == 1
    assert report.snapshots_failed_inferred == (0 if record_failure else 1)
    assert report.snapshots_cancelled_in_flight == 1
    # A recorded failure is a consumed record too.
    assert report.snapshots_consumed == (2 if record_failure else 1)
    assert report.snapshots_unmatched == 0
    btc = [(t.kind, t.accepted) for t in report.transitions if t.pair == "BTC-USD"]
    assert btc[-2:] == [("snapshot", True), ("delta", True)]


def test_a_missing_response_on_a_connection_that_never_ended_still_fails_loudly() -> None:
    frames = [
        connection(0, "binance", True, 1),
        ws(1, "binance", binance("BTCUSD", 95, 99, "100", "101")),
        snapshot(4, 5, "BTCUSD", 201, "99", "102", generation=2),
    ]

    with pytest.raises(ReplayError, match="generation"):
        asyncio.run(replay_frames(header({"binance": ["BTCUSD"]}), frames))
