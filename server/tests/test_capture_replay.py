from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from arb import main
from arb.capture import CaptureFrame, CaptureHeader, CaptureWriter
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
    assert first.observations == second.observations
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


def test_replay_processes_connection_lifecycle_without_market_transitions(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"

    async def scenario() -> None:
        writer = CaptureWriter(path, {"gemini": ["btcusd"]})
        task = asyncio.create_task(writer.run())
        assert writer.record_connection("gemini", True, 1, wall_ns=1, mono_ns=1)
        assert writer.record_ws("gemini", _gemini_snapshot(), [], wall_ns=2, mono_ns=2)
        assert writer.record_connection(
            "gemini", False, 1, wall_ns=3, mono_ns=3, reason="socket closed"
        )
        await writer.close()
        await task

    asyncio.run(scenario())
    report = asyncio.run(replay_file(path))

    assert len(report.transitions) == 1
    assert report.transitions[0].kind == "snapshot"
    assert [event.kind for event in report.lifecycle] == ["connected", "disconnected"]


def test_replay_observations_capture_canonical_post_apply_tops() -> None:
    second = 1_000_000_000
    header = CaptureHeader(exchanges={"gemini": ["btcusd"]}, started_wall_ns=second)

    def frame(index: int, raw: str) -> CaptureFrame:
        return CaptureFrame(
            exchange="gemini",
            kind="ws",
            wall_ns=second + index * second,
            mono_ns=second + index * second,
            raw=raw,
            payload=None,
            url=None,
            events=(),
        )

    frames = [
        frame(
            0,
            '{"e":"depthUpdate","s":"BTCUSD","U":1,"u":1,"E":1,'
            '"b":[["100","1"],["99","1"]],"a":[["101","1"],["102","1"]]}',
        ),
        # Deep-only bid update: the canonical top remains 100/101.
        frame(
            1,
            '{"e":"depthUpdate","s":"BTCUSD","U":2,"u":2,"E":2,"b":[["99","2"]],"a":[]}',
        ),
        # One-sided best-ask update.
        frame(
            2,
            '{"e":"depthUpdate","s":"BTCUSD","U":3,"u":3,"E":3,"b":[],"a":[["100.5","1"]]}',
        ),
        # Deleting that best ask exposes the next canonical level.
        frame(
            3,
            '{"e":"depthUpdate","s":"BTCUSD","U":4,"u":4,"E":4,"b":[],"a":[["100.5","0"]]}',
        ),
    ]

    report = asyncio.run(replay_frames(header, frames))

    assert [
        (observation.sequence, observation.best_bid_price, observation.best_ask_price)
        for observation in report.observations
    ] == [
        (1, "100", "101"),
        (2, "100", "101"),
        (3, "100", "100.5"),
        (4, "100", "101"),
    ]
    assert [transition.bids for transition in report.transitions] == [
        (("100", "1"), ("99", "1")),
        (("99", "2"),),
        (),
        (),
    ]


def test_replay_serves_snapshots_per_request_url(tmp_path: Path) -> None:
    """Snapshots for different pairs must not leak into each other's sync."""
    from arb.capture import read_capture

    async def scenario() -> tuple[str, str, int]:
        path = tmp_path / "capture.jsonl"
        writer = CaptureWriter(path, {"binance": ["BTCUSD", "ETHUSD"]})
        task = asyncio.create_task(writer.run())
        assert writer.record_ws(
            "binance", '{"s":"BTCUSD","U":95,"u":99,"E":1,"b":[["100","1"]],"a":[]}', []
        )
        assert writer.record_ws(
            "binance", '{"s":"ETHUSD","U":50,"u":55,"E":2,"b":[["200","1"]],"a":[]}', []
        )
        assert writer.record_snapshot(
            "binance",
            "https://api.binance.us/api/v3/depth?symbol=ETHUSD&limit=5000",
            {"lastUpdateId": 60, "bids": [["199", "1"]], "asks": [["201", "1"]]},
        )
        assert writer.record_snapshot(
            "binance",
            "https://api.binance.us/api/v3/depth?symbol=BTCUSD&limit=5000",
            {"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]},
        )
        assert writer.record_ws(
            "binance", '{"s":"BTCUSD","U":100,"u":105,"E":3,"b":[["99.5","1"]],"a":[]}', []
        )
        assert writer.record_ws(
            "binance", '{"s":"ETHUSD","U":60,"u":65,"E":4,"b":[["199.5","1"]],"a":[]}', []
        )
        await writer.close()
        await task
        header, frames = read_capture(path)
        first = await replay_frames(header, frames)
        second = await replay_frames(header, frames)
        pairs = {(t.pair, t.kind, t.sequence) for t in first.transitions}
        return first.digest, second.digest, len(pairs)

    first_digest, second_digest, distinct = asyncio.run(scenario())

    assert first_digest == second_digest
    assert distinct == 4


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
async def test_process_market_event_honors_recorded_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    # Both the wall-clock identity and the recorded monotonic instant reach
    # the detector, which is what makes replayed lifetimes deterministic.
    detector.detect_for_pair.assert_called_once_with("BTC-USD", [], 999, 1_000)
    assert manager.eligibility("gemini", "BTC-USD", 2_000).age_ns == 1_000


FEED_CONFIG = """
[detector]
threshold_pct = 0.1

[exchanges]
gemini = ["btcusd"]
coinbase = ["BTC-USD"]
binance = ["BTCUSD"]

[server]
host = "127.0.0.1"
port = 8000
database_path = "arb.sqlite3"
cors_allowed_origins = []

[persistence]
batch_size = 10
flush_interval_seconds = 0.05
queue_maxsize = 100

[fees]
gemini = { taker_pct = 0.40 }
coinbase = { taker_pct = 0.60 }
binance = { taker_pct = 0.60 }
"""


@pytest.mark.asyncio
async def test_feed_replay_into_pipeline_persists_and_publishes(tmp_path: Path) -> None:
    from arb.config import load_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(FEED_CONFIG)
    pipeline = main.build_pipeline(load_config(config_path), adapter_types=[])
    await pipeline.store.initialize()
    persistence_task = asyncio.create_task(pipeline.store.run())
    capture_path = tmp_path / "capture.jsonl"
    await _write_three_venue_capture(capture_path)

    report = await main.feed_replay_into_pipeline(pipeline, capture_path, None)

    await pipeline.store.close()
    await persistence_task
    await pipeline.broadcaster.aclose()
    assert report.transitions
    assert report.opportunities
    rows = await pipeline.store.recent()
    assert any(row["pair"] == "BTC-USD" for row in rows)
    assert pipeline.book_manager.top_of_book("gemini", "BTC-USD") is not None


def test_replay_reproduces_episode_boundaries_deterministically() -> None:
    """ARB-031: episode open, peak, close and end-of-capture shutdown on a replay.

    A Coinbase bid steps above Gemini's ask, widens, narrows, disappears and
    then reappears. Five updates touch the route while the spread rests, but
    the report holds exactly two episodes, each with the boundaries the frame
    stamps dictate: wall-clock start from `wall_ns`, lifetime from `mono_ns`.
    """
    from arb.capture import CaptureFrame, CaptureHeader

    second = 1_000_000_000
    base_wall = 1_700_000_000 * second
    base_mono = 5_000 * second

    def ws(index: int, exchange: str, raw: str) -> CaptureFrame:
        return CaptureFrame(
            exchange=exchange,
            kind="ws",
            wall_ns=base_wall + index * second,
            mono_ns=base_mono + index * second,
            raw=raw,
            payload=None,
            url=None,
            events=(),
        )

    def coinbase(seq: int, side: str, price: str, size: str) -> str:
        return (
            f'{{"type":"update","product_id":"BTC-USD","sequence_num":{seq},"updates":['
            f'{{"side":"{side}","price_level":"{price}","new_quantity":"{size}"}}]}}'
        )

    header = CaptureHeader(
        exchanges={"gemini": ["btcusd"], "coinbase": ["BTC-USD"]}, started_wall_ns=base_wall
    )
    frames = [
        ws(0, "gemini", _gemini_snapshot()),  # gemini bid 100 / ask 102
        ws(1, "coinbase", _coinbase_snapshot()),  # coinbase bid 99 / ask 104: no spread
        ws(2, "coinbase", coinbase(11, "bid", "103", "2")),  # open: (103-102)/102
        ws(3, "coinbase", coinbase(12, "bid", "103.5", "0.5")),  # peak: (103.5-102)/102
        ws(4, "coinbase", coinbase(13, "bid", "103.5", "0")),  # back to 103: still open
        ws(5, "coinbase", coinbase(14, "bid", "103", "0")),  # bid 99 again: close
        ws(6, "coinbase", coinbase(15, "bid", "103", "1")),  # reopen at the last frame
    ]

    first = asyncio.run(replay_frames(header, frames, threshold_pct=Decimal("0.1")))
    second_run = asyncio.run(replay_frames(header, frames, threshold_pct=Decimal("0.1")))
    assert first.digest == second_run.digest

    events = first.opportunities
    assert [(e.is_open, e.close_reason) for e in events] == [
        (True, None),
        (False, "spread_closed"),
        (True, None),
        (False, "shutdown"),
    ]
    opened, closed, reopened, shutdown = events
    assert (opened.buy_exchange, opened.sell_exchange) == ("gemini", "coinbase")
    assert opened.start_ns == base_wall + 2 * second
    assert closed.start_ns == opened.start_ns
    assert closed.end_ns == base_wall + 5 * second
    assert closed.duration_ns == 3 * second
    assert closed.spread_pct == Decimal(1) / Decimal(102) * 100  # at open
    assert closed.peak_spread_pct == Decimal("1.5") / Decimal(102) * 100
    assert closed.peak_size == Decimal("0.5")
    assert closed.peak_profit == Decimal("0.5") * Decimal("1.5")
    assert closed.close_spread_pct == Decimal(99 - 102) / Decimal(102) * 100
    # The capture ends with a spread standing: it closes at the last recorded
    # instant with zero lifetime rather than dangling.
    assert reopened.start_ns == base_wall + 6 * second
    assert shutdown.end_ns == reopened.start_ns
    assert shutdown.duration_ns == 0


def test_replay_samples_depth_on_a_thin_book_and_through_a_resync_window() -> None:
    """ARB-032: fill rates from a replay are deterministic and never fabricated.

    Gemini's DOT book is thin (about $510 a side), so $100 fills and $10,000
    never does. Binance's book covers $10,000 but goes through a sequence gap
    whose replacement snapshot only lands three sampling intervals later on the
    recorded clock; those samples count as ineligible, not as depth shortfalls.
    """
    from arb.capture import CaptureFrame, CaptureHeader
    from arb.fillrates import (
        INELIGIBLE_REASONS,
        FillRateItem,
        FillRateSession,
        aggregate,
        rows_payload,
    )
    from arb.orderbook import OrderBookManager
    from arb.pricing import DepthSampler

    second = 1_000_000_000
    base_wall = 1_700_000_000 * second
    base_mono = 9_000 * second

    def frame(
        index: int, exchange: str, raw: str | None, payload: dict[str, Any] | None = None
    ) -> CaptureFrame:
        return CaptureFrame(
            exchange=exchange,
            kind="ws" if raw is not None else "snapshot",
            wall_ns=base_wall + index * second,
            mono_ns=base_mono + index * second,
            raw=raw,
            payload=payload,
            url=None
            if raw is not None
            else "https://api.binance.us/api/v3/depth?symbol=DOTUSD&limit=5000",
            events=(),
        )

    def gemini(u: int, bid: str, ask: str) -> str:
        return (
            f'{{"e":"depthUpdate","s":"DOTUSD","U":{u},"u":{u},"E":1,'
            f'"b":[["{bid}","100"]],"a":[["{ask}","100"]]}}'
        )

    def binance(first: int, last: int, bid: str, ask: str) -> str:
        return (
            f'{{"s":"DOTUSD","U":{first},"u":{last},"E":1,'
            f'"b":[["{bid}","3000"]],"a":[["{ask}","3000"]]}}'
        )

    deep = {"lastUpdateId": 3, "bids": [["5", "3000"]], "asks": [["5.1", "3000"]]}
    header = CaptureHeader(
        exchanges={"gemini": ["dotusd"], "binance": ["DOTUSD"]}, started_wall_ns=base_wall
    )
    frames = [
        frame(0, "gemini", gemini(1, "5", "5.1")),
        frame(1, "binance", None, deep),
        frame(1, "binance", binance(1, 5, "5", "5.1")),
        frame(2, "binance", binance(6, 6, "5", "5.1")),
        frame(3, "binance", binance(20, 20, "5", "5.1")),  # gap: RESET, ineligible
        frame(6, "binance", None, {**deep, "lastUpdateId": 21}),
        frame(6, "binance", binance(21, 25, "5", "5.1")),  # resync completes
        frame(7, "gemini", gemini(2, "5", "5.1")),
    ]

    items: list[FillRateItem] = []

    def run() -> tuple[list[dict[str, object]], int, str]:
        items.clear()
        manager = OrderBookManager(max_age_seconds=60.0)
        sampler = DepthSampler(
            manager,
            [Decimal("100"), Decimal("10000")],
            {"gemini": None, "binance": 5000},
            {},
            interval_seconds=1.0,
            roster=[("binance", "DOT-USD"), ("coinbase", "DOT-USD"), ("gemini", "DOT-USD")],
            sink=items.append,
        )
        report = asyncio.run(
            replay_frames(header, frames, book_manager=manager, depth_sampler=sampler)
        )
        return sampler.fill_rates.rows(), sampler.samples, report.digest

    rows, samples, digest = run()
    first_items = list(items)
    again, samples_again, digest_again = run()
    assert (rows, samples, digest) == (again, samples_again, digest_again)
    # ARB-042: the replay emits the same session and minute buckets every time,
    # and the reference window reducer over them reproduces the session totals.
    assert first_items == items
    session = items[0]
    assert isinstance(session, FillRateSession)
    assert session.started_wall_ns == base_wall
    window = aggregate(items, 0, base_wall + 3_600 * second)
    assert window.sessions[session.session_id].samples == 7
    assert window.sessions[session.session_id].missed_samples == 0
    assert rows_payload(window.groups[session.config_fingerprint], session.config) == rows

    # A configured book that never receives data is counted in every sample.
    never = {(r["exchange"], r["notional"], r["side"]): r for r in rows}[("coinbase", "100", "buy")]
    assert never["samples"] == 7 and never["ineligible"] == {
        **dict.fromkeys(INELIGIBLE_REASONS, 0),
        "missing": 7,
    }
    assert never["fill_rate"] is None and never["eligible_share"] == 0.0

    # Samples fire after same-instant frames at t=1..7: seven samples on the
    # recorded clock.
    assert samples == 7
    by_key = {(r["exchange"], r["notional"], r["side"]): r for r in rows}
    thin_small = by_key[("gemini", "100", "buy")]
    thin_large = by_key[("gemini", "10000", "buy")]
    assert thin_small["observations"] == 7 and thin_small["filled"] == 7
    assert thin_large["observations"] == 7 and thin_large["filled"] == 0
    assert thin_large["subscribed_depth_levels"] is None

    deep_large = by_key[("binance", "10000", "buy")]
    # The recorded snapshot completes at t=1, so the t=1 and t=2 samples see an
    # eligible book. The gap frame at t=3 lands before that instant's sample,
    # so t=3, t=4 and t=5 are ineligible; the replacement snapshot recorded at
    # t=6 completes before the t=6 sample, so t=6 and t=7 are eligible again.
    assert deep_large["observations"] == 4 and deep_large["filled"] == 4
    assert deep_large["ineligible_samples"] == 3
    assert deep_large["samples"] == 7
    assert deep_large["subscribed_depth_levels"] == 5000


def _crossed_gemini_delta() -> str:
    # A bid above the standing ask: the adapter emits the delta and the book
    # manager rejects it, and a rejected update still changes eligibility.
    return (
        '{"e":"depthUpdate","s":"BTCUSD","U":2,"u":2,"E":2000,"b":[["105","1"]],"a":[["102","1"]]}'
    )


def _write_observer_capture(path: Path) -> None:
    async def scenario() -> None:
        writer = CaptureWriter(path, {"gemini": ["btcusd"]})
        task = asyncio.create_task(writer.run())
        assert writer.record_connection("gemini", True, 1, wall_ns=1_000, mono_ns=1_000)
        assert writer.record_ws("gemini", _gemini_snapshot(), [], wall_ns=2_000, mono_ns=2_000)
        assert writer.record_ws("gemini", _crossed_gemini_delta(), [], wall_ns=3_000, mono_ns=3_000)
        assert writer.record_connection(
            "gemini", False, 1, wall_ns=4_000, mono_ns=4_000, reason="socket closed"
        )
        await writer.close()
        await task

    asyncio.run(scenario())


def test_book_observer_sees_rejected_updates_and_connection_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    _write_observer_capture(path)

    seen: list[tuple[str, str, int, int]] = []
    observed = asyncio.run(
        replay_file(
            path,
            book_observer=lambda exchange, pair, wall_ns, mono_ns: seen.append(
                (exchange, pair, wall_ns, mono_ns)
            ),
        )
    )
    plain = asyncio.run(replay_file(path))

    assert observed.digest == plain.digest
    assert any(not transition.accepted for transition in observed.transitions)
    # The initial "connected" boundary precedes any book, so it notifies
    # nothing; the snapshot, the rejected delta, and the disconnect each do.
    assert seen == [
        ("gemini", "BTC-USD", 2_000, 2_000),
        ("gemini", "BTC-USD", 3_000, 3_000),
        ("gemini", "BTC-USD", 4_000, 4_000),
    ]


def test_book_observer_sees_age_expiry(tmp_path: Path) -> None:
    second = 1_000_000_000
    path = tmp_path / "capture.jsonl"

    async def scenario() -> None:
        writer = CaptureWriter(path, {"gemini": ["btcusd"]})
        task = asyncio.create_task(writer.run())
        assert writer.record_ws("gemini", _gemini_snapshot(), [], wall_ns=second, mono_ns=second)
        assert writer.record_ws(
            "gemini",
            '{"e":"depthUpdate","s":"BTCUSD","U":2,"u":2,"E":2000,"b":[["101","1"]],"a":[["103","1"]]}',
            [],
            wall_ns=3 * second,
            mono_ns=3 * second,
        )
        await writer.close()
        await task

    asyncio.run(scenario())
    seen: list[tuple[str, str, int]] = []
    report = asyncio.run(
        replay_file(
            path,
            max_age_seconds=1.0,
            book_observer=lambda exchange, pair, _wall_ns, mono_ns: seen.append(
                (exchange, pair, mono_ns)
            ),
        )
    )

    expiries = [event for event in report.lifecycle if event.kind == "book_expired"]
    assert expiries, "a one-second age limit must expire the book before the next update"
    assert ("gemini", "BTC-USD", expiries[0].mono_ns) in seen
    assert seen.index(("gemini", "BTC-USD", expiries[0].mono_ns)) == 1
