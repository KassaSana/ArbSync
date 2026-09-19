"""Run offline market-structure research over an ArbSync capture.

The tool replays a capture through the production adapters, trusted books,
episode detector, and depth sampler. It writes research datasets as JSONL files
and never opens or writes SQLite. The lead/lag module estimates asynchronous
return correlation with the Hayashi-Yoshida overlap rule; it is intentionally
research output, not a product metric or execution signal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any, cast

from arb.adapters import ADAPTER_TYPES
from arb.capture import CaptureFrame, CaptureHeader, read_capture
from arb.config import AppConfig, load_config
from arb.detector import ArbitrageDetector
from arb.orderbook import OrderBookManager
from arb.pricing import DepthSampler
from arb.replay import ReplayObservation, ReplayReport, replay_frames


@dataclass(frozen=True)
class PriceTick:
    timestamp_ns: int
    price: Decimal


@dataclass(frozen=True)
class ReturnInterval:
    start_ns: int
    end_ns: int
    value: float


def _adapter_depths(header: CaptureHeader) -> dict[str, int | None]:
    adapter_types = {adapter_type.name: adapter_type for adapter_type in ADAPTER_TYPES}
    depths: dict[str, int | None] = {}
    for exchange, symbols in header.exchanges.items():
        adapter_type = adapter_types.get(exchange)
        if adapter_type is None:
            raise ValueError(f"capture names unknown exchange {exchange!r}")
        depths[exchange] = adapter_type(list(symbols)).subscribed_depth_levels
    return depths


async def replay_for_research(
    header: CaptureHeader,
    frames: list[CaptureFrame],
    config: AppConfig,
) -> tuple[ReplayReport, DepthSampler]:
    """Replay one capture with the configured depth and fee assumptions."""
    book_manager = OrderBookManager(max_age_seconds=config.order_books.max_age_seconds)
    exchanges = set(header.exchanges)
    fees = {
        exchange: fee for exchange, fee in config.fees.taker_pct.items() if exchange in exchanges
    }
    sampler = DepthSampler(
        book_manager,
        config.pricing.notionals,
        _adapter_depths(header),
        fees,
        interval_seconds=config.pricing.sample_interval_seconds,
    )
    detector = ArbitrageDetector(
        threshold_pct=Decimal(str(config.detector.threshold_pct)),
        ledger_factory=sampler.ledgers_for_route,
    )
    report = await replay_frames(
        header,
        frames,
        threshold_pct=Decimal(str(config.detector.threshold_pct)),
        max_age_seconds=config.order_books.max_age_seconds,
        book_manager=book_manager,
        detector=detector,
        depth_sampler=sampler,
    )
    if detector.open_episodes():
        # A capture ending with a standing opportunity needs a deterministic
        # boundary for lifetime research, just like ordinary replay reports.
        last_wall_ns = max(frame.wall_ns for frame in frames)
        last_mono_ns = max(frame.mono_ns for frame in frames)
        report.opportunities.extend(detector.close_all(last_wall_ns, last_mono_ns))
    return report, sampler


def _canonical_episode_rows(report: ReplayReport) -> list[dict[str, object]]:
    """Collapse an open/close event pair into one research row."""
    rows: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for episode in report.opportunities:
        payload = episode.as_payload()
        key = (
            str(payload["start_ns"]),
            str(payload["pair"]),
            str(payload["buy_exchange"]),
            str(payload["sell_exchange"]),
        )
        current = rows.get(key)
        if (
            current is None
            or payload["end_ns"] is not None
            or payload["close_reason"] == "orphaned"
        ):
            rows[key] = payload
    return [rows[key] for key in sorted(rows, key=lambda item: (int(item[0]), item[1:]))]


def _survival_rows(episodes: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    totals: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        quote_asset = str(episode["quote_asset"])
        ledgers = episode.get("pricing_ledgers", [])
        if not isinstance(ledgers, list):
            continue
        for ledger in ledgers:
            if not isinstance(ledger, dict) or "notional" not in ledger:
                continue
            notional = str(ledger["notional"])
            entry = totals.setdefault(
                notional,
                {
                    "notional": notional,
                    "observations": 0,
                    "priced": 0,
                    "insufficient_depth": 0,
                    "survivors": 0,
                    "net_profit_by_quote": {},
                },
            )
            entry["observations"] += 1
            net_literal = ledger.get("net_executable_spread_pct")
            insufficient = bool(ledger.get("insufficient_depth")) or net_literal is None
            if insufficient:
                entry["insufficient_depth"] += 1
                continue
            entry["priced"] += 1
            net = Decimal(str(net_literal))
            if net <= 0:
                continue
            entry["survivors"] += 1
            profits = entry["net_profit_by_quote"]
            assert isinstance(profits, dict)
            profits[quote_asset] = str(
                Decimal(str(profits.get(quote_asset, "0"))) + Decimal(notional) * net / Decimal(100)
            )

    rows: list[dict[str, object]] = []
    for notional in sorted(totals, key=Decimal):
        entry = totals[notional]
        observations = int(entry["observations"])
        priced = int(entry["priced"])
        survivors = int(entry["survivors"])
        rows.append(
            {
                "module": "survival_by_notional",
                **entry,
                "fee_survival_rate": survivors / priced if priced else None,
                "executable_size_survival_rate": survivors / observations if observations else None,
            }
        )
    return rows


def _price_series(
    observations: Iterable[ReplayObservation], tick_bin_ns: int
) -> dict[tuple[str, str], list[PriceTick]]:
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


def _return_intervals(ticks: list[PriceTick]) -> list[ReturnInterval]:
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


def _hy_overlap_products(
    left: list[ReturnInterval], right: list[ReturnInterval], lag_ns: int
) -> tuple[float, int]:
    """Return Hayashi-Yoshida overlap products and the number of overlaps.

    Positive lag shifts the right-hand series earlier, so a positive estimated
    lag means the left-hand venue moved first by that amount.
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


def _hy_correlation(
    left: list[ReturnInterval], right: list[ReturnInterval], lag_ns: int
) -> tuple[float | None, int]:
    covariance, overlaps = _hy_overlap_products(left, right, lag_ns)
    left_variance = sum(interval.value * interval.value for interval in left)
    right_variance = sum(interval.value * interval.value for interval in right)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator == 0 or not math.isfinite(denominator):
        return None, overlaps
    return covariance / denominator, overlaps


def _correlation_interval(correlation: float, overlaps: int) -> tuple[float, float]:
    """Approximate a 95% Fisher confidence interval.

    Overlapping asynchronous returns are not independent, so this interval is
    deliberately labelled approximate in the research documentation.
    """
    if overlaps <= 3:
        return -1.0, 1.0
    bounded = max(-0.999999, min(0.999999, correlation))
    margin = 1.96 / math.sqrt(overlaps - 3)
    lower = -1.0 if correlation <= -1.0 else math.tanh(math.atanh(bounded) - margin)
    upper = 1.0 if correlation >= 1.0 else math.tanh(math.atanh(bounded) + margin)
    return max(-1.0, lower), min(1.0, upper)


def _lead_lag_rows(
    observations: Iterable[ReplayObservation],
    *,
    tick_bin_ns: int,
    max_lag_ns: int,
    lag_step_ns: int,
) -> list[dict[str, object]]:
    series = _price_series(observations, tick_bin_ns)
    grouped: dict[str, list[str]] = defaultdict(list)
    for pair, exchange in series:
        grouped[pair].append(exchange)

    rows: list[dict[str, object]] = []
    for pair in sorted(grouped):
        exchanges = sorted(set(grouped[pair]))
        for left_exchange, right_exchange in combinations(exchanges, 2):
            left_returns = _return_intervals(series[(pair, left_exchange)])
            right_returns = _return_intervals(series[(pair, right_exchange)])
            estimates: list[tuple[float, int, int]] = []
            for lag_ns in range(-max_lag_ns, max_lag_ns + 1, lag_step_ns):
                correlation, overlaps = _hy_correlation(left_returns, right_returns, lag_ns)
                if correlation is not None:
                    estimates.append((correlation, lag_ns, overlaps))
            if not estimates:
                rows.append(
                    {
                        "module": "lead_lag",
                        "pair": pair,
                        "left_exchange": left_exchange,
                        "right_exchange": right_exchange,
                        "status": "insufficient_data",
                        "reason": "no non-constant overlapping return series",
                    }
                )
                continue
            correlation, lag_ns, overlaps = max(
                estimates, key=lambda item: (item[0], -abs(item[1]))
            )
            low, high = _correlation_interval(correlation, overlaps)
            if lag_ns >= 0:
                leader, follower, lead_ns = left_exchange, right_exchange, lag_ns
            else:
                leader, follower, lead_ns = right_exchange, left_exchange, -lag_ns
            rows.append(
                {
                    "module": "lead_lag",
                    "pair": pair,
                    "leader_exchange": leader,
                    "follower_exchange": follower,
                    "estimated_lead_ns": lead_ns,
                    "estimated_lead_ms": lead_ns / 1_000_000,
                    "hayashi_yoshida_correlation": correlation,
                    "correlation_ci_95_low": low,
                    "correlation_ci_95_high": high,
                    "overlap_count": overlaps,
                    "status": "ok",
                }
            )
    return rows


def analyze_capture(
    report: ReplayReport,
    sampler: DepthSampler,
    *,
    tick_bin_ns: int = 250_000_000,
    max_lag_ns: int = 1_000_000_000,
    lag_step_ns: int = 100_000_000,
) -> dict[str, object]:
    """Build all file-backed research datasets from one replay report."""
    if tick_bin_ns <= 0 or max_lag_ns < 0 or lag_step_ns <= 0:
        raise ValueError("research time buckets and lag steps must be positive")
    episodes = _canonical_episode_rows(report)
    survival = _survival_rows(episodes)
    fill_rates = [{"module": "venue_fill_rate", **row} for row in sampler.tracker.rows()]
    lead_lag = _lead_lag_rows(
        report.observations,
        tick_bin_ns=tick_bin_ns,
        max_lag_ns=max_lag_ns,
        lag_step_ns=lag_step_ns,
    )
    return {
        "episodes": episodes,
        "survival": survival,
        "fill_rates": fill_rates,
        "lead_lag": lead_lag,
        "measurement": {
            "price_series_source": "canonical_post_apply_observations",
            "replay_observation_version": 1,
            "estimator": "Hayashi-Yoshida asynchronous return correlation",
            "tick_bin_ns": tick_bin_ns,
            "lag_step_ns": lag_step_ns,
            "max_lag_ns": max_lag_ns,
            "measurement_floor_ns": max(tick_bin_ns, lag_step_ns),
            "confidence_interval": "approximate 95% Fisher transform; overlap dependence remains",
            "clock_skew_warning": "local receive timestamps include network path and exchange clock effects",
        },
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            count += 1
    return count


async def run_research(
    capture: Path,
    config: AppConfig,
    output_dir: Path,
    *,
    tick_bin_ns: int,
    max_lag_ns: int,
    lag_step_ns: int,
    allow_lossy: bool = False,
) -> dict[str, object]:
    header, frames = read_capture(capture, allow_lossy=allow_lossy)
    replay_report, sampler = await replay_for_research(header, frames, config)
    datasets = analyze_capture(
        replay_report,
        sampler,
        tick_bin_ns=tick_bin_ns,
        max_lag_ns=max_lag_ns,
        lag_step_ns=lag_step_ns,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = {
        "episodes": _write_jsonl(
            output_dir / "episodes.jsonl",
            cast(list[dict[str, object]], datasets["episodes"]),
        ),
        "survival": _write_jsonl(
            output_dir / "survival_by_notional.jsonl",
            cast(list[dict[str, object]], datasets["survival"]),
        ),
        "fill_rates": _write_jsonl(
            output_dir / "venue_fill_rates.jsonl",
            cast(list[dict[str, object]], datasets["fill_rates"]),
        ),
        "lead_lag": _write_jsonl(
            output_dir / "lead_lag.jsonl",
            cast(list[dict[str, object]], datasets["lead_lag"]),
        ),
    }
    metadata: dict[str, object] = {
        "format": "arbsync-research",
        "version": 1,
        "capture": str(capture),
        "replay_digest": replay_report.digest,
        "snapshots_consumed": replay_report.snapshots_consumed,
        "transition_count": len(replay_report.transitions),
        "observation_count": len(replay_report.observations),
        "capture_integrity": header.integrity,
        "capture_provenance": header.provenance,
        "allow_lossy": allow_lossy,
        "dataset_counts": counts,
        "measurement": datasets["measurement"],
    }
    (output_dir / "report.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return metadata


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", type=Path, required=True, help="capture JSONL or JSONL.gz")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output-dir", type=Path, default=Path("var/research"))
    parser.add_argument("--tick-bin-ms", type=float, default=250.0)
    parser.add_argument("--max-lag-ms", type=float, default=1000.0)
    parser.add_argument("--lag-step-ms", type=float, default=100.0)
    parser.add_argument(
        "--allow-lossy",
        action="store_true",
        help="analyze captures that declare dropped frames and record that override",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    values = (args.tick_bin_ms, args.max_lag_ms, args.lag_step_ms)
    if args.tick_bin_ms <= 0 or args.max_lag_ms < 0 or args.lag_step_ms <= 0:
        raise SystemExit(
            "tick-bin-ms and lag-step-ms must be positive; max-lag-ms cannot be negative"
        )
    metadata = asyncio.run(
        run_research(
            args.capture,
            load_config(args.config),
            args.output_dir,
            tick_bin_ns=int(values[0] * 1_000_000),
            max_lag_ns=int(values[1] * 1_000_000),
            lag_step_ns=int(values[2] * 1_000_000),
            allow_lossy=args.allow_lossy,
        )
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
