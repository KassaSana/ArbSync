"""Golden wire fixtures shared with the dashboard's decoders.

The backend emits JSON through `as_payload()` and the API routes; the
dashboard validates it in `dashboard/src/api/schema.ts`. Neither side can see
the other, so a renamed key used to pass both CI suites and fail only in the
browser. These tests write what the backend actually sends to
`fixtures/wire/*.json`, and `dashboard/src/api/schema.test.ts` runs those same
files through every decoder. A change on either side now fails one of the two.

Regenerate after an intentional wire change:

    uv run pytest server/tests/test_wire_fixtures.py --update-wire-fixtures

then re-run the dashboard tests to confirm the decoders still accept it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from arb.adapters.base import ExchangeAdapter
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.pricing import DepthSampler
from arb.types import (
    EventKind,
    LiveMessage,
    MarketEvent,
    OpportunityEpisode,
    PriceLevel,
    PricingLedger,
)
from episodes import close_episode, make_episode
from fastapi.testclient import TestClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wire"

# 2024-07-03T10:26:40Z; every wall-clock read during fixture generation returns this.
NOW_NS = 1_720_000_000_000_000_000
SECOND_NS = 1_000_000_000
# Monotonic clock of the book manager; ages on the wire derive from it.
MONO_NS = 5_000 * SECOND_NS


class StubAdapter(ExchangeAdapter):
    name = "gemini"
    ws_url = "wss://example.test"
    snapshot_url = "https://example.test/snapshot"

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return symbol.upper()

    async def subscribe(self, websocket: Any) -> None:
        return None

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return []

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        raise AssertionError("not fetched here")


def snapshot(exchange: str, pair: str, bid: str, ask: str, size: str = "20") -> MarketEvent:
    return MarketEvent(
        exchange,
        pair,
        EventKind.SNAPSHOT,
        sequence=7,
        timestamp_ns=NOW_NS - 2 * SECOND_NS,
        bids=(PriceLevel(Decimal(bid), Decimal(size)),),
        asks=(PriceLevel(Decimal(ask), Decimal(size)),),
    )


def ledger() -> PricingLedger:
    return PricingLedger(
        notional=Decimal("1000"),
        top_of_book_spread_pct=Decimal("2.00"),
        buy_vwap=Decimal("100"),
        sell_vwap=Decimal("102"),
        gross_executable_spread_pct=Decimal("2.00"),
        depth_impact_pct=Decimal("0.00"),
        buy_taker_fee_pct=Decimal("0.4"),
        sell_taker_fee_pct=Decimal("0.6"),
        fee_impact_pct=Decimal("-1.006"),
        net_executable_spread_pct=Decimal("0.994"),
        insufficient_depth=False,
    )


def episodes() -> tuple[OpportunityEpisode, OpportunityEpisode, OpportunityEpisode]:
    """An orphaned, a closed, and an open episode, oldest first."""
    orphaned = make_episode(start_ns=NOW_NS - 600 * SECOND_NS, pricing_ledgers=(ledger(),))
    closed_open = make_episode(
        start_ns=NOW_NS - 300 * SECOND_NS,
        pair="ETH-USD",
        buy_exchange="coinbase",
        sell_exchange="gemini",
        buy_price=Decimal("3000.5"),
        sell_price=Decimal("3010.25"),
        spread_pct=Decimal("0.3249"),
        max_size=Decimal("0.25"),
        theoretical_profit=Decimal("2.4375"),
    )
    closed = close_episode(
        closed_open,
        duration_ns=45 * SECOND_NS,
        peak_spread_pct=Decimal("0.41"),
        peak_size=Decimal("0.3"),
        peak_profit=Decimal("3.69"),
        close_spread_pct=Decimal("-0.02"),
    )
    still_open = make_episode(start_ns=NOW_NS - 30 * SECOND_NS, pricing_ledgers=(ledger(),))
    return orphaned, closed, still_open


async def seeded_store(path: Path) -> OpportunityStore:
    orphaned, closed, still_open = episodes()
    first_run = OpportunityStore(str(path))
    await first_run.initialize()
    await first_run._flush([orphaned])
    await first_run._close_db()
    # A restart marks the episode left open by the first run as orphaned.
    store = OpportunityStore(str(path))
    await store.initialize()
    await store._flush([closed, still_open])
    return store


def build_app(store: OpportunityStore) -> tuple[TestClient, OrderBookManager]:
    manager = OrderBookManager(max_age_seconds=30.0, clock=lambda: MONO_NS)
    manager.apply(
        snapshot("gemini", "BTC-USD", "99", "100"), received_monotonic_ns=MONO_NS - 250_000_000
    )
    manager.apply(
        snapshot("coinbase", "BTC-USD", "102", "103"), received_monotonic_ns=MONO_NS - 40_000_000
    )
    manager.apply(
        snapshot("gemini", "ETH-USD", "3000", "3001", size="0.5"), received_monotonic_ns=MONO_NS
    )
    sampler = DepthSampler(
        manager,
        [Decimal("1000"), Decimal("250000")],
        {"gemini": None, "coinbase": 50},
        {"gemini": Decimal("0.4"), "coinbase": Decimal("0.6")},
        interval_seconds=5.0,
    )
    sampler.sample_all()
    adapter = StubAdapter(["btcusd", "ethusd"])
    adapter.connected = True
    adapter.last_message_ns = NOW_NS - 3 * SECOND_NS
    adapter.gap_count = 2
    adapter.reconnect_count = 1
    adapter.last_error = "connection reset"
    broadcaster = LiveBroadcaster()
    app = create_app(
        store,
        manager,
        broadcaster,
        adapters=[adapter],
        depth_sampler=sampler,
        expected_pairs=[
            ("gemini", "BTC-USD"),
            ("gemini", "ETH-USD"),
            ("coinbase", "BTC-USD"),
            ("coinbase", "ETH-USD"),
        ],
        started_at_ns=NOW_NS - 90 * SECOND_NS,
    )
    return TestClient(app), manager


def live_frames(manager: OrderBookManager) -> dict[str, object]:
    """One envelope of each live message type, as a connected client receives them.

    Runs on its own event loop with its own broadcaster; the one serving the
    `TestClient` belongs to that client's portal loop.
    """

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            return None

    async def collect() -> list[dict[str, Any]]:
        broadcaster = LiveBroadcaster()
        socket = Socket()
        await broadcaster.connect(socket)
        top = manager.top_of_book("gemini", "BTC-USD")
        assert top is not None
        await broadcaster.broadcast(LiveMessage("top_of_book", top.as_payload()))
        status = manager.eligibility("coinbase", "ETH-USD", MONO_NS)
        await broadcaster.broadcast(LiveMessage("book_status", status.as_payload()))
        _, closed, still_open = episodes()
        await broadcaster.broadcast(LiveMessage("opportunity", still_open.as_payload()))
        await broadcaster.broadcast(LiveMessage("opportunity", closed.as_payload()))
        await asyncio.sleep(0)
        await broadcaster.disconnect(socket)
        return socket.sent

    frames = asyncio.run(collect())
    return {
        frame["type"] + ("_close" if frame["payload"].get("end_ns") else ""): frame
        for frame in frames
    }


def generate(tmp_path: Path) -> dict[str, object]:
    store = asyncio.run(seeded_store(tmp_path / "wire.sqlite3"))
    client, manager = build_app(store)
    try:
        with client, client.websocket_connect("/ws/live") as ws:
            state_snapshot = ws.receive_json()
            fixtures = routes(client, state_snapshot)
    finally:
        # `_flush` opened the write connection on the seeding loop; its
        # aiosqlite thread is not a daemon and would keep the process alive.
        asyncio.run(store._close_db())
    for name, frame in live_frames(manager).items():
        fixtures[f"live_{name}"] = frame
    return fixtures


def routes(client: TestClient, state_snapshot: dict[str, Any]) -> dict[str, object]:
    return {
        "opportunities_recent": client.get("/api/opportunities/recent?limit=50").json(),
        # Two of three episodes, so the fixture carries a real next-page cursor.
        "opportunity_history": client.get("/api/opportunities?limit=2").json(),
        "stats": client.get("/api/stats?window=1h").json(),
        "pairs": client.get("/api/pairs").json(),
        "adapters": client.get("/api/adapters").json(),
        "book_status": client.get("/api/book-status").json(),
        "pricing_depth": client.get("/api/pricing/depth").json(),
        "system_overview": client.get("/api/system/overview").json(),
        "system_stats": client.get("/api/system/stats?window=1h").json(),
        "system_timeseries": client.get(
            "/api/system/timeseries?window=1h&bucket_seconds=60"
        ).json(),
        "live_state_snapshot": state_snapshot,
    }


def dumps(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


@pytest.fixture
def frozen_wall_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(time, "time_ns", lambda: NOW_NS)
    yield


def test_wire_fixtures_match_what_the_backend_emits(
    tmp_path: Path, frozen_wall_clock: None, request: pytest.FixtureRequest
) -> None:
    fixtures = generate(tmp_path)
    update = bool(request.config.getoption("--update-wire-fixtures"))
    if update:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in FIXTURE_DIR.glob("*.json"):
            stale.unlink()
    mismatches: list[str] = []
    for name, value in fixtures.items():
        path = FIXTURE_DIR / f"{name}.json"
        rendered = dumps(value)
        if update:
            path.write_bytes(rendered.encode())
            continue
        if not path.exists():
            mismatches.append(f"{path.name}: missing")
        elif path.read_bytes().decode() != rendered:
            mismatches.append(f"{path.name}: differs from what the backend emits")
    extra = sorted(p.name for p in FIXTURE_DIR.glob("*.json")) if not update else []
    expected_names = sorted(f"{name}.json" for name in fixtures)
    if extra and extra != expected_names:
        mismatches.append(f"fixture set differs: {sorted(set(extra) ^ set(expected_names))}")
    assert not mismatches, (
        "wire fixtures are out of date; if the change is intentional run\n"
        "  uv run pytest server/tests/test_wire_fixtures.py --update-wire-fixtures\n"
        "and re-run the dashboard tests:\n  " + "\n  ".join(mismatches)
    )


def test_fixtures_cover_every_close_reason_the_dashboard_distinguishes(
    tmp_path: Path, frozen_wall_clock: None
) -> None:
    recent = generate(tmp_path)["opportunities_recent"]
    assert isinstance(recent, list)
    assert {row["close_reason"] for row in recent} == {None, "spread_closed", "orphaned"}
    ledgers = [row["pricing_ledgers"] for row in recent]
    assert any(ledgers) and any(not entries for entries in ledgers)
