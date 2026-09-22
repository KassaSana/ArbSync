from __future__ import annotations

import asyncio
import gzip
import json
import math
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from arb.config import load_config
from arb.replay import ReplayObservation
from lead_lag import price_series
from research import (
    LeadLagSettings,
    _lead_lag_rows,
    _lead_lag_sensitivity_rows,
    _sensitivity_variants,
    _survival_rows,
    run_research,
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


FIXTURE = Path("server/tests/fixtures/captured/three_venue_150s_20260917.jsonl.gz")


def _fixture_slice(destination: Path, fraction: int = 12) -> Path:
    """The leading slice of the committed capture: enough books to price, quick to replay.

    The footer is rewritten so the slice validates as a clean, lossless capture.
    """
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    header, frames, footer = lines[0], lines[1:-1], json.loads(lines[-1])
    kept = frames[: len(frames) // fraction]
    by_kind: dict[str, int] = {}
    for line in kept:
        kind = json.loads(line)["kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
    footer["frame_count"] = len(kept)
    footer["frame_counts"] = {
        "attempted": len(kept),
        "accepted": len(kept),
        "flushed": len(kept),
        "attempted_by_kind": by_kind,
        "accepted_by_kind": by_kind,
        "dropped": {},
    }
    body = "\n".join([header, *kept, json.dumps(footer)]) + "\n"
    destination.write_text(body, encoding="utf-8", newline="\n")
    return destination


def _net_research(tmp_path: Path, name: str, **overrides: Any) -> dict[str, object]:
    config = load_config("config.toml")
    config = replace(config, detector=replace(config.detector, threshold_pct=0.0))
    sliced = _fixture_slice(tmp_path / "slice.jsonl")
    return asyncio.run(
        run_research(
            sliced,
            config,
            tmp_path / name,
            tick_bin_ns=100_000_000,
            max_lag_ns=1_000_000_000,
            lag_step_ns=100_000_000,
            null_surrogates=0,
            sensitivity=False,
            **overrides,
        )
    )


def test_net_intervals_replay_deterministically_and_join_theoretical_episodes(
    tmp_path: Path,
) -> None:
    first = _net_research(tmp_path, "first", write_net_signal=True)
    second = _net_research(tmp_path, "second")
    assert first["replay_digest"] == second["replay_digest"]
    for name in (
        "net_intervals.jsonl",
        "net_interval_sensitivity.jsonl",
        "venue_fill_rates.jsonl",
        "venue_fill_rate_minutes.jsonl",
    ):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()

    metadata = cast(dict[str, Any], first["measurement"])["net_intervals"]
    assert metadata["enabled"] is True
    assert metadata["threshold_pct"] == "0"
    assert metadata["delay_ns"] == 0
    assert metadata["delay_grid_ms"] == [50, 250, 1000]
    assert metadata["taker_fees_pct"] == {"binance": "0.6", "coinbase": "0.6", "gemini": "0.4"}
    assert metadata["notionals"] == ["100", "1000", "10000", "50000"]
    assert metadata["sensitivity_variants"] == "disabled"
    assert metadata["signal_rows"] > 0
    assert metadata["signal_bytes_estimate"] > metadata["signal_rows"]
    counts = cast(dict[str, int], first["dataset_counts"])
    assert counts["net_signal"] == metadata["signal_rows"]
    assert counts["net_interval_sensitivity"] == 0

    signal = [
        json.loads(line)
        for line in (tmp_path / "first" / "net_signal.jsonl").read_text().splitlines()
    ]
    priced = [row for row in signal if row["state"] == "priced"]
    assert priced, "real three-venue books must price some routes"
    assert all(
        row["buy_cost_quote"] == row["notional"] and row["sell_proceeds_quote"] is not None
        for row in priced
    )
    assert first["version"] == 5

    # ARB-042: fill-rate rows carry their sampling provenance, cover the whole
    # configured roster, and reduce from the file-backed minute buckets.
    fill = cast(dict[str, Any], first["measurement"])["fill_rates"]
    assert fill["samples"] > 0 and fill["missed_samples"] == 0
    assert fill["session"]["config_fingerprint"]
    roster = {tuple(book) for book in fill["config"]["roster"]}
    fill_rows = [
        json.loads(line)
        for line in (tmp_path / "first" / "venue_fill_rates.jsonl").read_text().splitlines()
    ]
    assert {(row["exchange"], row["pair"]) for row in fill_rows} >= roster
    assert all(
        row["samples"] == fill["samples"]
        and row["samples"]
        == row["filled"] + row["insufficient_depth"] + sum(row["ineligible"].values())
        for row in fill_rows
    )
    minutes = [
        json.loads(line)
        for line in (tmp_path / "first" / "venue_fill_rate_minutes.jsonl").read_text().splitlines()
    ]
    assert counts["fill_rate_minutes"] == len(minutes) > 0
    assert sum(minute["samples"] for minute in minutes) == fill["samples"]

    intervals = [
        json.loads(line)
        for line in (tmp_path / "first" / "net_intervals.jsonl").read_text().splitlines()
    ]
    for row in intervals:
        assert row["module"] == "net_interval"
        assert row["fee_mode"] == "configured"
        assert row["theoretical_open_at_start"] in (True, False)
        assert Decimal(row["theoretical_coverage_fraction"]) <= 1
        assert Decimal(row["peak_net_spread_pct"]) > 0


def test_net_intervals_can_be_disabled(tmp_path: Path) -> None:
    metadata = _net_research(tmp_path, "off", net_enabled=False)
    measurement = cast(dict[str, Any], metadata["measurement"])["net_intervals"]
    assert measurement == {"enabled": False}
    counts = cast(dict[str, int], metadata["dataset_counts"])
    assert counts["net_intervals"] == 0
    assert "net_signal" not in counts
