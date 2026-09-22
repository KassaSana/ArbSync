"""Filtered, cursor-paginated opportunity history and its bounded JSONL export."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from arb.api import create_app
from arb.broadcast import LiveBroadcaster
from arb.history import (
    HISTORY_ORDER,
    CursorError,
    HistoryCursor,
    HistoryFilters,
    build_page_query,
)
from arb.orderbook import OrderBookManager
from arb.persistence import HistoryBudgetExceeded, OpportunityStore
from arb.types import OpportunityEpisode, PricingLedger
from episodes import close_episode, make_episode
from fastapi.testclient import TestClient
from starlette.types import Message

ROUTES = (
    ("BTC-USD", "gemini", "coinbase"),
    ("BTC-USD", "coinbase", "gemini"),
    ("ETH-USD", "gemini", "coinbase"),
    ("ETH-USDT", "binance", "coinbase"),
)


def _episodes() -> list[OpportunityEpisode]:
    """Twelve episodes over four routes; two share a start to exercise the id tiebreak."""
    episodes: list[OpportunityEpisode] = []
    for index in range(12):
        pair, buy, sell = ROUTES[index % len(ROUTES)]
        episode = make_episode(
            start_ns=1_000 + 100 * (index // 2),
            pair=pair,
            buy_exchange=buy,
            sell_exchange=sell,
            spread_pct=Decimal(f"0.{index + 1:02d}00"),
            theoretical_profit=Decimal("0.00000001") * (index + 1),
        )
        if index % 3 == 0:
            episode = close_episode(episode, duration_ns=50)
        elif index % 3 == 1:
            episode = close_episode(episode, duration_ns=70, reason="book_ineligible")
        episodes.append(episode)
    return episodes


async def _seed(path: Path, episodes: list[OpportunityEpisode]) -> OpportunityStore:
    store = OpportunityStore(str(path))
    await store.initialize()
    await store._flush(episodes)
    await store._close_db()
    return store


def _key(item: dict[str, Any]) -> tuple[int, str, str, str]:
    return (int(item["start_ns"]), item["pair"], item["buy_exchange"], item["sell_exchange"])


async def _walk(
    store: OpportunityStore, filters: HistoryFilters, limit: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: HistoryCursor | None = None
    while True:
        page = await store.history_page(filters, cursor, limit)
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


@pytest.mark.asyncio
async def test_paged_traversal_matches_one_query_in_start_then_id_order(tmp_path: Path) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    whole = (await store.history_page(HistoryFilters(), limit=500)).items
    assert len(whole) == 12
    for limit in (1, 5, 11, 12):
        assert await _walk(store, HistoryFilters(), limit) == whole
    starts = [item["start_ns"] for item in whole]
    assert starts == sorted(starts, reverse=True)
    # Equal starts come back newest-inserted first (id DESC), and a page break
    # between them neither repeats nor drops either row.
    tied = [item for item in whole if item["start_ns"] == 1_500]
    assert [item["pair"] for item in tied] == ["ETH-USDT", "ETH-USD"]
    first = await store.history_page(HistoryFilters(), limit=1)
    assert first.next_cursor is not None
    assert (await store.history_page(HistoryFilters(), first.next_cursor, 1)).items == [whole[1]]


@pytest.mark.asyncio
async def test_last_full_page_has_no_cursor_and_empty_store_returns_nothing(
    tmp_path: Path,
) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    page = await store.history_page(HistoryFilters(), limit=12)
    assert len(page.items) == 12 and page.next_cursor is None
    empty = await _seed(tmp_path / "empty.sqlite3", [])
    page = await empty.history_page(HistoryFilters(), limit=10)
    assert page.items == [] and page.next_cursor is None


@pytest.mark.asyncio
async def test_each_filter_and_their_conjunction(tmp_path: Path) -> None:
    episodes = _episodes()
    store = await _seed(tmp_path / "h.sqlite3", episodes)

    async def keys(**filters: Any) -> set[tuple[int, str, str, str]]:
        return {_key(item) for item in await _walk(store, HistoryFilters(**filters), 3)}

    def expected(predicate: Any) -> set[tuple[int, str, str, str]]:
        return {
            (e.start_ns, e.pair, e.buy_exchange, e.sell_exchange) for e in episodes if predicate(e)
        }

    assert await keys(pair="BTC-USD") == expected(lambda e: e.pair == "BTC-USD")
    assert await keys(buy_exchange="binance") == expected(lambda e: e.buy_exchange == "binance")
    assert await keys(sell_exchange="gemini") == expected(lambda e: e.sell_exchange == "gemini")
    assert await keys(close_reason="book_ineligible") == expected(
        lambda e: e.close_reason == "book_ineligible"
    )
    assert await keys(state="open") == expected(lambda e: e.is_open)
    assert await keys(state="closed") == expected(lambda e: not e.is_open)
    # from_ns is inclusive and to_ns exclusive on start_ns.
    assert await keys(from_ns=1_100, to_ns=1_300) == expected(lambda e: 1_100 <= e.start_ns < 1_300)
    assert await keys(pair="BTC-USD", buy_exchange="gemini", state="closed") == expected(
        lambda e: e.pair == "BTC-USD" and e.buy_exchange == "gemini" and not e.is_open
    )
    assert await keys(pair="SOL-USD") == set()


@pytest.mark.asyncio
async def test_orphaned_rows_are_closed_not_open(tmp_path: Path) -> None:
    path = tmp_path / "h.sqlite3"
    await _seed(path, [make_episode(start_ns=5)])
    restarted = OpportunityStore(str(path))
    await restarted.initialize()  # marks the row a previous process left open

    assert (await restarted.history_page(HistoryFilters(state="open"))).items == []
    closed = (await restarted.history_page(HistoryFilters(state="closed"))).items
    assert [(item["close_reason"], item["end_ns"]) for item in closed] == [("orphaned", None)]
    orphaned = await restarted.history_page(HistoryFilters(close_reason="orphaned"))
    assert len(orphaned.items) == 1


@pytest.mark.asyncio
async def test_pages_are_stable_across_concurrent_inserts_and_closes(tmp_path: Path) -> None:
    episodes = _episodes()
    store = await _seed(tmp_path / "h.sqlite3", episodes)
    before = {_key(item) for item in await _walk(store, HistoryFilters(), 500)}

    first = await store.history_page(HistoryFilters(), limit=4)
    assert first.next_cursor is not None
    # While the client pages: a newer episode, an older-start episode written
    # later (a higher id below the cursor), and a close of a row not yet read.
    open_unread = next(e for e in episodes if e.is_open and e.start_ns < 1_200)
    writer = OpportunityStore(store.db_path)
    await writer._flush(
        [
            make_episode(start_ns=9_999, pair="SOL-USD"),
            make_episode(start_ns=1_000, pair="LTC-USD"),
            close_episode(open_unread, duration_ns=5),
        ]
    )
    await writer._close_db()

    items = list(first.items)
    cursor: HistoryCursor | None = first.next_cursor
    while cursor is not None:
        page = await store.history_page(HistoryFilters(), cursor, 4)
        items.extend(page.items)
        cursor = page.next_cursor

    keys = [_key(item) for item in items]
    assert len(keys) == len(set(keys)), "a row was repeated"
    assert set(keys) == before, "a pre-existing row was skipped or a new one leaked in"
    closed = next(
        item
        for item in items
        if _key(item)[0] == open_unread.start_ns and item["pair"] == open_unread.pair
    )
    assert closed["close_reason"] == "spread_closed"  # fields are read as of the page


def test_cursor_round_trips_and_rejects_malformed_or_foreign_tokens() -> None:
    filters = HistoryFilters(pair="BTC-USD")
    cursor = HistoryCursor(10, 3, 99, filters.fingerprint())
    assert HistoryCursor.decode(cursor.encode(), filters) == cursor

    with pytest.raises(CursorError, match="different filters"):
        HistoryCursor.decode(cursor.encode(), HistoryFilters(pair="ETH-USD"))

    def token(body: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")

    good = {"v": 1, "s": 10, "i": 3, "m": 99, "f": filters.fingerprint()}
    for bad in (
        "",
        "x" * 600,
        "!!!",
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        token([1, 2]),
        token({**good, "v": 2}),
        token({**good, "s": True}),
        token({**good, "i": -1}),
        token({**good, "m": 2**63}),
        token({**good, "s": "10"}),
        token({**good, "f": 7}),
    ):
        with pytest.raises(CursorError):
            HistoryCursor.decode(bad, filters)


def test_filters_validate_bounds() -> None:
    with pytest.raises(ValueError, match="less than"):
        HistoryFilters(from_ns=5, to_ns=5)
    with pytest.raises(ValueError, match="between"):
        HistoryFilters(from_ns=-1)
    with pytest.raises(ValueError, match="between"):
        HistoryFilters(to_ns=2**63)


def test_filter_values_are_bound_parameters() -> None:
    hostile = "BTC-USD' OR 1=1 --"
    sql, params = build_page_query(HistoryFilters(pair=hostile), (5, 6), 10, 3)
    assert hostile not in sql
    assert params == [10, 5, 6, hostile, 3]


def _plan(db_path: str, filters: HistoryFilters, after: tuple[int, int] | None) -> str:
    sql, params = build_page_query(filters, after, 2**62, 101)
    with closing(sqlite3.connect(db_path)) as db:
        rows = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(row[-1]) for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "index"),
    [
        (HistoryFilters(), "idx_episodes_start"),
        (HistoryFilters(from_ns=1, to_ns=2), "idx_episodes_start"),
        (HistoryFilters(state="open"), "idx_episodes_close_start"),
        (HistoryFilters(state="closed"), "idx_episodes_start"),
        (HistoryFilters(close_reason="orphaned"), "idx_episodes_close_start"),
        (HistoryFilters(buy_exchange="gemini", sell_exchange="coinbase"), "idx_episodes_start"),
        (HistoryFilters(pair="BTC-USD"), "idx_episodes_pair_start"),
        (HistoryFilters(pair="BTC-USD", from_ns=1, to_ns=2), "idx_episodes_pair_start"),
    ],
)
async def test_page_queries_walk_an_index_in_order(
    tmp_path: Path, filters: HistoryFilters, index: str
) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    for after in (None, (1_500, 7)):
        plan = _plan(store.db_path, filters, after)
        assert index in plan, plan
        assert "TEMP B-TREE" not in plan, plan


@pytest.mark.asyncio
async def test_page_query_that_exceeds_its_budget_fails_fast(tmp_path: Path) -> None:
    episodes = [
        make_episode(start_ns=index + 1, pair="BTC-USD", buy_exchange=f"x{index % 50}")
        for index in range(3_000)
    ]
    store = await _seed(tmp_path / "h.sqlite3", episodes)
    with pytest.raises(HistoryBudgetExceeded):
        await store.history_page(HistoryFilters(buy_exchange="nowhere"), budget_seconds=0)
    # The same sparse query completes under the normal budget.
    page = await store.history_page(HistoryFilters(buy_exchange="nowhere"))
    assert page.items == []


# --- API ---------------------------------------------------------------------


def _client(store: OpportunityStore) -> TestClient:
    return TestClient(create_app(store, OrderBookManager(), LiveBroadcaster()))


@pytest.mark.asyncio
async def test_history_endpoint_pages_with_cursor_and_wire_strings(tmp_path: Path) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    client = _client(store)

    body = client.get("/api/opportunities", params={"limit": 5, "pair": "BTC-USD"}).json()
    assert body["order"] == HISTORY_ORDER
    assert len(body["items"]) == 5
    first = body["items"][0]
    assert isinstance(first["start_ns"], str)
    assert first["quote_asset"] == "USD"
    seen = list(body["items"])
    while body["next_cursor"] is not None:
        body = client.get(
            "/api/opportunities",
            params={"limit": 5, "pair": "BTC-USD", "cursor": body["next_cursor"]},
        ).json()
        seen.extend(body["items"])
    assert len(seen) == 6
    assert all(item["pair"] == "BTC-USD" for item in seen)
    # Decimal strings survive exactly, including trailing zeros.
    assert {item["spread_pct"] for item in seen} >= {"0.0100", "0.1000"}


@pytest.mark.asyncio
async def test_history_endpoint_rejects_invalid_requests(tmp_path: Path) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    client = _client(store)
    cursor = client.get("/api/opportunities", params={"limit": 1}).json()["next_cursor"]

    for params, status in (
        ({"limit": 0}, 422),
        ({"limit": 501}, 422),
        ({"from_ns": "-1"}, 422),
        ({"from_ns": "1.5"}, 422),
        ({"to_ns": "9223372036854775808"}, 422),
        ({"from_ns": "5", "to_ns": "5"}, 422),
        ({"state": "pending"}, 422),
        ({"close_reason": "expired"}, 422),
        ({"pair": "BTC/USD"}, 422),
        ({"buy_exchange": "Gemini"}, 422),
        ({"cursor": "not-a-cursor"}, 400),
        ({"cursor": cursor, "pair": "BTC-USD"}, 400),
    ):
        response = client.get("/api/opportunities", params=params)
        assert response.status_code == status, (params, response.text)


@pytest.mark.asyncio
async def test_history_endpoint_reports_budget_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())

    async def exhausted(*args: Any, **kwargs: Any) -> Any:
        raise HistoryBudgetExceeded

    monkeypatch.setattr(store, "history_page", exhausted)
    response = _client(store).get("/api/opportunities")
    assert response.status_code == 503
    assert "narrow" in response.json()["detail"]


def _jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.asyncio
async def test_export_streams_episodes_and_an_end_record(tmp_path: Path) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    client = _client(store)
    response = client.get("/api/opportunities/export", params={"state": "closed"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]
    records = _jsonl(response.text)
    *episodes, end = records
    assert all(record["type"] == "episode" for record in episodes)
    assert end == {
        "type": "end",
        "order": HISTORY_ORDER,
        "rows": len(episodes),
        "truncated": False,
        "next_cursor": None,
        "error": None,
    }
    paged = client.get("/api/opportunities", params={"state": "closed", "limit": 500}).json()
    assert [{**e, "type": None} for e in episodes] == [
        {**item, "type": None} for item in paged["items"]
    ]
    assert {e["theoretical_profit"] for e in episodes} >= {"1E-8", "4E-8"}


@pytest.mark.asyncio
async def test_truncated_export_resumes_from_its_cursor(tmp_path: Path) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    client = _client(store)
    first = _jsonl(client.get("/api/opportunities/export", params={"max_rows": 5}).text)
    assert first[-1]["rows"] == 5 and first[-1]["truncated"] is True
    rest = _jsonl(
        client.get(
            "/api/opportunities/export",
            params={"max_rows": 100, "cursor": first[-1]["next_cursor"]},
        ).text
    )
    assert rest[-1]["truncated"] is False
    exported = first[:-1] + rest[:-1]
    whole = client.get("/api/opportunities", params={"limit": 500}).json()["items"]
    assert [_key(e) for e in exported] == [_key(item) for item in whole]


@pytest.mark.asyncio
async def test_export_ends_with_resumable_error_when_budget_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    real = store._history_rows
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HistoryBudgetExceeded
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "_history_rows", flaky)
    monkeypatch.setattr(
        store,
        "history_export_chunks",
        lambda filters, cursor, max_rows: OpportunityStore.history_export_chunks(
            store, filters, cursor, max_rows, page_size=4
        ),
    )
    records = _jsonl(_client(store).get("/api/opportunities/export").text)
    end = records[-1]
    assert end["error"] == "query_budget_exceeded"
    assert end["truncated"] is True and end["rows"] == 4
    monkeypatch.setattr(store, "_history_rows", real)
    rest = _jsonl(
        _client(store).get("/api/opportunities/export", params={"cursor": end["next_cursor"]}).text
    )
    assert rest[-1]["rows"] == 8 and rest[-1]["truncated"] is False


@pytest.mark.asyncio
async def test_only_one_export_runs_at_a_time(tmp_path: Path) -> None:
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    app = create_app(store, OrderBookManager(), LiveBroadcaster())
    client = TestClient(app)
    lock: asyncio.Lock = app.state.history_export_lock

    await lock.acquire()
    busy = client.get("/api/opportunities/export")
    assert busy.status_code == 429
    lock.release()

    assert client.get("/api/opportunities/export").status_code == 200
    assert not lock.locked()  # released once the stream finished
    for params in ({"max_rows": 0}, {"max_rows": 100_001}, {"cursor": "bad"}):
        assert client.get("/api/opportunities/export", params=params).status_code in (400, 422)
    assert not lock.locked()  # a rejected request never takes the lock


@pytest.mark.asyncio
async def test_multi_page_export_lines_match_api_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Export lines splice stored ledger JSON instead of re-encoding it; parsed,
    # they must equal the API items across several small pages.
    episodes = [
        replace(episode, pricing_ledgers=(_ledger(index),)) if index % 2 else episode
        for index, episode in enumerate(_episodes())
    ]
    store = await _seed(tmp_path / "h.sqlite3", episodes)
    monkeypatch.setattr(
        store,
        "history_export_chunks",
        lambda filters, cursor, max_rows: OpportunityStore.history_export_chunks(
            store, filters, cursor, max_rows, page_size=5
        ),
    )
    client = _client(store)
    *exported, end = _jsonl(client.get("/api/opportunities/export").text)
    items = client.get("/api/opportunities", params={"limit": 500}).json()["items"]
    assert end["rows"] == 12 and end["truncated"] is False
    assert [{k: v for k, v in e.items() if k != "type"} for e in exported] == items
    assert any(item["pricing_ledgers"] for item in items)


def _ledger(index: int) -> PricingLedger:
    return PricingLedger(
        notional=Decimal("1000"),
        top_of_book_spread_pct=Decimal("0.5"),
        buy_vwap=Decimal(f"100.{index}"),
        sell_vwap=Decimal("100.80"),
        gross_executable_spread_pct=Decimal("0.2"),
        depth_impact_pct=Decimal("-0.3"),
        buy_taker_fee_pct=Decimal("0.4"),
        sell_taker_fee_pct=Decimal("0.6"),
        fee_impact_pct=Decimal("-1.0"),
        net_executable_spread_pct=Decimal("-0.8"),
        insufficient_depth=False,
    )


def _export_scope(spec_version: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/opportunities/export",
        "raw_path": b"/api/opportunities/export",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("test", 1),
        "server": ("test", 80),
    }


async def _never_disconnect() -> dict[str, Any]:
    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}


async def _disconnected() -> dict[str, Any]:
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disconnect_before_start", "start_fails", "body_fails", "stall"])
async def test_export_slot_is_released_however_the_response_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Regression: the slot used to be released only by the body generator, which
    Starlette never starts or closes on these paths, refusing every later export."""
    store = await _seed(tmp_path / "h.sqlite3", _episodes())
    app = create_app(store, OrderBookManager(), LiveBroadcaster())
    lock: asyncio.Lock = app.state.history_export_lock
    monkeypatch.setattr("arb.api.HISTORY_EXPORT_SEND_TIMEOUT_SECONDS", 0.05)

    async def send(message: Message) -> None:
        kind = message["type"]
        if case == "start_fails" and kind == "http.response.start":
            raise OSError("client went away")
        if case == "body_fails" and kind == "http.response.body":
            raise OSError("client went away")
        if case == "stall" and kind == "http.response.body":
            await asyncio.sleep(3600)

    spec, receive = (
        ("2.3", _disconnected) if case == "disconnect_before_start" else ("2.4", _never_disconnect)
    )
    try:
        await app(_export_scope(spec), receive, send)
    except (OSError, TimeoutError):
        pass  # how the server sees a failed or stalled client
    except Exception as exc:  # Starlette may wrap a failed send
        assert "ClientDisconnect" in type(exc).__name__ or isinstance(exc.__cause__, OSError)
    assert not lock.locked()
    assert TestClient(app).get("/api/opportunities/export").status_code == 200
