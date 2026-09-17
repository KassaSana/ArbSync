from __future__ import annotations

import argparse
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from arb.persistence import CREATE_TABLE_SQL
from fee_survival import (
    OpportunityRow,
    apply_scenario,
    load_rows,
    parse_fee_argument,
    parse_utc,
    summarize,
)

SECOND_NS = 1_000_000_000


def make_row(
    *,
    timestamp_ns: int,
    spread_pct: str,
    buy_exchange: str = "coinbase",
    sell_exchange: str = "gemini",
    buy_price: str = "100",
    max_size: str = "2",
    pair: str = "UNI-USD",
) -> OpportunityRow:
    buy = Decimal(buy_price)
    sell = buy * (Decimal(1) + Decimal(spread_pct) / Decimal(100))
    size = Decimal(max_size)
    return OpportunityRow(
        timestamp_ns=timestamp_ns,
        pair=pair,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        buy_price=buy,
        sell_price=sell,
        spread_pct=Decimal(spread_pct),
        max_size=size,
        quote_asset="USD",
        theoretical_profit=(sell - buy) * size,
    )


def seed_database(path: Path, rows: list[OpportunityRow]) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(CREATE_TABLE_SQL)
        connection.executemany(
            "INSERT INTO opportunities (timestamp_ns, pair, buy_exchange, sell_exchange, "
            "buy_price, sell_price, spread_pct, max_size, quote_asset, theoretical_profit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.timestamp_ns,
                    row.pair,
                    row.buy_exchange,
                    row.sell_exchange,
                    str(row.buy_price),
                    str(row.sell_price),
                    str(row.spread_pct),
                    str(row.max_size),
                    row.quote_asset,
                    str(row.theoretical_profit),
                )
                for row in rows
            ],
        )


def test_fees_on_both_legs_decide_survival() -> None:
    rows = [
        make_row(timestamp_ns=1, spread_pct="0.50"),
        make_row(timestamp_ns=2, spread_pct="0.30", buy_price="50"),
    ]
    result = apply_scenario("test", {"coinbase": Decimal("0.20"), "gemini": Decimal("0.20")}, rows)

    assert result.survivors == 1
    assert result.distinct_survivors == 1
    # 0.50% - 0.20% - 0.20% = 0.10% of a 200 USD notional.
    assert result.net_profit_by_quote == {"USD": Decimal("0.2")}
    assert result.missing_fee_exchanges == ()


def test_repeated_detections_of_one_quote_pair_are_paid_once() -> None:
    rows = [make_row(timestamp_ns=t, spread_pct="0.50") for t in (1, 2, 3)]
    rows.append(make_row(timestamp_ns=4, spread_pct="0.50", max_size="3"))
    result = apply_scenario("test", {"coinbase": Decimal(0), "gemini": Decimal(0)}, rows)

    assert result.survivors == 4
    assert result.distinct_survivors == 2
    # 0.5% of 200 USD once, plus 0.5% of 300 USD once.
    assert result.net_profit_by_quote == {"USD": Decimal("2.5")}


def test_rows_touching_venues_without_a_fee_entry_are_skipped_and_named() -> None:
    rows = [
        make_row(timestamp_ns=1, spread_pct="1.00"),
        make_row(timestamp_ns=2, spread_pct="1.00", sell_exchange="binance"),
    ]
    result = apply_scenario("test", {"coinbase": Decimal(0), "gemini": Decimal(0)}, rows)

    assert result.survivors == 1
    assert result.missing_fee_exchanges == ("binance",)


def test_load_rows_applies_an_inclusive_window_and_keeps_decimal_strings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opps.sqlite3"
    seed_database(
        database,
        [
            make_row(timestamp_ns=1 * SECOND_NS, spread_pct="0.10"),
            make_row(timestamp_ns=2 * SECOND_NS, spread_pct="0.20"),
            make_row(timestamp_ns=3 * SECOND_NS, spread_pct="0.30"),
        ],
    )

    loaded = load_rows(database, 2 * SECOND_NS, 3 * SECOND_NS)

    assert [row.spread_pct for row in loaded] == [Decimal("0.20"), Decimal("0.30")]
    assert isinstance(loaded[0].buy_price, Decimal)
    assert load_rows(database, None, None)[0].spread_pct == Decimal("0.10")


def test_summary_counts_routes_pairs_and_distinct_quotes() -> None:
    rows = [
        make_row(timestamp_ns=1, spread_pct="0.10"),
        make_row(timestamp_ns=2, spread_pct="0.10"),
        make_row(
            timestamp_ns=3, spread_pct="0.40", buy_exchange="gemini", sell_exchange="coinbase"
        ),
    ]
    summary = summarize(rows)

    assert summary["opportunities"] == 3
    assert summary["distinct_quote_pairs"] == 2
    assert summary["routes"] == {"coinbase->gemini USD": 2, "gemini->coinbase USD": 1}
    assert summary["pairs"] == {"UNI-USD": 3}
    spread = summary["spread_pct"]
    assert isinstance(spread, dict)
    assert spread["min"] == "0.10" and spread["max"] == "0.40"
    assert summarize([])["opportunities"] == 0


def test_argument_parsing() -> None:
    assert parse_fee_argument("gemini=0.25") == ("gemini", Decimal("0.25"))
    assert parse_utc("2026-09-16T10:25:38Z") == parse_utc("2026-09-16T10:25:38")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_fee_argument("gemini")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_fee_argument("gemini=-1")
