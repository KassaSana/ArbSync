from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest
from arb.replay import ReplayObservation
from hypothesis import given, settings
from hypothesis import strategies as st
from lead_lag import (
    LeadLagAnalysis,
    PriceTick,
    ReturnInterval,
    analyze_pair,
    estimate_lead_lag,
    hy_correlation,
    hy_overlap_products,
    lag_grid,
    lead_lag_row,
    price_series,
    return_intervals,
    surrogate_null,
    window_stability,
)

LATENT_STEP_NS = 10_000_000
SECOND = 1_000_000_000
PAIR = "BTC-USD"


def _observation(exchange: str, wall_ns: int, mid: float) -> ReplayObservation:
    return ReplayObservation(
        exchange=exchange,
        pair=PAIR,
        best_bid_price=str(Decimal(f"{mid - 0.5:.6f}")),
        best_ask_price=str(Decimal(f"{mid + 0.5:.6f}")),
        wall_ns=wall_ns,
        mono_ns=wall_ns,
        sequence=wall_ns,
        eligible=True,
    )


def _planted_observations(
    *,
    seed: int,
    lag_ns: int,
    duration_ns: int = 60 * SECOND,
    left_rate_hz: float = 5.0,
    right_rate_hz: float = 5.0,
    noise: float = 0.0,
    dropout: float = 0.0,
    independent: bool = False,
) -> list[ReplayObservation]:
    """Two venues observing one latent random walk at their own Poisson arrival times.

    The right venue sees the latent path ``lag_ns`` later (positive lag means the
    left venue leads) plus optional idiosyncratic noise. ``independent`` gives the
    right venue its own unrelated walk for null simulations.
    """
    rng = random.Random(seed)
    steps = (duration_ns + abs(lag_ns)) // LATENT_STEP_NS + 2

    def walk() -> list[float]:
        path = [0.0]
        for _ in range(steps):
            path.append(path[-1] + rng.gauss(0.0, 0.001))
        return path

    left_path = walk()
    right_path = walk() if independent else left_path

    def arrivals(rate_hz: float) -> list[int]:
        stamps: list[int] = []
        now = 0
        while True:
            now += int(rng.expovariate(rate_hz) * SECOND)
            if now >= duration_ns:
                return stamps
            if rng.random() >= dropout:
                stamps.append(now)

    rows: list[ReplayObservation] = []
    for stamp in arrivals(left_rate_hz):
        rows.append(
            _observation("left", stamp, 100.0 * math.exp(left_path[stamp // LATENT_STEP_NS]))
        )
    for stamp in arrivals(right_rate_hz):
        index = max(0, (stamp - lag_ns) // LATENT_STEP_NS)
        value = right_path[index] + rng.gauss(0.0, noise)
        rows.append(_observation("right", stamp, 100.0 * math.exp(value)))
    return rows


def _analyze(
    observations: list[ReplayObservation],
    *,
    tick_bin_ns: int = 50_000_000,
    max_lag_ns: int = SECOND,
    lag_step_ns: int = 100_000_000,
    min_overlap: int = 30,
    windows: int = 4,
    null_surrogates: int = 10,
) -> LeadLagAnalysis:
    series = price_series(observations, tick_bin_ns)
    return analyze_pair(
        series.get((PAIR, "left"), []),
        series.get((PAIR, "right"), []),
        max_lag_ns=max_lag_ns,
        lag_step_ns=lag_step_ns,
        min_overlap=min_overlap,
        windows=windows,
        null_surrogates=null_surrogates,
    )


def _assert_recovers(analysis: LeadLagAnalysis, lag_ns: int, step_ns: int = 100_000_000) -> None:
    estimate = analysis.estimate
    assert analysis.status == "ok", (analysis.status, analysis.reason)
    assert estimate.lag_ns is not None
    assert abs(estimate.lag_ns - lag_ns) <= step_ns
    if estimate.correlation is not None:
        assert -1.0 <= estimate.correlation <= 1.0
    assert analysis.null.p_value is not None and analysis.null.p_value <= 0.05


# --- planted lags -----------------------------------------------------------


def test_planted_positive_lag_names_the_left_venue_as_leader() -> None:
    analysis = _analyze(_planted_observations(seed=1, lag_ns=300_000_000))

    _assert_recovers(analysis, 300_000_000)
    row = lead_lag_row(PAIR, "left", "right", analysis, min_overlap=30)
    assert row["leader_exchange"] == "left"
    assert row["follower_exchange"] == "right"
    assert row["estimated_lead_ns"] == analysis.estimate.lag_ns


def test_planted_negative_lag_names_the_right_venue_as_leader() -> None:
    analysis = _analyze(_planted_observations(seed=2, lag_ns=-300_000_000))

    _assert_recovers(analysis, -300_000_000)
    row = lead_lag_row(PAIR, "left", "right", analysis, min_overlap=30)
    assert row["leader_exchange"] == "right"
    assert row["estimated_lead_ns"] == -analysis.estimate.lag_ns


def test_planted_zero_lag_reports_no_lead() -> None:
    analysis = _analyze(_planted_observations(seed=3, lag_ns=0))

    _assert_recovers(analysis, 0)


def test_missing_ticks_and_irregular_sampling_still_recover_the_lag() -> None:
    analysis = _analyze(_planted_observations(seed=4, lag_ns=300_000_000, dropout=0.3))

    _assert_recovers(analysis, 300_000_000)


def test_unequal_activity_recovers_the_lag_within_bounds() -> None:
    analysis = _analyze(
        _planted_observations(seed=5, lag_ns=300_000_000, left_rate_hz=1.0, right_rate_hz=8.0)
    )

    _assert_recovers(analysis, 300_000_000)


def test_idiosyncratic_noise_lowers_correlation_but_keeps_the_lag() -> None:
    analysis = _analyze(_planted_observations(seed=6, lag_ns=300_000_000, noise=0.0005))

    _assert_recovers(analysis, 300_000_000)
    assert analysis.estimate.correlation is not None
    assert analysis.estimate.correlation < 0.95


def test_planted_lag_shows_stable_windows_and_clears_the_null() -> None:
    analysis = _analyze(_planted_observations(seed=1, lag_ns=300_000_000))

    assert analysis.stability.window_count == 4
    assert analysis.stability.agreement_fraction == 1.0
    assert analysis.null.count == 20
    assert analysis.null.max_abs_correlation_p95 is not None
    assert abs(analysis.estimate.normalized_contrast or 0.0) > analysis.null.max_abs_correlation_p95
    assert analysis.null.exceedance_fraction == 0.0


# --- null and non-identifiable outcomes --------------------------------------


def test_independent_walks_do_not_produce_an_identified_lead() -> None:
    # Seed chosen from a 30-seed calibration in which 29 nulls were retained.
    analysis = _analyze(_planted_observations(seed=103, lag_ns=0, independent=True))

    assert analysis.status == "not_identifiable"
    assert analysis.reason == "null_not_rejected"
    assert analysis.estimate.status == "ok"
    assert analysis.null.p_value is not None and analysis.null.p_value > 0.05
    assert (
        analysis.stability.agreement_fraction is None or analysis.stability.agreement_fraction < 1.0
    )


def test_lag_at_the_grid_edge_is_not_identifiable() -> None:
    analysis = _analyze(_planted_observations(seed=9, lag_ns=SECOND))

    assert analysis.status == "not_identifiable"
    assert analysis.reason == "maximum_at_grid_edge"
    assert analysis.estimate.lag_ns == SECOND
    assert analysis.null.count == 0


def test_out_of_range_correlation_keeps_the_lag_and_exposes_the_raw_ratio() -> None:
    # One long left return overlapping ten short right returns of the same sign:
    # the contrast counts the left return ten times, the denominator once.
    left = [ReturnInterval(0, 100, 1.0), ReturnInterval(100, 200, -1.0)]
    right = [ReturnInterval(i * 10, (i + 1) * 10, 0.1) for i in range(10)] + [
        ReturnInterval(100 + i * 10, 110 + i * 10, -0.1) for i in range(10)
    ]

    estimate = estimate_lead_lag(left, right, max_lag_ns=20, lag_step_ns=10, min_overlap=1)

    assert estimate.status == "ok"
    assert estimate.lag_ns == 0
    assert estimate.correlation is None
    assert estimate.correlation_out_of_range is True
    assert estimate.normalized_contrast == pytest.approx(math.sqrt(10))


def test_tied_maximum_with_opposite_signs_is_not_identifiable() -> None:
    # Symmetric returns: shifting right earlier or later by one step is indistinguishable.
    left = [ReturnInterval(10, 20, 1.0), ReturnInterval(20, 30, 1.0)]
    right = [ReturnInterval(0, 10, 1.0), ReturnInterval(30, 40, 1.0)]

    estimate = estimate_lead_lag(left, right, max_lag_ns=20, lag_step_ns=10, min_overlap=1)

    assert estimate.status == "not_identifiable"
    assert estimate.reason == "tied_maximum"
    assert estimate.tie_count >= 1


# --- insufficient data --------------------------------------------------------


def test_constant_series_is_insufficient_data() -> None:
    left = [ReturnInterval(i * 10, (i + 1) * 10, 0.0) for i in range(10)]
    right = [ReturnInterval(i * 10 + 5, (i + 1) * 10 + 5, 0.5) for i in range(10)]

    estimate = estimate_lead_lag(left, right, max_lag_ns=20, lag_step_ns=10, min_overlap=1)

    assert (estimate.status, estimate.reason) == ("insufficient_data", "constant_returns")
    assert estimate.lag_ns is None


def test_disjoint_time_ranges_are_no_overlap() -> None:
    left = [ReturnInterval(i * 10, (i + 1) * 10, 0.5) for i in range(5)]
    right = [ReturnInterval(1000 + i * 10, 1010 + i * 10, 0.5) for i in range(5)]

    estimate = estimate_lead_lag(left, right, max_lag_ns=20, lag_step_ns=10, min_overlap=1)

    assert (estimate.status, estimate.reason) == ("insufficient_data", "no_overlap")
    assert hy_correlation(left, right, 0).correlation is None


def test_too_few_observations_and_too_few_overlaps_are_distinct() -> None:
    left = [ReturnInterval(0, 10, 0.5)]
    right = [ReturnInterval(0, 10, 0.5)]
    estimate = estimate_lead_lag(left, right, max_lag_ns=0, lag_step_ns=10, min_overlap=1)
    assert (estimate.status, estimate.reason) == ("insufficient_data", "too_few_observations")

    left = [ReturnInterval(i * 10, (i + 1) * 10, 0.5) for i in range(5)]
    right = [ReturnInterval(i * 10 + 5, (i + 1) * 10 + 5, 0.5) for i in range(5)]
    estimate = estimate_lead_lag(left, right, max_lag_ns=0, lag_step_ns=10, min_overlap=100)
    assert (estimate.status, estimate.reason) == ("insufficient_data", "too_few_overlaps")
    assert estimate.correlation_at_zero_lag is not None


def test_zero_overlap_lag_is_never_a_correlation_of_zero() -> None:
    left = [ReturnInterval(0, 10, 0.5), ReturnInterval(10, 20, -0.5)]
    right = [ReturnInterval(100, 110, 0.5), ReturnInterval(110, 120, -0.5)]

    point = hy_correlation(left, right, 0)

    assert point.overlaps == 0
    assert point.correlation is None


# --- grid, sweep, and diagnostics -------------------------------------------


def test_lag_grid_is_symmetric_and_records_the_realised_edge() -> None:
    grid = lag_grid(1000, 300)

    assert grid.lags == (-900, -600, -300, 0, 300, 600, 900)
    assert grid.max_lag_ns == 900
    assert lag_grid(0, 10).lags == (0,)


def _brute_force(
    left: list[ReturnInterval], right: list[ReturnInterval], lag: int
) -> tuple[float, int]:
    total = 0.0
    count = 0
    for a in left:
        for b in right:
            if min(a.end_ns, b.end_ns - lag) > max(a.start_ns, b.start_ns - lag):
                total += a.value * b.value
                count += 1
    return total, count


def _interval_lists() -> st.SearchStrategy[list[ReturnInterval]]:
    return st.lists(
        st.tuples(st.integers(1, 15), st.floats(-2, 2, allow_nan=False)), min_size=0, max_size=12
    ).map(
        lambda gaps: [
            ReturnInterval(sum(g for g, _ in gaps[:i]), sum(g for g, _ in gaps[: i + 1]), v)
            for i, (_, v) in enumerate(gaps)
        ]
    )


@settings(max_examples=300, deadline=None)
@given(_interval_lists(), _interval_lists(), st.integers(-40, 40))
def test_overlap_sweep_matches_brute_force(
    left: list[ReturnInterval], right: list[ReturnInterval], lag: int
) -> None:
    total, count = hy_overlap_products(left, right, lag)
    expected_total, expected_count = _brute_force(left, right, lag)

    assert count == expected_count
    assert total == pytest.approx(expected_total)


def test_surrogate_null_preserves_variance_through_circular_shifts() -> None:
    observations = _planted_observations(seed=11, lag_ns=200_000_000, duration_ns=20 * SECOND)
    series = price_series(observations, 50_000_000)
    left = return_intervals(series[(PAIR, "left")])
    right = return_intervals(series[(PAIR, "right")])

    null = surrogate_null(
        left, right, 0.0, surrogates=3, max_lag_ns=SECOND, lag_step_ns=100_000_000, min_overlap=5
    )

    assert null.count == 6
    assert null.exceedance_fraction == 1.0
    assert null.p_value == pytest.approx(1.0)
    assert (
        surrogate_null(
            left,
            right,
            0.5,
            surrogates=0,
            max_lag_ns=SECOND,
            lag_step_ns=100_000_000,
            min_overlap=5,
        ).count
        == 0
    )


def test_window_stability_reports_per_window_lags() -> None:
    observations = _planted_observations(seed=1, lag_ns=300_000_000)
    series = price_series(observations, 50_000_000)
    left_ticks = series[(PAIR, "left")]
    right_ticks = series[(PAIR, "right")]
    full = estimate_lead_lag(
        return_intervals(left_ticks),
        return_intervals(right_ticks),
        max_lag_ns=SECOND,
        lag_step_ns=100_000_000,
        min_overlap=30,
    )

    stability = window_stability(
        left_ticks,
        right_ticks,
        full,
        windows=3,
        max_lag_ns=SECOND,
        lag_step_ns=100_000_000,
        min_overlap=30,
    )

    assert stability.window_count == 3
    assert len(stability.window_lead_ns) == 3
    assert all(lead is None or lead > 0 for lead in stability.window_lead_ns)
    assert stability.agreement_fraction == 1.0
    empty = window_stability(
        [],
        [PriceTick(1, Decimal(1))],
        full,
        windows=3,
        max_lag_ns=SECOND,
        lag_step_ns=100_000_000,
        min_overlap=1,
    )
    assert empty.window_count == 0 and empty.agreement_fraction is None


def test_row_never_carries_a_correlation_outside_unit_interval() -> None:
    for seed in range(1, 6):
        analysis = _analyze(_planted_observations(seed=seed, lag_ns=100_000_000 * seed))
        row = lead_lag_row(PAIR, "left", "right", analysis, min_overlap=30)
        correlation = row["hayashi_yoshida_correlation"]
        assert correlation is None or -1.0 <= float(str(correlation)) <= 1.0
        assert row["status"] in {"ok", "insufficient_data", "not_identifiable"}
        assert "correlation_ci_95_low" not in row
