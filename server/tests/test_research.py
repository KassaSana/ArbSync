from __future__ import annotations

from decimal import Decimal

from arb.replay import ReplayObservation
from research import _hy_correlation, _lead_lag_rows, _price_series, _survival_rows


def _observation(
    exchange: str,
    wall_ns: int,
    bid: str,
    ask: str,
    *,
    eligible: bool = True,
) -> ReplayObservation:
    return ReplayObservation(
        exchange=exchange,
        pair="BTC-USD",
        best_bid_price=bid,
        best_ask_price=ask,
        wall_ns=wall_ns,
        mono_ns=wall_ns,
        sequence=wall_ns,
        eligible=eligible,
    )


def test_hayashi_yoshida_correlation_aligns_asynchronous_intervals() -> None:
    from research import ReturnInterval

    left = [
        ReturnInterval(0, 10, 1.0),
        ReturnInterval(10, 20, 1.0),
    ]
    right = [
        ReturnInterval(5, 15, 1.0),
        ReturnInterval(15, 25, 1.0),
    ]

    correlation, overlaps = _hy_correlation(left, right, 5)

    assert correlation == 1.0
    assert overlaps == 2


def test_lead_lag_reports_the_leader_and_measurement_direction() -> None:
    observations = [
        _observation("gemini", 1_000_000_000, "99", "101"),
        _observation("gemini", 2_000_000_000, "100", "102"),
        _observation("gemini", 3_000_000_000, "97.98", "99.98"),
        _observation("gemini", 4_000_000_000, "100.9494", "102.9494"),
        _observation("gemini", 5_000_000_000, "99.420159", "101.420159"),
        _observation("gemini", 6_000_000_000, "101.930663", "103.930663"),
        _observation("coinbase", 1_500_000_000, "99", "101"),
        _observation("coinbase", 2_500_000_000, "100", "102"),
        _observation("coinbase", 3_500_000_000, "97.98", "99.98"),
        _observation("coinbase", 4_500_000_000, "100.9494", "102.9494"),
        _observation("coinbase", 5_500_000_000, "99.420159", "101.420159"),
        _observation("coinbase", 6_500_000_000, "101.930663", "103.930663"),
    ]

    [row] = _lead_lag_rows(
        observations,
        tick_bin_ns=1,
        max_lag_ns=1_000_000_000,
        lag_step_ns=500_000_000,
    )

    assert row["status"] == "ok"
    assert row["leader_exchange"] == "gemini"
    assert row["follower_exchange"] == "coinbase"
    assert row["estimated_lead_ns"] == 500_000_000
    assert row["hayashi_yoshida_correlation"] == 1.0
    assert row["correlation_ci_95_low"] <= 1.0 <= row["correlation_ci_95_high"]


def test_price_series_uses_canonical_post_apply_observations() -> None:
    observations = [
        # Snapshot establishes the top of book.
        _observation("gemini", 1, "100", "102"),
        # A deep-only delta must not move the research midpoint to 99/103.
        _observation("gemini", 2, "100", "102"),
        # One-sided best-ask update is represented by the new canonical top.
        _observation("gemini", 3, "100", "101"),
        # Deleting the old best ask exposes the next level.
        _observation("gemini", 4, "100", "102"),
        # Invalidation may retain a cached top, but it is not a valid tick.
        _observation("gemini", 5, "100", "102", eligible=False),
    ]

    series = _price_series(observations, tick_bin_ns=1)

    assert [tick.price for tick in series[("BTC-USD", "gemini")]] == [
        Decimal("101"),
        Decimal("101"),
        Decimal("100.5"),
        Decimal("101"),
    ]


def test_survival_rows_separate_fee_survival_from_insufficient_size() -> None:
    rows = _survival_rows(
        [
            {
                "quote_asset": "USD",
                "pricing_ledgers": [
                    {
                        "notional": "100",
                        "net_executable_spread_pct": "0.25",
                        "insufficient_depth": False,
                    },
                    {
                        "notional": "1000",
                        "net_executable_spread_pct": None,
                        "insufficient_depth": True,
                    },
                ],
            },
            {
                "quote_asset": "USD",
                "pricing_ledgers": [
                    {
                        "notional": "100",
                        "net_executable_spread_pct": "-0.10",
                        "insufficient_depth": False,
                    }
                ],
            },
        ]
    )

    assert rows == [
        {
            "module": "survival_by_notional",
            "notional": "100",
            "observations": 2,
            "priced": 2,
            "insufficient_depth": 0,
            "survivors": 1,
            "net_profit_by_quote": {"USD": "0.25"},
            "fee_survival_rate": 0.5,
            "executable_size_survival_rate": 0.5,
        },
        {
            "module": "survival_by_notional",
            "notional": "1000",
            "observations": 1,
            "priced": 0,
            "insufficient_depth": 1,
            "survivors": 0,
            "net_profit_by_quote": {},
            "fee_survival_rate": None,
            "executable_size_survival_rate": 0.0,
        },
    ]


def test_survival_profit_is_decimal_derived() -> None:
    rows = _survival_rows(
        [
            {
                "quote_asset": "USD",
                "pricing_ledgers": [
                    {
                        "notional": "1000",
                        "net_executable_spread_pct": "0.333333333333333333",
                        "insufficient_depth": False,
                    }
                ],
            }
        ]
    )

    assert Decimal(rows[0]["net_profit_by_quote"]["USD"]) == Decimal("3.33333333333333333")
