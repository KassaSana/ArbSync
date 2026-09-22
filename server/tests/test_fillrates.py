"""ARB-042: complete, windowed, and reproducible fill-rate statistics."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.fillrates import (
    INELIGIBLE_REASONS,
    MINUTE_NS,
    FillRateConfig,
    FillRateItem,
    FillRateMinute,
    FillRateSession,
    aggregate,
    window_bounds,
)
from arb.maintenance import prune_batch
from arb.orderbook import OrderBookManager
from arb.persistence import (
    CREATE_INDEX_SQL,
    CREATE_ROLLUP_SQL,
    CREATE_TABLE_SQL,
    SCHEMA_VERSION,
    OpportunityStore,
)
from arb.pricing import DepthSampler
from arb.types import EventKind, MarketEvent, PriceLevel
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

SECOND = 1_000_000_000
WALL = 1_700_000_020 * SECOND  # 40 s into a minute, so sessions straddle minutes


def book(
    exchange: str,
    asks: list[tuple[str, str]],
    bids: list[tuple[str, str]] | None = None,
    pair: str = "BTC-USD",
) -> MarketEvent:
    return MarketEvent(
        exchange=exchange,
        pair=pair,
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=1,
        bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in (bids or [("99", "100")])),
        asks=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in asks),
    )


def sampler_for(
    manager: OrderBookManager,
    *,
    roster: list[tuple[str, str]],
    notionals: tuple[str, ...] = ("100", "1000"),
    depth_levels: dict[str, int | None] | None = None,
    interval_seconds: float = 5.0,
    sink: Callable[[FillRateItem], object] | None = None,
    sleep: Callable[[float], object] | None = None,
) -> DepthSampler:
    kwargs: dict[str, object] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return DepthSampler(
        manager,
        [Decimal(value) for value in notionals],
        depth_levels if depth_levels is not None else {"gemini": None, "binance": 2},
        {},
        interval_seconds=interval_seconds,
        roster=roster,
        max_age_seconds=3.0,
        sink=sink,
        wall_clock=lambda: WALL,
        **kwargs,  # type: ignore[arg-type]
    )


def by_key(rows: list[dict[str, object]]) -> dict[tuple[object, ...], dict[str, object]]:
    return {(r["exchange"], r["pair"], r["notional"], r["side"]): r for r in rows}


def reasons(**counts: int) -> dict[str, int]:
    return {**dict.fromkeys(INELIGIBLE_REASONS, 0), **counts}


# --- Classification of every configured book ---


def test_every_configured_book_is_counted_with_its_reason_or_depth_outcome() -> None:
    now = [0]
    manager = OrderBookManager(max_age_seconds=3.0, clock=lambda: now[0])
    # Gemini streams a full book: 100 fills, 1000 does not (about 505 of asks).
    manager.apply(book("gemini", [("101", "5")]), received_monotonic_ns=0)
    # Binance is capped at 2 levels and holds exactly 2: its shortfall is at the cap.
    manager.apply(book("binance", [("101", "1"), ("102", "1")]), received_monotonic_ns=0)
    # Known but never (re)initialized, and a venue reported down with no book.
    manager.apply(book("gemini", [("101", "5")], pair="ETH-USD"), received_monotonic_ns=0)
    manager.invalidate("gemini", "ETH-USD")
    manager.set_exchange_connected("kraken", False)
    roster = [
        ("gemini", "BTC-USD"),
        ("binance", "BTC-USD"),
        ("coinbase", "BTC-USD"),  # configured, never seen
        ("gemini", "ETH-USD"),
        ("kraken", "BTC-USD"),
    ]
    sampler = sampler_for(manager, roster=roster)

    sampler.sample_all(0)
    now[0] = 4 * SECOND  # every received book is now older than max age
    sampler.sample_all()

    rows = by_key(sampler.fill_rates.rows())
    assert len(rows) == 5 * 2 * 2  # nothing configured is absent
    for row in rows.values():
        # The invariant: each sample lands in exactly one outcome.
        ineligible = row["ineligible"]
        assert isinstance(ineligible, dict)
        assert row["samples"] == 2
        assert row["samples"] == row["filled"] + row["insufficient_depth"] + sum(  # type: ignore[operator]
            ineligible.values()
        )

    small = rows[("gemini", "BTC-USD", "100", "buy")]
    assert (small["filled"], small["ineligible"]) == (1, reasons(too_old=1))
    assert small["fill_rate"] == 1.0 and small["eligible_share"] == 0.5
    full_depth = rows[("gemini", "BTC-USD", "1000", "buy")]
    assert (full_depth["insufficient_depth"], full_depth["insufficient_at_depth_cap"]) == (1, 0)
    capped = rows[("binance", "BTC-USD", "1000", "buy")]
    assert (capped["insufficient_depth"], capped["insufficient_at_depth_cap"]) == (1, 1)
    assert capped["subscribed_depth_levels"] == 2
    missing = rows[("coinbase", "BTC-USD", "100", "sell")]
    assert missing["ineligible"] == reasons(missing=2)
    assert missing["fill_rate"] is None and missing["eligible_share"] == 0.0
    assert rows[("gemini", "ETH-USD", "100", "buy")]["ineligible"] == reasons(uninitialized=2)
    assert rows[("kraken", "BTC-USD", "100", "buy")]["ineligible"] == reasons(disconnected=2)


def test_a_quiet_book_within_max_age_stays_eligible() -> None:
    now = [0]
    manager = OrderBookManager(max_age_seconds=3.0, clock=lambda: now[0])
    manager.apply(book("gemini", [("101", "5")]), received_monotonic_ns=0)
    sampler = sampler_for(manager, roster=[("gemini", "BTC-USD")], notionals=("100",))
    for instant in (1, 2, 3):  # no updates at all, but never older than 3 s
        now[0] = instant * SECOND
        sampler.sample_all()
    row = by_key(sampler.fill_rates.rows())[("gemini", "BTC-USD", "100", "buy")]
    assert (row["samples"], row["filled"], row["ineligible_samples"]) == (3, 3, 0)


def test_unknown_reason_is_rejected_rather_than_miscounted() -> None:
    from arb.fillrates import FillCounts

    with pytest.raises(ValueError, match="unknown ineligibility reason"):
        FillCounts().record_ineligible("resting")


# --- The sample grid, live and replay ---


def run_live(
    sampler: DepthSampler,
    now: list[int],
    wakes: list[int],
) -> None:
    """Drive `DepthSampler.run` on a fake clock; `wakes` are extra nanoseconds per sleep."""

    async def fake_sleep(seconds: float) -> None:
        if not wakes:
            raise asyncio.CancelledError
        now[0] += round(seconds * SECOND) + wakes.pop(0)

    sampler._sleep = fake_sleep

    async def main() -> None:
        with pytest.raises(asyncio.CancelledError):
            await sampler.run()

    asyncio.run(main())


def test_live_loop_counts_whole_missed_intervals_and_stamps_the_grid() -> None:
    now = [0]
    manager = OrderBookManager(max_age_seconds=3.0, clock=lambda: now[0])
    items: list[FillRateItem] = []
    sampler = sampler_for(
        manager, roster=[("gemini", "BTC-USD")], notionals=("100",), sink=items.append
    )
    # On time, then 2.5 intervals late, then 0.4 of an interval late (not a whole one).
    run_live(sampler, now, [0, int(12.5 * SECOND), 2 * SECOND])
    sampler.flush_fill_rates()

    assert sampler.samples == 3
    assert sampler.missed_samples == 2
    session = items[0]
    assert isinstance(session, FillRateSession) and session.started_wall_ns == WALL
    minutes = [item for item in items if isinstance(item, FillRateMinute)]
    # Ticks 1, 4 and 5 are due at +5 s, +20 s and +25 s: 45 s, 60 s and 65 s past
    # the minute WALL starts in. The missed ticks count where the gap ended.
    assert [(m.minute_ns - WALL + 40 * SECOND, m.samples, m.missed_samples) for m in minutes] == [
        (0, 1, 0),
        (MINUTE_NS, 2, 2),
    ]


def test_replay_and_live_agree_on_the_same_observation_sequence() -> None:
    """Scheduled replay sampling and the live loop produce identical buckets."""
    events: list[tuple[int, Callable[[OrderBookManager], object]]] = [
        (0, lambda m: m.apply(book("gemini", [("101", "5")]), received_monotonic_ns=0)),
        (
            4 * SECOND,
            lambda m: m.apply(
                book("binance", [("101", "1"), ("102", "1")]), received_monotonic_ns=4 * SECOND
            ),
        ),
        (6 * SECOND, lambda m: m.invalidate("binance", "BTC-USD")),
        (
            12 * SECOND,
            lambda m: m.apply(book("binance", [("101", "20")]), received_monotonic_ns=12 * SECOND),
        ),
        (
            20 * SECOND,
            lambda m: m.apply(book("gemini", [("101", "5")]), received_monotonic_ns=20 * SECOND),
        ),
    ]
    roster = [("gemini", "BTC-USD"), ("binance", "BTC-USD"), ("coinbase", "BTC-USD")]
    ticks = 16  # 80 s of 5 s samples: crosses two minute boundaries

    def fresh() -> tuple[list[int], OrderBookManager, list[FillRateItem], list[int]]:
        now = [0]
        return now, OrderBookManager(max_age_seconds=3.0, clock=lambda: now[0]), [], [0]

    def apply_due(manager: OrderBookManager, applied: list[int], until_ns: int) -> None:
        while applied[0] < len(events) and events[applied[0]][0] <= until_ns:
            events[applied[0]][1](manager)
            applied[0] += 1

    # Replay: events at an instant land before that instant's sample.
    now, manager, replayed, applied = fresh()
    sampler = sampler_for(manager, roster=roster, sink=replayed.append)
    sampler.start_session(0, WALL)
    for tick in range(1, ticks + 1):
        instant = tick * 5 * SECOND
        now[0] = instant
        apply_due(manager, applied, instant)
        sampler.sample_all(instant, tick=sampler.tick_at(instant))
    sampler.flush_fill_rates()

    # Live: the loop sleeps to each due instant; the fake sleep delivers events.
    now, manager, live, applied = fresh()
    apply_due(manager, applied, 0)
    remaining = [ticks]

    async def sleep_until_due(seconds: float) -> None:
        if remaining[0] == 0:
            raise asyncio.CancelledError
        remaining[0] -= 1
        now[0] += round(seconds * SECOND)
        apply_due(manager, applied, now[0])

    live_sampler = sampler_for(manager, roster=roster, sink=live.append)
    live_sampler._sleep = sleep_until_due

    async def main() -> None:
        with pytest.raises(asyncio.CancelledError):
            await live_sampler.run()

    asyncio.run(main())
    live_sampler.flush_fill_rates()

    assert live == replayed
    assert len([item for item in live if isinstance(item, FillRateMinute)]) == 3
    assert live_sampler.fill_rates.rows() == sampler.fill_rates.rows()
    binance = by_key(sampler.fill_rates.rows())[("binance", "BTC-USD", "1000", "buy")]
    # t=5 capped shortfall; t=10 invalidated; t=15 refilled 3 s earlier, still
    # fresh; t=20..80 too old after that last receipt.
    assert (binance["insufficient_at_depth_cap"], binance["filled"]) == (1, 1)
    assert binance["ineligible"] == reasons(uninitialized=1, too_old=13)


# --- Windows, restarts, configuration changes, storage ---


def session_items(
    anchor_wall_ns: int,
    samples: int,
    notionals: tuple[str, ...] = ("100",),
    roster: tuple[tuple[str, str], ...] = (("gemini", "BTC-USD"), ("coinbase", "BTC-USD")),
) -> list[FillRateItem]:
    manager = OrderBookManager(max_age_seconds=3.0, clock=lambda: 0)
    if ("gemini", "BTC-USD") in roster:
        manager.apply(book("gemini", [("101", "5")]), received_monotonic_ns=0)
    items: list[FillRateItem] = []
    sampler = sampler_for(
        manager,
        roster=list(roster),
        notionals=notionals,
        interval_seconds=10.0,
        sink=items.append,
    )
    sampler.start_session(0, anchor_wall_ns)
    for tick in range(1, samples + 1):
        sampler.sample_all(0, tick=tick)
    sampler.flush_fill_rates()
    return items


async def persist(path: Path, items: list[FillRateItem]) -> OpportunityStore:
    store = OpportunityStore(str(path), batch_size=3, flush_interval_seconds=0.01)
    await store.initialize()
    worker = asyncio.create_task(store.run())
    for item in items:
        assert store.offer_fill_rates(item)
    await store.close()
    await worker
    return store


@pytest.mark.asyncio
async def test_window_across_a_restart_and_a_config_change_matches_the_reference(
    tmp_path: Path,
) -> None:
    minute = 28_333_334 * MINUTE_NS  # a whole minute
    first = session_items(minute, 12)  # samples at +10 s .. +120 s: minutes 0-2
    # Restarted five minutes later with an extra notional: a different config.
    second = session_items(minute + 5 * MINUTE_NS, 6, notionals=("100", "5000"))
    store = await persist(tmp_path / "fills.sqlite3", first + second)

    start, end = minute, minute + 7 * MINUTE_NS
    window = await store.fill_rate_window(start, end)
    reference = aggregate(first + second, start, end)
    assert window.payload() == reference.payload()

    body = window.payload()
    assert body["samples"] == 18 and body["missed_samples"] == 0
    # 18 samples x 10 s over a 7-minute window; the restart gap is not interpolated.
    assert body["coverage"] == pytest.approx(180 / 420)
    sessions = body["sessions"]
    assert isinstance(sessions, list) and [s["samples"] for s in sessions] == [12, 6]
    configurations = body["configurations"]
    assert isinstance(configurations, list) and len(configurations) == 2
    notional_sets = sorted(
        tuple(sorted({row["notional"] for row in group["rows"]})) for group in configurations
    )
    assert notional_sets == [("100",), ("100", "5000")]  # never merged across configs

    # A window must fall on whole minutes: the partial edges are excluded.
    trimmed = await store.fill_rate_window(minute + 30 * SECOND, minute + 2 * MINUTE_NS)
    assert trimmed.effective_from_ns == minute + MINUTE_NS
    assert trimmed.payload()["samples"] == 6
    assert window_bounds(minute + 1, minute + MINUTE_NS - 1) == (minute + MINUTE_NS,) * 2

    only_coinbase = await store.fill_rate_window(start, end, exchange="coinbase")
    rows = [row for group in only_coinbase.payload()["configurations"] for row in group["rows"]]  # type: ignore[attr-defined]
    assert {row["exchange"] for row in rows} == {"coinbase"}
    assert all(row["ineligible"]["missing"] == row["samples"] for row in rows)


@pytest.mark.asyncio
async def test_schema_4_database_upgrades_in_place_and_keeps_episodes(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(CREATE_TABLE_SQL + CREATE_INDEX_SQL + CREATE_ROLLUP_SQL)
        db.execute(
            "INSERT INTO opportunity_episodes (start_ns, end_ns, pair, quote_asset, "
            "buy_exchange, sell_exchange, buy_price, sell_price, spread_pct, max_size, "
            "theoretical_profit, peak_spread_pct, peak_size, peak_profit, pricing_ledgers, "
            "close_spread_pct, close_reason) VALUES "
            "(1, 2, 'BTC-USD', 'USD', 'gemini', 'coinbase', '1', '2', '1', '1', '1', '1', "
            "'1', '1', '[]', '0', 'spread_closed')"
        )
        db.execute("PRAGMA user_version = 4")
    await persist(path, session_items(60 * MINUTE_NS, 3))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        assert db.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0] == 1
        assert db.execute("SELECT SUM(samples) FROM fill_rate_ticks").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_pruning_removes_only_whole_minutes_before_the_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "prune.sqlite3"
    minute = 50 * MINUTE_NS
    await persist(path, session_items(minute, 17))  # buckets at minutes 0, 1, 2
    deleted = prune_batch(path, minute + 2 * MINUTE_NS + 30 * SECOND)
    assert deleted == 2 * 2 * 1 * 2  # two minutes x two books x one notional x two sides
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT minute_ns FROM fill_rate_ticks").fetchall() == [
            (minute + 2 * MINUTE_NS,)
        ]
        assert db.execute("SELECT COUNT(*) FROM fill_rate_sessions").fetchone()[0] == 1
    prune_batch(path, minute + 10 * MINUTE_NS)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM fill_rate_sessions").fetchone()[0] == 0


def test_a_full_queue_drops_buckets_without_blocking(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "full.sqlite3"), queue_maxsize=1)
    before = REGISTRY.get_sample_value(
        "arb_persistence_queue_drops_total", {"reason": "queue_full"}
    )
    items = session_items(MINUTE_NS, 1)
    assert store.offer_fill_rates(items[0]) is True
    assert store.offer_fill_rates(items[1]) is False
    after = REGISTRY.get_sample_value("arb_persistence_queue_drops_total", {"reason": "queue_full"})
    assert (after or 0) - (before or 0) == 1


def test_config_fingerprint_round_trips_and_tracks_every_input() -> None:
    config = FillRateConfig.build(
        [("gemini", "BTC-USD")], [Decimal("100")], 5.0, {"gemini": None}, 60.0
    )
    assert FillRateConfig.from_payload(config.payload()).fingerprint() == config.fingerprint()
    changed = [
        FillRateConfig.build([], [Decimal("100")], 5.0, {"gemini": None}, 60.0),
        FillRateConfig.build([("gemini", "BTC-USD")], [Decimal("1")], 5.0, {"gemini": None}, 60.0),
        FillRateConfig.build(
            [("gemini", "BTC-USD")], [Decimal("100")], 1.0, {"gemini": None}, 60.0
        ),
        FillRateConfig.build([("gemini", "BTC-USD")], [Decimal("100")], 5.0, {"gemini": 5}, 60.0),
        FillRateConfig.build([("gemini", "BTC-USD")], [Decimal("100")], 5.0, {"gemini": None}, 1.0),
    ]
    assert len({config.fingerprint(), *(other.fingerprint() for other in changed)}) == 6


# --- API ---


@pytest.mark.asyncio
async def test_fill_rate_endpoint_serves_the_session_and_persisted_windows(
    tmp_path: Path,
) -> None:
    minute = 100 * MINUTE_NS
    path = tmp_path / "api.sqlite3"
    store = await persist(path, session_items(minute, 12))
    manager = OrderBookManager(max_age_seconds=3.0, clock=lambda: 0)
    sampler = sampler_for(manager, roster=[("coinbase", "BTC-USD")], notionals=("100",))
    sampler.sample_all(0)
    client = TestClient(create_app(store, manager, LiveBroadcaster(), depth_sampler=sampler))

    current = client.get("/api/pricing/fill-rates").json()
    assert current["samples"] == 1 and current["missed_samples"] == 0
    assert current["session"]["config_fingerprint"] == sampler.fill_config.fingerprint()
    assert current["config"]["roster"] == [["coinbase", "BTC-USD"]]
    assert [row["ineligible"]["missing"] for row in current["rows"]] == [1, 1]

    windowed = client.get(
        "/api/pricing/fill-rates",
        params={"from_ns": str(minute), "to_ns": str(minute + 3 * MINUTE_NS)},
    ).json()
    assert windowed["samples"] == 12 and windowed["effective_to_ns"] == str(minute + 3 * MINUTE_NS)
    assert len(windowed["configurations"]) == 1
    only_gemini = client.get(
        "/api/pricing/fill-rates",
        params={"from_ns": str(minute), "to_ns": str(minute + 3 * MINUTE_NS), "exchange": "gemini"},
    ).json()
    assert {row["exchange"] for row in only_gemini["configurations"][0]["rows"]} == {"gemini"}

    for params in (
        {"from_ns": str(minute)},
        {"from_ns": str(minute), "to_ns": str(minute)},
        {"from_ns": "99999999999999999999", "to_ns": "1"},
        {"from_ns": str(2**63), "to_ns": str(2**63 + 1)},
    ):
        assert client.get("/api/pricing/fill-rates", params=params).status_code == 422


@pytest.mark.asyncio
async def test_filtered_windows_count_only_sessions_behind_the_returned_rows(
    tmp_path: Path,
) -> None:
    minute = 200 * MINUTE_NS
    gemini_only = session_items(minute, 6, roster=(("gemini", "BTC-USD"),))
    coinbase_only = session_items(minute + 2 * MINUTE_NS, 3, roster=(("coinbase", "BTC-USD"),))
    items = gemini_only + coinbase_only
    store = await persist(tmp_path / "filtered.sqlite3", items)
    start, end = minute, minute + 5 * MINUTE_NS
    filters: list[tuple[str | None, str | None]] = [
        (None, None),
        ("coinbase", None),
        ("gemini", None),
        (None, "ETH-USD"),
    ]
    for exchange, pair in filters:
        stored = await store.fill_rate_window(start, end, exchange=exchange, pair=pair)
        reference = aggregate(items, start, end, exchange=exchange, pair=pair)
        assert stored.payload() == reference.payload(), (exchange, pair)

    body = (await store.fill_rate_window(start, end, exchange="coinbase")).payload()
    assert body["samples"] == 3
    assert [session["samples"] for session in body["sessions"]] == [3]  # type: ignore[attr-defined]
    assert body["coverage"] == pytest.approx(30 / 300)
    empty = (await store.fill_rate_window(start, end, pair="ETH-USD")).payload()
    assert (empty["samples"], empty["sessions"], empty["configurations"]) == (0, [], [])


@pytest.mark.asyncio
async def test_a_bucket_restores_a_session_row_pruned_before_its_first_minute(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphan.sqlite3"
    minute = 300 * MINUTE_NS
    items = session_items(minute, 12)
    session, buckets = items[0], items[1:]
    await persist(path, [session])
    # Pruned while the session had flushed nothing yet: its row goes.
    prune_batch(path, minute + 30 * SECOND)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM fill_rate_sessions").fetchone()[0] == 0
    store = await persist(path, buckets)
    window = await store.fill_rate_window(minute, minute + 3 * MINUTE_NS)
    assert window.payload()["samples"] == 12


def test_interval_is_rounded_to_nanoseconds_and_must_be_positive() -> None:
    def config(interval: float) -> FillRateConfig:
        return FillRateConfig.build([], [Decimal("1")], interval, {}, None)

    assert config(1.001).interval_ns == 1_001_000_000
    assert config(0.1).interval_ns == 100_000_000
    for interval in (0.0, 1e-12):
        with pytest.raises(ValueError, match="must be positive"):
            config(interval)
