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
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
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
from lead_lag import analyze_pair, lag_grid, lead_lag_row, price_series

ESTIMATOR_REFERENCES = (
    "Hayashi & Yoshida (2005) Bernoulli 11(2) overlap covariance; "
    "Hoffmann, Rosenbaum & Yoshida (2013) Bernoulli 19(2) argmax |U(theta)| lag selection; "
    "Huth & Abergel (2014) J. Empirical Finance 26 realized-variance normalization"
)


MIN_NULL_SURROGATES = 10


@dataclass(frozen=True)
class LeadLagSettings:
    tick_bin_ns: int
    max_lag_ns: int
    lag_step_ns: int
    min_overlap: int
    windows: int
    null_surrogates: int

    def validate(self) -> None:
        if self.tick_bin_ns <= 0 or self.max_lag_ns < 0 or self.lag_step_ns <= 0:
            raise ValueError("research time buckets and lag steps must be positive")
        if self.max_lag_ns > 0 and self.lag_step_ns > self.max_lag_ns:
            raise ValueError("lag step cannot exceed the maximum lag")
        if self.min_overlap < 1 or self.windows < 1 or self.null_surrogates < 0:
            raise ValueError(
                "min overlap and windows must be positive; null surrogates cannot be negative"
            )
        if 0 < self.null_surrogates < MIN_NULL_SURROGATES:
            # Two shifts per surrogate; (0 + 1) / (2n + 1) must be able to reach 0.05.
            raise ValueError(
                f"null surrogates must be 0 or at least {MIN_NULL_SURROGATES} so the "
                "permutation p-value can fall to 0.05"
            )


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


def _lead_lag_rows(
    observations: Iterable[ReplayObservation], settings: LeadLagSettings
) -> list[dict[str, object]]:
    series = price_series(observations, settings.tick_bin_ns)
    grouped: dict[str, list[str]] = defaultdict(list)
    for pair, exchange in series:
        grouped[pair].append(exchange)

    rows: list[dict[str, object]] = []
    for pair in sorted(grouped):
        exchanges = sorted(set(grouped[pair]))
        for left_exchange, right_exchange in combinations(exchanges, 2):
            analysis = analyze_pair(
                series[(pair, left_exchange)],
                series[(pair, right_exchange)],
                max_lag_ns=settings.max_lag_ns,
                lag_step_ns=settings.lag_step_ns,
                min_overlap=settings.min_overlap,
                windows=settings.windows,
                null_surrogates=settings.null_surrogates,
            )
            rows.append(
                lead_lag_row(
                    pair,
                    left_exchange,
                    right_exchange,
                    analysis,
                    min_overlap=settings.min_overlap,
                )
            )
    return rows


def _sensitivity_variants(settings: LeadLagSettings) -> list[tuple[str, LeadLagSettings]]:
    """One-at-a-time half and double variants of each knob, null surrogates disabled."""
    base = replace(settings, null_surrogates=0)
    variants = [("baseline", base)]
    for name, field in (
        ("tick_bin", "tick_bin_ns"),
        ("lag_step", "lag_step_ns"),
        ("windows", "windows"),
        ("min_overlap", "min_overlap"),
    ):
        current = getattr(base, field)
        for label, value in (("half", max(1, current // 2)), ("double", current * 2)):
            candidate = replace(base, **{field: value})
            if field == "lag_step_ns" and base.max_lag_ns > 0 and value > base.max_lag_ns:
                continue
            variants.append((f"{name}_{label}", candidate))
    return variants


def _lead_lag_sensitivity_rows(
    observations: list[ReplayObservation], settings: LeadLagSettings
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for variant, candidate in _sensitivity_variants(settings):
        for row in _lead_lag_rows(observations, candidate):
            rows.append(
                {
                    "module": "lead_lag_sensitivity",
                    "variant": variant,
                    "tick_bin_ns": candidate.tick_bin_ns,
                    "lag_step_ns": candidate.lag_step_ns,
                    "windows": candidate.windows,
                    "min_overlap": candidate.min_overlap,
                    "null_surrogates": 0,
                    "pair": row["pair"],
                    "left_exchange": row["left_exchange"],
                    "right_exchange": row["right_exchange"],
                    "status": row["status"],
                    "reason": row["reason"],
                    "leader_exchange": row.get("leader_exchange"),
                    "estimated_lead_ns": row.get("estimated_lead_ns"),
                    "hayashi_yoshida_correlation": row["hayashi_yoshida_correlation"],
                    "window_agreement_fraction": row["window_agreement_fraction"],
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
    min_overlap: int = 30,
    windows: int = 4,
    null_surrogates: int = 20,
    sensitivity: bool = True,
) -> dict[str, object]:
    """Build all file-backed research datasets from one replay report."""
    settings = LeadLagSettings(
        tick_bin_ns=tick_bin_ns,
        max_lag_ns=max_lag_ns,
        lag_step_ns=lag_step_ns,
        min_overlap=min_overlap,
        windows=windows,
        null_surrogates=null_surrogates,
    )
    settings.validate()
    episodes = _canonical_episode_rows(report)
    survival = _survival_rows(episodes)
    fill_rates = [{"module": "venue_fill_rate", **row} for row in sampler.tracker.rows()]
    lead_lag = _lead_lag_rows(report.observations, settings)
    lead_lag_sensitivity = (
        _lead_lag_sensitivity_rows(report.observations, settings) if sensitivity else []
    )
    return {
        "episodes": episodes,
        "survival": survival,
        "fill_rates": fill_rates,
        "lead_lag": lead_lag,
        "lead_lag_sensitivity": lead_lag_sensitivity,
        "measurement": {
            "price_series_source": "canonical_post_apply_observations",
            "replay_observation_version": 1,
            "estimator": "Hayashi-Yoshida asynchronous return correlation",
            "estimator_references": ESTIMATOR_REFERENCES,
            "lag_selection": "argmax |U(theta)| over a symmetric grid; ties resolve to the smallest |theta|",
            "tick_bin_ns": tick_bin_ns,
            "lag_step_ns": lag_step_ns,
            "max_lag_ns": max_lag_ns,
            "grid_max_lag_ns": lag_grid(max_lag_ns, lag_step_ns).max_lag_ns,
            "min_overlap": min_overlap,
            "windows": windows,
            "null_surrogates": null_surrogates,
            "measurement_floor_ns": max(tick_bin_ns, lag_step_ns),
            "uncertainty": (
                "no confidence interval; window stability and a shifted-surrogate null "
                "for lag-grid selection are reported instead"
            ),
            "sensitivity": (
                "one-at-a-time half and double of tick bin, lag step, windows, and min overlap"
                if sensitivity
                else "disabled"
            ),
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
    min_overlap: int = 30,
    windows: int = 4,
    null_surrogates: int = 20,
    sensitivity: bool = True,
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
        min_overlap=min_overlap,
        windows=windows,
        null_surrogates=null_surrogates,
        sensitivity=sensitivity,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "episodes": "episodes.jsonl",
        "survival": "survival_by_notional.jsonl",
        "fill_rates": "venue_fill_rates.jsonl",
        "lead_lag": "lead_lag.jsonl",
        "lead_lag_sensitivity": "lead_lag_sensitivity.jsonl",
    }
    counts = {
        name: _write_jsonl(output_dir / filename, cast(list[dict[str, object]], datasets[name]))
        for name, filename in files.items()
    }
    metadata: dict[str, object] = {
        "format": "arbsync-research",
        "version": 2,
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
        "--min-overlap",
        type=int,
        default=30,
        help="lags with fewer overlapping return pairs never enter lag selection",
    )
    parser.add_argument(
        "--windows", type=int, default=4, help="equal time windows for leader stability"
    )
    parser.add_argument(
        "--null-surrogates",
        type=int,
        default=20,
        help="displacement surrogates per sign for the grid-selection null; 0 disables",
    )
    parser.add_argument(
        "--no-sensitivity",
        action="store_true",
        help="skip the one-at-a-time lead/lag sensitivity dataset",
    )
    parser.add_argument(
        "--allow-lossy",
        action="store_true",
        help="analyze captures that declare dropped frames and record that override",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = LeadLagSettings(
        tick_bin_ns=int(args.tick_bin_ms * 1_000_000),
        max_lag_ns=int(args.max_lag_ms * 1_000_000),
        lag_step_ns=int(args.lag_step_ms * 1_000_000),
        min_overlap=args.min_overlap,
        windows=args.windows,
        null_surrogates=args.null_surrogates,
    )
    try:
        settings.validate()
    except ValueError as error:
        raise SystemExit(str(error)) from error
    metadata = asyncio.run(
        run_research(
            args.capture,
            load_config(args.config),
            args.output_dir,
            tick_bin_ns=settings.tick_bin_ns,
            max_lag_ns=settings.max_lag_ns,
            lag_step_ns=settings.lag_step_ns,
            min_overlap=settings.min_overlap,
            windows=settings.windows,
            null_surrogates=settings.null_surrogates,
            sensitivity=not args.no_sensitivity,
            allow_lossy=args.allow_lossy,
        )
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
