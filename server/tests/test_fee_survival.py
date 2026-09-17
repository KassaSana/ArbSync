from __future__ import annotations

import argparse
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from arb.persistence import CREATE_TABLE_SQL
from fee_survival import (
    EpisodeRow,
    apply_scenario,
    load_rows,
    parse_fee_argument,
    parse_utc,
    summarize,
)

SECOND_NS = 1_000_000_000


def make_row(
    *,
    start_ns: int,
    spread_pct: str,
    peak_spread_pct: str | None = None,
    duration_ns: int | None = 5 * SECOND_NS,
    buy_exchange: str = "coinbase",
    sell_exchange: str = "gemini",
    buy_price: str = "100",
    max_size: str = "2",
    peak_size: str | None = None,
    pair: str = "UNI-USD",
) -> EpisodeRow:
    """An episode whose peak defaults to its open values."""
    buy = Decimal(buy_price)
    sell = buy * (Decimal(1) + Decimal(spread_pct) / Decimal(100))
    size = Decimal(max_size)
    peak_pct = Decimal(spread_pct if peak_spread_pct is None else peak_spread_pct)
    peak_sz = size if peak_size is None else Decimal(peak_size)
    return EpisodeRow(
        start_ns=start_ns,
        end_ns=None if duration_ns is None else start_ns + duration_ns,
        pair=pair,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        buy_price=buy,
        sell_price=sell,
        spread_pct=Decimal(spread_pct),
        max_size=size,
        quote_asset="USD",
        theoretical_profit=(sell - buy) * size,
        peak_spread_pct=peak_pct,
        peak_size=peak_sz,
        peak_profit=buy * peak_pct / Decimal(100) * peak_sz,
        close_reason=None if duration_ns is None else "spread_closed",
    )


def seed_database(path: Path, rows: list[EpisodeRow]) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(CREATE_TABLE_SQL)
        connection.executemany(
            "INSERT INTO opportunity_episodes (start_ns, end_ns, pair, quote_asset, buy_exchange, "
            "sell_exchange, buy_price, sell_price, spread_pct, max_size, theoretical_profit, "
            "peak_spread_pct, peak_size, peak_profit, close_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.start_ns,
                    row.end_ns,
                    row.pair,
                    row.quote_asset,
                    row.buy_exchange,
                    row.sell_exchange,
                    str(row.buy_price),
                    str(row.sell_price),
                    str(row.spread_pct),
                    str(row.max_size),
                    str(row.theoretical_profit),
                    str(row.peak_spread_pct),
                    str(row.peak_size),
                    str(row.peak_profit),
                    row.close_reason,
                )
                for row in rows
            ],
        )


def test_fees_on_both_legs_decide_survival() -> None:
    rows = [
        make_row(start_ns=1, spread_pct="0.50"),
        make_row(start_ns=2, spread_pct="0.30", buy_price="50"),
    ]
    result = apply_scenario("test", {"coinbase": Decimal("0.20"), "gemini": Decimal("0.20")}, rows)

    assert result.survivors == 1
    # 0.50% - 0.20% - 0.20% = 0.10% of a 200 USD notional.
    assert result.net_profit_by_quote == {"USD": Decimal("0.2")}
    assert result.missing_fee_exchanges == ()


def test_survival_is_judged_at_the_episode_peak() -> None:
    # ARB-031: an episode that opened below the fee line but widened past it
    # survives, paid once at the size recorded at its peak.
    row = make_row(
        start_ns=1, spread_pct="0.30", peak_spread_pct="0.50", max_size="2", peak_size="3"
    )
    fees = {"coinbase": Decimal("0.20"), "gemini": Decimal("0.20")}
    result = apply_scenario("test", fees, [row])

    assert result.survivors == 1
    # 0.10% of 300 USD (peak size 3 at buy price 100), not of the 200 USD at open.
    assert result.net_profit_by_quote == {"USD": Decimal("0.3")}


def test_rows_touching_venues_without_a_fee_entry_are_skipped_and_named() -> None:
    rows = [
        make_row(start_ns=1, spread_pct="1.00"),
        make_row(start_ns=2, spread_pct="1.00", sell_exchange="binance"),
    ]
    result = apply_scenario("test", {"coinbase": Decimal(0), "gemini": Decimal(0)}, rows)

    assert result.survivors == 1
    assert result.missing_fee_exchanges == ("binance",)


def test_load_rows_applies_an_inclusive_window_on_start_and_keeps_decimal_strings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opps.sqlite3"
    seed_database(
        database,
        [
            make_row(start_ns=1 * SECOND_NS, spread_pct="0.10"),
            make_row(start_ns=2 * SECOND_NS, spread_pct="0.20", duration_ns=None),
            make_row(start_ns=3 * SECOND_NS, spread_pct="0.30", peak_spread_pct="0.35"),
        ],
    )

    loaded = load_rows(database, 2 * SECOND_NS, 3 * SECOND_NS)

    assert [row.spread_pct for row in loaded] == [Decimal("0.20"), Decimal("0.30")]
    assert loaded[0].end_ns is None and loaded[0].close_reason is None
    assert loaded[1].peak_spread_pct == Decimal("0.35")
    assert loaded[1].duration_seconds == Decimal(5)
    assert isinstance(loaded[0].buy_price, Decimal)
    assert load_rows(database, None, None)[0].spread_pct == Decimal("0.10")


def test_summary_counts_routes_pairs_reasons_and_lifetimes() -> None:
    rows = [
        make_row(start_ns=1, spread_pct="0.10", duration_ns=2 * SECOND_NS),
        make_row(start_ns=2, spread_pct="0.10", duration_ns=None),
        make_row(
            start_ns=3,
            spread_pct="0.40",
            duration_ns=10 * SECOND_NS,
            buy_exchange="gemini",
            sell_exchange="coinbase",
        ),
    ]
    summary = summarize(rows)

    assert summary["episodes"] == 3
    assert summary["routes"] == {"coinbase->gemini USD": 2, "gemini->coinbase USD": 1}
    assert summary["pairs"] == {"UNI-USD": 3}
    assert summary["close_reasons"] == {"spread_closed": 2, "open": 1}
    spread = summary["peak_spread_pct"]
    assert isinstance(spread, dict)
    assert spread["min"] == "0.10" and spread["max"] == "0.40"
    assert summary["lifetime_seconds"] == {"closed": 2, "p50": "10", "p90": "10", "max": "10"}
    assert summarize([])["episodes"] == 0
    assert "lifetime_seconds" not in summarize([])


def test_argument_parsing() -> None:
    assert parse_fee_argument("gemini=0.25") == ("gemini", Decimal("0.25"))
    assert parse_utc("2026-09-16T10:25:38Z") == parse_utc("2026-09-16T10:25:38")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_fee_argument("gemini")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_fee_argument("gemini=-1")
