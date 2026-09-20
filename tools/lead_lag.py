"""Hayashi-Yoshida lead/lag estimation for offline research.

Pure functions, no I/O. ``research.py`` owns replay, orchestration, and JSONL
output. Everything here is research output, not a product metric.

Estimator (notation: left returns ``r_i`` on intervals ``I_i``, right returns
``s_j`` on intervals ``J_j``, lag ``theta``):

- Contrast ``U(theta) = sum_{i,j} r_i * s_j * 1{I_i overlaps (J_j - theta)}``
  is the Hayashi-Yoshida cross-covariance of the right series shifted earlier by
  ``theta`` (Hayashi & Yoshida 2005, Bernoulli 11(2), 359-379).
- The lag estimate is ``argmax_theta |U(theta)|`` over a symmetric grid
  (Hoffmann, Rosenbaum & Yoshida 2013, Bernoulli 19(2), 426-461).
- The reported correlation is ``U(theta) / sqrt(sum r_i^2 * sum s_j^2)``
  (Huth & Abergel 2014, Journal of Empirical Finance 26, 41-58).

The normalized contrast is consistent for the true correlation but is not
bounded by one in finite samples: each return is multiplied by every partner
return it overlaps, while the denominator counts it once, so a perfectly
correlated pair sampled asynchronously straddles one. The lag estimate does not
depend on the normalization, so an out-of-range value leaves the lag status
alone: the bounded ``correlation`` field becomes ``None``, the raw ratio stays
available as ``normalized_contrast``, and ``correlation_out_of_range`` is set.

No confidence interval is reported. Overlapping asynchronous returns are
dependent, and the reported value is a maximum selected over a lag grid, so a
Fisher-style interval on the overlap count would be wrong on both counts. The
substitutes are window stability (does each window agree on the leader?) and a
shifted-surrogate null (how large does the grid maximum get when the right
series is displaced far outside the grid, so no sub-grid lead/lag can exist?).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal

from arb.replay import ReplayObservation

STATUS_OK = "ok"
STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUS_NOT_IDENTIFIABLE = "not_identifiable"

_TIE_TOLERANCE = 1e-12
_SURROGATE_SHIFT_MULTIPLIER = 5
_NULL_ALPHA = 0.05


@dataclass(frozen=True)
class PriceTick:
    timestamp_ns: int
    price: Decimal


@dataclass(frozen=True)
class ReturnInterval:
    start_ns: int
    end_ns: int
    value: float


@dataclass(frozen=True)
class LagGrid:
    """Symmetric lag grid; ``max_lag_ns`` is the realised edge, not the request."""

    max_lag_ns: int
    step_ns: int
    lags: tuple[int, ...]


@dataclass(frozen=True)
class HYPoint:
    lag_ns: int
    contrast: float
    correlation: float | None
    overlaps: int


@dataclass(frozen=True)
class LeadLagEstimate:
    status: str
    reason: str | None
    grid: LagGrid
    left_return_count: int
    right_return_count: int
    lag_ns: int | None = None
    correlation: float | None = None
    normalized_contrast: float | None = None
    correlation_out_of_range: bool = False
    contrast: float | None = None
    overlaps: int | None = None
    correlation_at_zero_lag: float | None = None
    runner_up_lag_ns: int | None = None
    runner_up_abs_correlation: float | None = None
    tie_count: int = 0


@dataclass(frozen=True)
class WindowStability:
    window_count: int
    window_lead_ns: tuple[int | None, ...]
    window_statuses: tuple[str, ...]
    agreement_fraction: float | None


@dataclass(frozen=True)
class SurrogateNull:
    count: int
    max_abs_correlation_p95: float | None
    exceedance_fraction: float | None
    p_value: float | None


@dataclass(frozen=True)
class LeadLagAnalysis:
    status: str
    reason: str | None
    estimate: LeadLagEstimate
    stability: WindowStability
    null: SurrogateNull


def price_series(
    observations: Iterable[ReplayObservation], tick_bin_ns: int
) -> dict[tuple[str, str], list[PriceTick]]:
    """Keep the last eligible canonical midpoint per tick bin, per (pair, exchange)."""
    latest: dict[tuple[str, str, int], PriceTick] = {}
    for observation in observations:
        if not observation.eligible:
            continue
        if observation.wall_ns <= 0:
            continue
        bid = Decimal(observation.best_bid_price)
        ask = Decimal(observation.best_ask_price)
        if bid <= 0 or ask <= 0:
            continue
        key = (observation.pair, observation.exchange, observation.wall_ns // tick_bin_ns)
        latest[key] = PriceTick(observation.wall_ns, (bid + ask) / Decimal(2))

    series: dict[tuple[str, str], list[PriceTick]] = defaultdict(list)
    for (pair, exchange, _), tick in latest.items():
        series[(pair, exchange)].append(tick)
    for ticks in series.values():
        ticks.sort(key=lambda tick: tick.timestamp_ns)
    return dict(series)


def return_intervals(ticks: list[PriceTick]) -> list[ReturnInterval]:
    intervals: list[ReturnInterval] = []
    for previous, current in zip(ticks, ticks[1:]):
        if (
            current.timestamp_ns <= previous.timestamp_ns
            or previous.price <= 0
            or current.price <= 0
        ):
            continue
        value = math.log(float(current.price / previous.price))
        if math.isfinite(value):
            intervals.append(ReturnInterval(previous.timestamp_ns, current.timestamp_ns, value))
    return intervals


def lag_grid(max_lag_ns: int, lag_step_ns: int) -> LagGrid:
    """Build ``[-K*step, ..., 0, ..., K*step]`` with ``K = max_lag // step``."""
    if lag_step_ns <= 0:
        raise ValueError("lag step must be positive")
    if max_lag_ns < 0:
        raise ValueError("max lag cannot be negative")
    steps = max_lag_ns // lag_step_ns
    lags = tuple(k * lag_step_ns for k in range(-steps, steps + 1))
    return LagGrid(max_lag_ns=steps * lag_step_ns, step_ns=lag_step_ns, lags=lags)


def hy_overlap_products(
    left: list[ReturnInterval], right: list[ReturnInterval], lag_ns: int
) -> tuple[float, int]:
    """Return the Hayashi-Yoshida contrast and the number of overlapping pairs.

    Positive lag shifts the right-hand series earlier, so a positive estimated
    lag means the left-hand venue moved first by that amount. Intervals that
    only touch at an endpoint do not overlap.
    """
    total = 0.0
    overlaps = 0
    right_index = 0
    for left_interval in left:
        while (
            right_index < len(right)
            and right[right_index].end_ns - lag_ns <= left_interval.start_ns
        ):
            right_index += 1
        candidate = right_index
        while candidate < len(right):
            right_interval = right[candidate]
            shifted_start = right_interval.start_ns - lag_ns
            shifted_end = right_interval.end_ns - lag_ns
            if shifted_start >= left_interval.end_ns:
                break
            if min(left_interval.end_ns, shifted_end) > max(left_interval.start_ns, shifted_start):
                total += left_interval.value * right_interval.value
                overlaps += 1
            if shifted_end <= left_interval.end_ns:
                candidate += 1
            else:
                break
    return total, overlaps


def _realized_variance(intervals: list[ReturnInterval]) -> float:
    return sum(interval.value * interval.value for interval in intervals)


def hy_correlation(left: list[ReturnInterval], right: list[ReturnInterval], lag_ns: int) -> HYPoint:
    """Normalized contrast at one lag; ``None`` when it is undefined.

    The correlation is undefined when either series has zero or non-finite
    realized variance, and when no interval pair overlaps at this lag. A zero
    contrast from zero overlaps is absence of evidence, not a correlation of 0.
    """
    contrast, overlaps = hy_overlap_products(left, right, lag_ns)
    denominator = math.sqrt(_realized_variance(left) * _realized_variance(right))
    if overlaps == 0 or denominator == 0 or not math.isfinite(denominator):
        return HYPoint(lag_ns, contrast, None, overlaps)
    return HYPoint(lag_ns, contrast, contrast / denominator, overlaps)


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def estimate_lead_lag(
    left: list[ReturnInterval],
    right: list[ReturnInterval],
    *,
    max_lag_ns: int,
    lag_step_ns: int,
    min_overlap: int,
) -> LeadLagEstimate:
    """Select the lag with the largest absolute correlation over the grid.

    Lags with fewer than ``min_overlap`` overlapping pairs never enter the
    selection. The status ladder is evaluated in order, and a later failure
    keeps the diagnostics computed before it so the row can still be inspected.
    """
    grid = lag_grid(max_lag_ns, lag_step_ns)
    result = LeadLagEstimate(
        STATUS_INSUFFICIENT_DATA,
        "too_few_observations",
        grid=grid,
        left_return_count=len(left),
        right_return_count=len(right),
    )
    if len(left) < 2 or len(right) < 2:
        return result
    left_variance = _realized_variance(left)
    right_variance = _realized_variance(right)
    if (
        left_variance == 0
        or right_variance == 0
        or not math.isfinite(left_variance)
        or not math.isfinite(right_variance)
    ):
        return replace(result, reason="constant_returns")

    points = [hy_correlation(left, right, lag_ns) for lag_ns in grid.lags]
    zero_point = next(point for point in points if point.lag_ns == 0)
    result = replace(result, correlation_at_zero_lag=zero_point.correlation)
    if all(point.overlaps == 0 for point in points):
        return replace(result, reason="no_overlap")
    eligible = [
        point for point in points if point.correlation is not None and point.overlaps >= min_overlap
    ]
    if not eligible:
        return replace(result, reason="too_few_overlaps")

    def magnitude(point: HYPoint) -> float:
        assert point.correlation is not None
        return abs(point.correlation)

    best_abs = max(magnitude(point) for point in eligible)
    tolerance = _TIE_TOLERANCE * max(1.0, best_abs)
    ties = [point for point in eligible if best_abs - magnitude(point) <= tolerance]
    best = min(ties, key=lambda point: (abs(point.lag_ns), point.lag_ns))
    others = [point for point in eligible if point not in ties]
    runner_up = max(others, key=magnitude) if others else None
    result = replace(
        result,
        status=STATUS_NOT_IDENTIFIABLE,
        lag_ns=best.lag_ns,
        correlation=None if best_abs > 1.0 else best.correlation,
        normalized_contrast=best.correlation,
        correlation_out_of_range=best_abs > 1.0,
        contrast=best.contrast,
        overlaps=best.overlaps,
        runner_up_lag_ns=None if runner_up is None else runner_up.lag_ns,
        runner_up_abs_correlation=None if runner_up is None else magnitude(runner_up),
        tie_count=len(ties) - 1,
    )
    if len({_sign(point.lag_ns) for point in ties}) > 1:
        return replace(result, reason="tied_maximum")
    if grid.max_lag_ns > 0 and abs(best.lag_ns) == grid.max_lag_ns:
        return replace(result, reason="maximum_at_grid_edge")
    return replace(result, status=STATUS_OK, reason=None)


def _ticks_between(ticks: list[PriceTick], start_ns: int, end_ns: int) -> list[PriceTick]:
    return [tick for tick in ticks if start_ns <= tick.timestamp_ns < end_ns]


def window_stability(
    left_ticks: list[PriceTick],
    right_ticks: list[PriceTick],
    full_estimate: LeadLagEstimate,
    *,
    windows: int,
    max_lag_ns: int,
    lag_step_ns: int,
    min_overlap: int,
) -> WindowStability:
    """Re-estimate in equal time windows and report leader-sign agreement.

    Agreement is the share of ``ok`` windows whose lag sign matches the full
    sample. It is ``None`` when fewer than two windows produced an estimate or
    the full-sample estimate itself is not ``ok``.
    """
    if windows < 1 or not left_ticks or not right_ticks:
        return WindowStability(0, (), (), None)
    start = min(left_ticks[0].timestamp_ns, right_ticks[0].timestamp_ns)
    end = max(left_ticks[-1].timestamp_ns, right_ticks[-1].timestamp_ns)
    span = end - start
    if span <= 0:
        return WindowStability(0, (), (), None)

    leads: list[int | None] = []
    statuses: list[str] = []
    for index in range(windows):
        window_start = start + span * index // windows
        # The last window is closed on the right so the final tick is not dropped.
        window_end = end + 1 if index == windows - 1 else start + span * (index + 1) // windows
        estimate = estimate_lead_lag(
            return_intervals(_ticks_between(left_ticks, window_start, window_end)),
            return_intervals(_ticks_between(right_ticks, window_start, window_end)),
            max_lag_ns=max_lag_ns,
            lag_step_ns=lag_step_ns,
            min_overlap=min_overlap,
        )
        statuses.append(estimate.status)
        leads.append(estimate.lag_ns if estimate.status == STATUS_OK else None)

    agreement: float | None = None
    resolved = [lead for lead in leads if lead is not None]
    if (
        full_estimate.status == STATUS_OK
        and full_estimate.lag_ns is not None
        and len(resolved) >= 2
    ):
        target = _sign(full_estimate.lag_ns)
        agreement = sum(1 for lead in resolved if _sign(lead) == target) / len(resolved)
    return WindowStability(windows, tuple(leads), tuple(statuses), agreement)


def _grid_max_abs_correlation(
    left: list[ReturnInterval],
    right: list[ReturnInterval],
    grid: LagGrid,
    min_overlap: int,
) -> float | None:
    best: float | None = None
    for lag_ns in grid.lags:
        point = hy_correlation(left, right, lag_ns)
        if point.correlation is None or point.overlaps < min_overlap:
            continue
        magnitude = abs(point.correlation)
        if best is None or magnitude > best:
            best = magnitude
    return best


def _circular_shift(
    intervals: list[ReturnInterval], shift_ns: int, start_ns: int, end_ns: int
) -> list[ReturnInterval]:
    """Shift intervals by ``shift_ns`` inside ``[start_ns, end_ns)``, wrapping around.

    Wrapping keeps every return inside the common span, so the surrogate has
    the same realized variance and the same overlap coverage as the original.
    The one interval that would straddle the wrap point is dropped.
    """
    span = end_ns - start_ns
    shifted: list[ReturnInterval] = []
    for interval in intervals:
        new_start = start_ns + (interval.start_ns + shift_ns - start_ns) % span
        new_end = new_start + (interval.end_ns - interval.start_ns)
        if new_end <= end_ns:
            shifted.append(ReturnInterval(new_start, new_end, interval.value))
    shifted.sort(key=lambda interval: interval.start_ns)
    return shifted


def surrogate_null(
    left: list[ReturnInterval],
    right: list[ReturnInterval],
    observed_abs_correlation: float | None,
    *,
    surrogates: int,
    max_lag_ns: int,
    lag_step_ns: int,
    min_overlap: int,
) -> SurrogateNull:
    """Null distribution of the grid maximum under circularly displaced right series.

    Each surrogate rotates the right intervals by ``+/- base * (1 + 5k)`` for
    ``k = 1..surrogates`` within the common time span, where ``base`` is the
    larger of the max lag and the lag step. The displacement is far outside
    the grid, so any lead/lag inside the grid is destroyed while each series
    keeps its own return structure and total variance. Offsets are
    deterministic; there is no random number generator.

    ``p_value`` is the permutation-style ``(exceedances + 1) / (count + 1)``.
    Surrogates share the same data, so this is a screening diagnostic for
    grid-selection noise, not a calibrated hypothesis test.
    """
    if surrogates <= 0 or not left or not right:
        return SurrogateNull(0, None, None, None)
    grid = lag_grid(max_lag_ns, lag_step_ns)
    base = max(max_lag_ns, lag_step_ns)
    span_start = min(left[0].start_ns, right[0].start_ns)
    span_end = max(left[-1].end_ns, right[-1].end_ns)
    if span_end <= span_start:
        return SurrogateNull(0, None, None, None)
    values: list[float] = []
    for k in range(1, surrogates + 1):
        magnitude = base * (1 + _SURROGATE_SHIFT_MULTIPLIER * k)
        for shift in (magnitude, -magnitude):
            shifted = _circular_shift(right, shift, span_start, span_end)
            value = _grid_max_abs_correlation(left, shifted, grid, min_overlap)
            if value is not None:
                values.append(value)
    if not values:
        return SurrogateNull(0, None, None, None)
    values.sort()
    p95 = values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)]
    exceedance: float | None = None
    p_value: float | None = None
    if observed_abs_correlation is not None:
        exceedances = sum(1 for value in values if value >= observed_abs_correlation)
        exceedance = exceedances / len(values)
        p_value = (exceedances + 1) / (len(values) + 1)
    return SurrogateNull(len(values), p95, exceedance, p_value)


def analyze_pair(
    left_ticks: list[PriceTick],
    right_ticks: list[PriceTick],
    *,
    max_lag_ns: int,
    lag_step_ns: int,
    min_overlap: int,
    windows: int,
    null_surrogates: int,
) -> LeadLagAnalysis:
    """Full-sample estimate plus stability and null diagnostics with a final status."""
    left = return_intervals(left_ticks)
    right = return_intervals(right_ticks)
    estimate = estimate_lead_lag(
        left, right, max_lag_ns=max_lag_ns, lag_step_ns=lag_step_ns, min_overlap=min_overlap
    )
    stability = window_stability(
        left_ticks,
        right_ticks,
        estimate,
        windows=windows,
        max_lag_ns=max_lag_ns,
        lag_step_ns=lag_step_ns,
        min_overlap=min_overlap,
    )
    observed = None if estimate.normalized_contrast is None else abs(estimate.normalized_contrast)
    null = surrogate_null(
        left,
        right,
        observed,
        surrogates=null_surrogates if estimate.status == STATUS_OK else 0,
        max_lag_ns=max_lag_ns,
        lag_step_ns=lag_step_ns,
        min_overlap=min_overlap,
    )
    status, reason = estimate.status, estimate.reason
    if status == STATUS_OK and null.p_value is not None and null.p_value > _NULL_ALPHA:
        status, reason = STATUS_NOT_IDENTIFIABLE, "null_not_rejected"
    return LeadLagAnalysis(status, reason, estimate, stability, null)


def lead_lag_row(
    pair: str,
    left_exchange: str,
    right_exchange: str,
    analysis: LeadLagAnalysis,
    *,
    min_overlap: int,
) -> dict[str, object]:
    """Serialize one venue pair. Positive lead means the named leader moved first."""
    estimate = analysis.estimate
    row: dict[str, object] = {
        "module": "lead_lag",
        "pair": pair,
        "left_exchange": left_exchange,
        "right_exchange": right_exchange,
        "status": analysis.status,
        "reason": analysis.reason,
        "left_return_count": estimate.left_return_count,
        "right_return_count": estimate.right_return_count,
        "grid_max_lag_ns": estimate.grid.max_lag_ns,
        "lag_step_ns": estimate.grid.step_ns,
        "min_overlap": min_overlap,
        "hayashi_yoshida_correlation": estimate.correlation,
        "normalized_contrast": estimate.normalized_contrast,
        "correlation_out_of_range": estimate.correlation_out_of_range,
        "hy_contrast": estimate.contrast,
        "overlap_count": estimate.overlaps,
        "correlation_at_zero_lag": estimate.correlation_at_zero_lag,
        "runner_up_lag_ns": estimate.runner_up_lag_ns,
        "runner_up_abs_correlation": estimate.runner_up_abs_correlation,
        "tie_count": estimate.tie_count,
        "window_count": analysis.stability.window_count,
        "window_lead_ns": list(analysis.stability.window_lead_ns),
        "window_statuses": list(analysis.stability.window_statuses),
        "window_agreement_fraction": analysis.stability.agreement_fraction,
        "null_surrogate_count": analysis.null.count,
        "null_max_abs_correlation_p95": analysis.null.max_abs_correlation_p95,
        "null_exceedance_fraction": analysis.null.exceedance_fraction,
        "null_p_value": analysis.null.p_value,
    }
    if estimate.lag_ns is not None:
        if estimate.lag_ns >= 0:
            leader, follower, lead_ns = left_exchange, right_exchange, estimate.lag_ns
        else:
            leader, follower, lead_ns = right_exchange, left_exchange, -estimate.lag_ns
        row.update(
            leader_exchange=leader,
            follower_exchange=follower,
            estimated_lead_ns=lead_ns,
            estimated_lead_ms=lead_ns / 1_000_000,
        )
    return row
