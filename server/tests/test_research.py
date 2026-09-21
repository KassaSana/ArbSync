from __future__ import annotations

import math
import random
from decimal import Decimal
from typing import Any, cast

import pytest
from arb.replay import ReplayObservation
from lead_lag import price_series
from research import (
    LeadLagSettings,
    _lead_lag_rows,
    _lead_lag_sensitivity_rows,
    _sensitivity_variants,
    _survival_rows,
)


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


def _shifted_pair(shift_ns: int, count: int = 400, seed: int = 7) -> list[ReplayObservation]:
    """One seeded random-walk path sampled every 100 ms on two venues, coinbase delayed.

    The lag grid step must be at least the 100 ms sampling interval: a lag that
    misaligns by less than one interval overlaps two returns on the other side
    and sits on a plateau of the contrast, so it cannot be told apart.
    """
    rng = random.Random(seed)
    observations: list[ReplayObservation] = []
    mid = 100.0
    for index in range(count):
        mid *= math.exp(rng.gauss(0.0, 0.01))
        bid = Decimal(f"{mid - 1:.6f}")
        ask = Decimal(f"{mid + 1:.6f}")
        stamp = (index + 1) * 100_000_000
        observations.append(_observation("gemini", stamp, str(bid), str(ask)))
        observations.append(_observation("coinbase", stamp + shift_ns, str(bid), str(ask)))
    return observations


SETTINGS = LeadLagSettings(
    tick_bin_ns=1,
    max_lag_ns=1_000_000_000,
    lag_step_ns=500_000_000,
    min_overlap=5,
    windows=2,
    null_surrogates=10,
)


def test_lead_lag_reports_the_leader_and_measurement_direction() -> None:
    [row] = _lead_lag_rows(_shifted_pair(500_000_000), SETTINGS)

    assert row["status"] == "ok"
    assert row["reason"] is None
    assert row["leader_exchange"] == "gemini"
    assert row["follower_exchange"] == "coinbase"
    assert row["estimated_lead_ns"] == 500_000_000
    correlation = row["hayashi_yoshida_correlation"]
    assert isinstance(correlation, float)
    assert correlation == pytest.approx(1.0)
    assert -1.0 <= correlation <= 1.0
    assert "correlation_ci_95_low" not in row
    assert row["null_surrogate_count"] == 20
    assert row["null_exceedance_fraction"] == 0.0
    assert row["window_count"] == 2
    assert row["window_agreement_fraction"] == 1.0


def test_sensitivity_variants_are_one_at_a_time_without_null_surrogates() -> None:
    variants = dict(_sensitivity_variants(SETTINGS))

    assert list(variants) == [
        "baseline",
        "tick_bin_half",
        "tick_bin_double",
        "lag_step_half",
        "lag_step_double",
        "windows_half",
        "windows_double",
        "min_overlap_half",
        "min_overlap_double",
    ]
    assert all(candidate.null_surrogates == 0 for candidate in variants.values())
    assert variants["baseline"] == LeadLagSettings(
        tick_bin_ns=1,
        max_lag_ns=1_000_000_000,
        lag_step_ns=500_000_000,
        min_overlap=5,
        windows=2,
        null_surrogates=0,
    )
    assert variants["lag_step_half"].lag_step_ns == 250_000_000
    assert variants["windows_half"].windows == 1
    assert variants["min_overlap_double"].min_overlap == 10


def test_sensitivity_rows_match_the_primary_estimate_at_baseline() -> None:
    observations = _shifted_pair(500_000_000)
    [primary] = _lead_lag_rows(observations, SETTINGS)
    rows = _lead_lag_sensitivity_rows(observations, SETTINGS)

    assert len(rows) == 9
    assert {row["module"] for row in rows} == {"lead_lag_sensitivity"}
    [baseline] = [row for row in rows if row["variant"] == "baseline"]
    assert baseline["estimated_lead_ns"] == primary["estimated_lead_ns"]
    assert baseline["leader_exchange"] == primary["leader_exchange"]
    assert baseline["null_surrogates"] == 0
    for row in rows:
        assert row["status"] in {"ok", "insufficient_data", "not_identifiable"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"tick_bin_ns": 0},
        {"lag_step_ns": 0},
        {"max_lag_ns": -1},
        {"lag_step_ns": 2_000_000_000},
        {"min_overlap": 0},
        {"windows": 0},
        {"null_surrogates": -1},
        {"null_surrogates": 9},
    ],
)
def test_lead_lag_settings_reject_invalid_values(overrides: dict[str, int]) -> None:
    values = {
        "tick_bin_ns": 1,
        "max_lag_ns": 1_000_000_000,
        "lag_step_ns": 100_000_000,
        "min_overlap": 30,
        "windows": 4,
        "null_surrogates": 20,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        LeadLagSettings(**values).validate()


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

    series = price_series(observations, tick_bin_ns=1)

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

    net_profit = cast("dict[str, Any]", rows[0]["net_profit_by_quote"])
    assert Decimal(net_profit["USD"]) == Decimal("3.33333333333333333")
