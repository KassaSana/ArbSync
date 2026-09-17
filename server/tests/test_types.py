from __future__ import annotations

from decimal import Decimal

from arb.types import TopOfBook
from episodes import make_episode


def test_top_of_book_payload_stringifies_decimals() -> None:
    top = TopOfBook(
        exchange="gemini",
        pair="BTC-USD",
        best_bid_price=Decimal("100.5"),
        best_bid_size=Decimal("1.25"),
        best_ask_price=Decimal("101.5"),
        best_ask_size=Decimal("0.75"),
        sequence=42,
        timestamp_ns=999,
    )
    payload = top.as_payload()
    assert payload["exchange"] == "gemini"
    assert payload["pair"] == "BTC-USD"
    assert payload["sequence"] == 42
    assert payload["timestamp_ns"] == "999"
    for key in ("best_bid_price", "best_bid_size", "best_ask_price", "best_ask_size"):
        assert isinstance(payload[key], str)
    # Exact string round-trip — no float drift.
    assert payload["best_bid_price"] == "100.5"


def test_arbitrage_opportunity_payload_round_trips_decimal_strings() -> None:
    opp = make_episode(
        start_ns=1,
        pair="ETH-USD",
        quote_asset="USD",
        buy_exchange="gemini",
        sell_exchange="binance",
        buy_price=Decimal("2000.123"),
        sell_price=Decimal("2010.456"),
        spread_pct=Decimal("0.516"),
        max_size=Decimal("0.25"),
        theoretical_profit=Decimal("2.583250"),
    )
    payload = opp.as_payload()
    assert payload["pair"] == "ETH-USD"
    assert payload["start_ns"] == "1"
    assert payload["end_ns"] is None
    assert payload["buy_price"] == "2000.123"
    assert payload["quote_asset"] == "USD"
    assert payload["theoretical_profit"] == "2.583250"
    # All Decimal fields must be strings; non-Decimal scalars unchanged.
    assert isinstance(payload["spread_pct"], str)
    assert isinstance(payload["max_size"], str)
