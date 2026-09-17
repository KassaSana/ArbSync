import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from arb.adapters.base import ExchangeAdapter
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.orderbook import OrderBookManager
from arb.persistence import OpportunityStore
from arb.types import EventKind, MarketEvent, OpportunityEpisode, PriceLevel
from episodes import close_episode, make_episode
from fastapi.testclient import TestClient


class StubAdapter(ExchangeAdapter):
    name = "stub"
    ws_url = "wss://example.test"
    snapshot_url = "https://example.test/snapshot"

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return symbol

    async def subscribe(self, websocket: Any) -> None:
        return None

    async def parse_message(self, message: str) -> list[MarketEvent]:
        return []

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=trigger_sequence,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        )


@pytest.mark.asyncio
async def test_recent_endpoint_returns_saved_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    store = OpportunityStore(str(db_path), batch_size=1, flush_interval_seconds=0.01)
    await store.initialize()
    await store.enqueue(
        make_episode(
            start_ns=1,
            pair="BTC-USD",
            quote_asset="USD",
            buy_exchange="gemini",
            sell_exchange="coinbase",
            buy_price=Decimal("100"),
            sell_price=Decimal("101"),
            spread_pct=Decimal("1"),
            max_size=Decimal("0.5"),
            theoretical_profit=Decimal("0.5"),
        )
    )
    task = asyncio.create_task(store.run())
    await store.close()
    await asyncio.wait_for(task, timeout=1.0)

    client = TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))
    response = client.get("/api/opportunities/recent?limit=10")
    assert response.status_code == 200
    assert response.json()[0]["pair"] == "BTC-USD"
    assert response.json()[0]["start_ns"] == "1"
    assert response.json()[0]["end_ns"] is None
    assert response.json()[0]["quote_asset"] == "USD"
    assert response.json()[0]["theoretical_profit"] == "0.5"


@pytest.mark.asyncio
async def test_recent_endpoint_accepts_orphaned_crash_recovered_episode(tmp_path: Path) -> None:
    path = str(tmp_path / "orphans.sqlite3")
    store = OpportunityStore(path, batch_size=10, flush_interval_seconds=0.05)
    await store.initialize()
    await store._flush([make_episode(start_ns=1)])
    await store._close_db()

    restarted = OpportunityStore(path)
    await restarted.initialize()
    client = TestClient(create_app(restarted, OrderBookManager(), LiveBroadcaster()))

    recent = client.get("/api/opportunities/recent?limit=10").json()
    assert len(recent) == 1
    row = recent[0]
    assert row["close_reason"] == "orphaned"
    assert row["end_ns"] is None
    assert row["duration_ns"] is None
    assert row["close_spread_pct"] is None

    overview = client.get("/api/system/overview").json()
    assert overview["open_count"] == 0
    assert overview["all_time_lifetime"] is None


def test_test_client_prefers_httpx2() -> None:
    """The httpx2 development dependency is required, and only implicitly.

    `starlette.testclient` does `import httpx2 as httpx` and falls back to
    httpx with a deprecation warning. Nothing here imports httpx2 by name, so
    an audit that only greps for imports concludes it is unused and removes
    it. This test fails the moment that happens.
    """
    import starlette.testclient

    assert starlette.testclient.httpx.__name__ == "httpx2"


def test_root_describes_the_service() -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )
    response = client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["health"] == "/healthz"
    assert payload["live_updates"] == "/ws/live"


def test_healthz_is_alive() -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_allows_only_configured_origin() -> None:
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            OrderBookManager(),
            LiveBroadcaster(),
            cors_allowed_origins=["https://dashboard.example.test"],
        )
    )

    allowed = client.options(
        "/healthz",
        headers={
            "Origin": "https://dashboard.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    denied = client.options(
        "/healthz",
        headers={
            "Origin": "https://untrusted.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://dashboard.example.test"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize("limit", [0, 501])
def test_recent_endpoint_rejects_out_of_range_limits(limit: int) -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )

    assert client.get(f"/api/opportunities/recent?limit={limit}").status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/stats?window=forever",
        "/api/system/stats?window=forever",
        "/api/system/timeseries?window=forever",
        "/api/system/timeseries?bucket_seconds=0",
        "/api/system/timeseries?bucket_seconds=86401",
    ],
)
def test_statistics_endpoints_reject_invalid_query_values(path: str) -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )

    assert client.get(path).status_code == 422


def test_metrics_endpoint_exposes_prometheus_payload() -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "arb_ws_clients" in response.text
    assert "arb_ws_sender_failures_total" in response.text


def test_adapter_status_returns_runtime_fields() -> None:
    adapter = StubAdapter(["BTC-USD"])
    adapter.connected = True
    adapter.last_message_ns = time.time_ns()
    adapter.reconnect_count = 2
    adapter.last_error = "socket reset"

    client = TestClient(
        create_app(
            OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster(), adapters=[adapter]
        )
    )
    response = client.get("/api/adapters")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["exchange"] == "stub"
    assert body[0]["connected"] is True
    assert isinstance(body[0]["last_message_age_ms"], int)
    assert body[0]["last_message_age_ms"] >= 0
    assert body[0]["gap_count"] == 0
    assert body[0]["reconnect_count"] == 2
    assert body[0]["last_error"] == "socket reset"


def test_readyz_returns_not_ready_for_missing_updates() -> None:
    adapter = StubAdapter(["BTC-USD"])
    adapter.connected = True
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            OrderBookManager(),
            LiveBroadcaster(),
            adapters=[adapter],
            expected_pairs=[("stub", "BTC-USD")],
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["disconnected_adapters"] == []
    assert body["stale_pairs"] == [
        {
            "exchange": "stub",
            "pair": "BTC-USD",
            "initialized": False,
            "continuous": False,
            "connected": True,
            "age_ms": None,
            "max_age_ms": 30_000,
            "eligible": False,
            "reason": "missing",
        }
    ]


def test_readyz_returns_ready_for_fresh_books() -> None:
    adapter = StubAdapter(["BTC-USD"])
    adapter.connected = True
    manager = OrderBookManager()
    manager.apply(
        MarketEvent(
            exchange="stub",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=1,
            timestamp_ns=time.time_ns(),
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        )
    )

    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            manager,
            LiveBroadcaster(),
            adapters=[adapter],
            expected_pairs=[("stub", "BTC-USD")],
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "disconnected_adapters": [],
        "stale_pairs": [],
        "background_task_failures": [],
    }


def test_readyz_reports_background_task_failures() -> None:
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            OrderBookManager(),
            LiveBroadcaster(),
            background_failures=lambda: [
                {"task": "adapter:gemini", "error": "RuntimeError('boom')"}
            ],
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["background_task_failures"] == [
        {"task": "adapter:gemini", "error": "RuntimeError('boom')"}
    ]


def test_book_status_uses_canonical_eligibility_payload() -> None:
    manager = OrderBookManager(max_age_seconds=12.5, clock=lambda: 1_000)
    manager.apply(
        MarketEvent(
            exchange="stub",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=1,
            timestamp_ns=1,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        ),
        received_monotonic_ns=1_000,
    )
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            manager,
            LiveBroadcaster(),
            expected_pairs=[("stub", "BTC-USD")],
        )
    )

    assert client.get("/api/book-status").json() == [
        {
            "exchange": "stub",
            "pair": "BTC-USD",
            "initialized": True,
            "continuous": True,
            "connected": True,
            "age_ms": 0,
            "max_age_ms": 12_500,
            "eligible": True,
            "reason": None,
        }
    ]


def test_window_to_ns_known_and_unknown_values() -> None:
    from arb.api import window_to_ns

    assert window_to_ns("1h") == 3_600_000_000_000
    assert window_to_ns("4h") == 14_400_000_000_000
    assert window_to_ns("24h") == 86_400_000_000_000
    assert window_to_ns("1d") == 86_400_000_000_000
    assert window_to_ns("72h") == 259_200_000_000_000
    assert window_to_ns("1w") == 604_800_000_000_000
    # Unknown windows must default to 1h, never raise.
    assert window_to_ns("nonsense") == 3_600_000_000_000


def test_pairs_endpoint_lists_tracked_pairs_before_any_market_event() -> None:
    """Cold start: the roster is configuration, not a consequence of traffic."""
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            OrderBookManager(),
            LiveBroadcaster(),
            expected_pairs=[("gemini", "BTC-USD"), ("coinbase", "BTC-USD")],
        )
    )

    response = client.get("/api/pairs")

    assert response.status_code == 200
    assert response.json() == [
        {"exchange": "coinbase", "pair": "BTC-USD"},
        {"exchange": "gemini", "pair": "BTC-USD"},
    ]


def test_pairs_endpoint_is_stable_when_one_exchange_initializes() -> None:
    """Partial initialization must not add, drop, or reorder entries."""
    manager = OrderBookManager()
    expected = [("gemini", "BTC-USD"), ("coinbase", "BTC-USD"), ("binance", "BTC-USDT")]
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"), manager, LiveBroadcaster(), expected_pairs=expected
        )
    )
    cold = client.get("/api/pairs").json()

    manager.apply(
        MarketEvent(
            exchange="gemini",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=1,
            timestamp_ns=time.time_ns(),
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        )
    )

    assert client.get("/api/pairs").json() == cold
    assert cold == [
        {"exchange": "binance", "pair": "BTC-USDT"},
        {"exchange": "coinbase", "pair": "BTC-USD"},
        {"exchange": "gemini", "pair": "BTC-USD"},
    ]


def test_pairs_endpoint_reports_an_unconfigured_book_once() -> None:
    """A symbol an exchange sends but nobody configured stays visible, not doubled."""
    manager = OrderBookManager()
    for exchange in ("gemini", "kraken"):
        manager.apply(
            MarketEvent(
                exchange=exchange,
                pair="BTC-USD",
                kind=EventKind.SNAPSHOT,
                sequence=1,
                timestamp_ns=time.time_ns(),
                bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
                asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
            )
        )
    client = TestClient(
        create_app(
            OpportunityStore(":memory:"),
            manager,
            LiveBroadcaster(),
            expected_pairs=[("gemini", "BTC-USD")],
        )
    )

    assert client.get("/api/pairs").json() == [
        {"exchange": "gemini", "pair": "BTC-USD"},
        {"exchange": "kraken", "pair": "BTC-USD"},
    ]


def test_pairs_endpoint_lists_known_books() -> None:
    manager = OrderBookManager()
    manager.apply(
        MarketEvent(
            exchange="gemini",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=1,
            timestamp_ns=time.time_ns(),
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("1")),),
        )
    )
    client = TestClient(create_app(OpportunityStore(":memory:"), manager, LiveBroadcaster()))
    response = client.get("/api/pairs")
    assert response.status_code == 200
    assert response.json() == [{"exchange": "gemini", "pair": "BTC-USD"}]


@pytest.mark.asyncio
async def test_stats_endpoint_returns_aggregates(tmp_path: Path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    store = OpportunityStore(str(db_path), batch_size=1, flush_interval_seconds=0.01)
    await store.initialize()
    now_ns = time.time_ns()
    await store.enqueue(
        make_episode(
            start_ns=now_ns,
            pair="BTC-USD",
            quote_asset="USD",
            buy_exchange="gemini",
            sell_exchange="coinbase",
            buy_price=Decimal("100"),
            sell_price=Decimal("103"),
            spread_pct=Decimal("3"),
            max_size=Decimal("1"),
            theoretical_profit=Decimal("3"),
        )
    )
    runner = asyncio.create_task(store.run())
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    client = TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))
    response = client.get("/api/stats?window=1h")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert Decimal(body["max_spread_pct"]) == Decimal("3")
    assert body["theoretical_profit_by_quote"] == {"USD": "3.0"}


def test_websocket_route_accepts_connection() -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )
    with client.websocket_connect("/ws/live") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "state_snapshot"
        assert snapshot["payload"] == {"books": [], "statuses": []}
        assert snapshot["stream_sequence"] == 1
        ws.send_text("ping")


def test_websocket_connection_restores_only_current_eligible_books() -> None:
    manager = OrderBookManager(clock=lambda: 1_000)
    manager.apply(
        MarketEvent(
            exchange="stub",
            pair="BTC-USD",
            kind=EventKind.SNAPSHOT,
            sequence=4,
            timestamp_ns=99,
            bids=(PriceLevel(price=Decimal("100"), size=Decimal("1")),),
            asks=(PriceLevel(price=Decimal("101"), size=Decimal("2")),),
        ),
        received_monotonic_ns=1_000,
    )
    app = create_app(
        OpportunityStore(":memory:"),
        manager,
        LiveBroadcaster(),
        expected_pairs=[("stub", "BTC-USD"), ("stub", "ETH-USD")],
    )

    with TestClient(app).websocket_connect("/ws/live") as ws:
        snapshot = ws.receive_json()

    assert snapshot["type"] == "state_snapshot"
    assert snapshot["payload"]["books"] == [
        {
            "exchange": "stub",
            "pair": "BTC-USD",
            "best_bid_price": "100",
            "best_bid_size": "1",
            "best_ask_price": "101",
            "best_ask_size": "2",
            "sequence": 4,
            "timestamp_ns": "99",
        }
    ]
    assert [status["eligible"] for status in snapshot["payload"]["statuses"]] == [True, False]


def _seed_opp(store: OpportunityStore, **kwargs: Any) -> OpportunityEpisode:
    kwargs.setdefault("start_ns", time.time_ns())
    return make_episode(**kwargs)


@pytest.mark.asyncio
async def test_system_overview_reports_uptime_and_started_at(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "ov.sqlite3"))
    await store.initialize()
    started_at_ns = time.time_ns() - 5_000_000_000  # started 5s ago
    client = TestClient(
        create_app(store, OrderBookManager(), LiveBroadcaster(), started_at_ns=started_at_ns)
    )
    response = client.get("/api/system/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["started_at_ns"] == str(started_at_ns)
    assert body["uptime_seconds"] >= 5
    assert body["all_time_count"] == 0
    assert body["all_time_peak_minute"] is None
    assert body["open_count"] == 0
    assert body["all_time_lifetime"] is None


@pytest.mark.asyncio
async def test_system_stats_endpoint_returns_extended_aggregates(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "sys.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now = time.time_ns()
    await store.enqueue(_seed_opp(store, start_ns=now, spread_pct=Decimal("2"), pair="BTC-USD"))
    await store.enqueue(_seed_opp(store, start_ns=now - 1, spread_pct=Decimal("4"), pair="BTC-USD"))
    await store.enqueue(_seed_opp(store, start_ns=now - 2, spread_pct=Decimal("1"), pair="ETH-USD"))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    client = TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))
    response = client.get("/api/system/stats?window=1h")
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1h"
    assert body["count"] == 3
    assert Decimal(body["max_spread_pct"]) == Decimal("4")
    assert body["top_pair"] == "BTC-USD"
    assert body["peak_minute"] is not None
    assert body["lifetime"] is None, "no episode has closed yet"


@pytest.mark.asyncio
async def test_stats_and_overview_report_episode_lifetimes(tmp_path: Path) -> None:
    # ARB-031: lifetime distribution covers episodes that started in the
    # window and have closed; open ones are counted separately.
    store = OpportunityStore(
        str(tmp_path / "life.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    now = time.time_ns()
    second = 1_000_000_000
    still_open = _seed_opp(store, start_ns=now, pair="ETH-USD")
    for offset, duration in ((1, 2 * second), (2, 4 * second), (3, 30 * second)):
        opened = _seed_opp(store, start_ns=now - offset)
        await store.enqueue(opened)
        await store.enqueue(close_episode(opened, duration_ns=duration))
    await store.enqueue(still_open)
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    client = TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))
    stats = client.get("/api/system/stats?window=1h").json()
    assert stats["count"] == 4
    assert stats["lifetime"] == {
        "closed_count": 3,
        "p50_seconds": 4.0,
        "p90_seconds": 30.0,
        "max_seconds": 30.0,
    }
    overview = client.get("/api/system/overview").json()
    assert overview["open_count"] == 1
    assert overview["all_time_lifetime"]["closed_count"] == 3

    recent = client.get("/api/opportunities/recent?limit=10").json()
    assert recent[0]["pair"] == "ETH-USD" and recent[0]["end_ns"] is None
    assert recent[1]["end_ns"] == str(now - 1 + 2 * second)
    assert recent[1]["duration_ns"] == str(2 * second)
    assert recent[1]["close_reason"] == "spread_closed"


@pytest.mark.asyncio
async def test_system_timeseries_endpoint_returns_buckets(tmp_path: Path) -> None:
    store = OpportunityStore(
        str(tmp_path / "ts.sqlite3"), batch_size=10, flush_interval_seconds=0.05
    )
    await store.initialize()
    runner = asyncio.create_task(store.run())
    bucket_ns = 60 * 1_000_000_000
    base = (time.time_ns() // bucket_ns) * bucket_ns
    await store.enqueue(_seed_opp(store, start_ns=base))
    await store.enqueue(_seed_opp(store, start_ns=base + bucket_ns))
    await store.close()
    await asyncio.wait_for(runner, timeout=1.0)

    client = TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))
    response = client.get("/api/system/timeseries?window=1h&bucket_seconds=60")
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1h"
    assert body["bucket_seconds"] == 60
    assert len(body["points"]) >= 1
    assert isinstance(body["points"][0]["bucket_start_ns"], str)


def test_system_reset_endpoint_is_not_exposed() -> None:
    client = TestClient(
        create_app(OpportunityStore(":memory:"), OrderBookManager(), LiveBroadcaster())
    )

    assert client.post("/api/system/reset").status_code == 404


@pytest.mark.asyncio
async def test_system_endpoints_handle_empty_store(tmp_path: Path) -> None:
    store = OpportunityStore(str(tmp_path / "empty.sqlite3"))
    await store.initialize()
    client = TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))

    overview = client.get("/api/system/overview").json()
    assert overview["all_time_count"] == 0
    assert overview["all_time_peak_minute"] is None

    stats = client.get("/api/system/stats?window=1w").json()
    assert stats["count"] == 0
    assert stats["top_pair"] is None
    assert stats["peak_minute"] is None

    ts = client.get("/api/system/timeseries?window=1h&bucket_seconds=60").json()
    assert ts["points"] == []


def test_pricing_endpoints_walk_eligible_books_and_report_fill_rates(tmp_path: Path) -> None:
    # ARB-032: VWAP per venue, side and notional as decimal strings, an
    # explicit insufficient_depth result rather than a made-up price, and the
    # venue's depth ceiling on every quote.
    from arb.pricing import DepthSampler

    manager = OrderBookManager()
    manager.apply(
        MarketEvent(
            "gemini",
            "BTC-USD",
            EventKind.SNAPSHOT,
            1,
            1,
            bids=(PriceLevel(Decimal("99"), Decimal("1")),),
            asks=(
                PriceLevel(Decimal("100"), Decimal("1")),
                PriceLevel(Decimal("104"), Decimal("1")),
            ),
        )
    )
    manager.apply(
        MarketEvent(
            "binance",
            "ETH-USD",
            EventKind.SNAPSHOT,
            1,
            1,
            bids=(PriceLevel(Decimal("9"), Decimal("1")),),
            asks=(PriceLevel(Decimal("10"), Decimal("1")),),
        )
    )
    sampler = DepthSampler(
        manager,
        [Decimal("50"), Decimal("150")],
        {"gemini": None, "binance": 5000},
        {},
        interval_seconds=5.0,
    )
    sampler.sample_all()
    store = OpportunityStore(str(tmp_path / "pricing.sqlite3"))
    client = TestClient(create_app(store, manager, LiveBroadcaster(), depth_sampler=sampler))

    body = client.get("/api/pricing/depth?pair=BTC-USD").json()
    assert body["notionals"] == ["50", "150"]
    quotes = {(q["side"], q["notional"]): q for q in body["quotes"]}
    assert quotes[("buy", "50")]["vwap"] == "100"
    assert quotes[("buy", "50")]["subscribed_depth_levels"] is None
    # 100 at 100, then 50 of the 104 at 104: 150 / (1 + 50/104) = 150 * 104 / 154.
    assert Decimal(quotes[("buy", "150")]["vwap"]) == Decimal(150 * 104) / Decimal(154)
    assert quotes[("sell", "150")] == {
        "exchange": "gemini",
        "pair": "BTC-USD",
        "side": "sell",
        "notional": "150",
        "vwap": None,
        "insufficient_depth": True,
        "filled_notional": "99",
        "filled_base": "1",
        "levels_used": 1,
        "subscribed_depth_levels": None,
    }
    everything = client.get("/api/pricing/depth").json()["quotes"]
    assert {(q["exchange"], q["pair"]) for q in everything} == {
        ("gemini", "BTC-USD"),
        ("binance", "ETH-USD"),
    }
    assert all(
        q["subscribed_depth_levels"] == 5000 for q in everything if q["exchange"] == "binance"
    )

    rates = client.get("/api/pricing/fill-rates").json()
    assert rates["samples"] == 1 and rates["sample_interval_seconds"] == 5.0
    by_key = {(r["exchange"], r["pair"], r["notional"], r["side"]): r for r in rates["rows"]}
    assert by_key[("gemini", "BTC-USD", "150", "buy")]["fill_rate"] == 1.0
    assert by_key[("gemini", "BTC-USD", "150", "sell")]["fill_rate"] == 0.0
    assert by_key[("binance", "ETH-USD", "50", "buy")]["filled"] == 0

    unconfigured = TestClient(create_app(store, manager, LiveBroadcaster()))
    assert unconfigured.get("/api/pricing/depth").status_code == 404
    assert unconfigured.get("/api/pricing/fill-rates").status_code == 404


def test_depth_endpoint_includes_fee_aware_route_ledgers(tmp_path: Path) -> None:
    from arb.pricing import DepthSampler

    manager = OrderBookManager()
    for exchange, bid, ask in (("gemini", "99", "100"), ("coinbase", "102", "103")):
        manager.apply(
            MarketEvent(
                exchange,
                "BTC-USD",
                EventKind.SNAPSHOT,
                1,
                1,
                bids=(PriceLevel(Decimal(bid), Decimal("20")),),
                asks=(PriceLevel(Decimal(ask), Decimal("20")),),
            )
        )
    sampler = DepthSampler(
        manager,
        [Decimal("1000")],
        {"gemini": None, "coinbase": None},
        {"gemini": Decimal("0.4"), "coinbase": Decimal("0.6")},
        interval_seconds=5.0,
    )
    client = TestClient(
        create_app(
            OpportunityStore(str(tmp_path / "routes.sqlite3")),
            manager,
            LiveBroadcaster(),
            depth_sampler=sampler,
        )
    )

    routes = client.get("/api/pricing/depth?pair=BTC-USD").json()["routes"]
    route = next(
        row
        for row in routes
        if row["buy_exchange"] == "gemini" and row["sell_exchange"] == "coinbase"
    )
    assert route["top_of_book_spread_pct"] == "2.00"
    assert route["gross_executable_spread_pct"] == "2.00"
    assert route["depth_impact_pct"] == "0.00"
    assert route["buy_taker_fee_pct"] == "0.4"
    assert route["sell_taker_fee_pct"] == "0.6"
    assert Decimal(route["net_executable_spread_pct"]) < Decimal(
        route["gross_executable_spread_pct"]
    )
