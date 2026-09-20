"""Shared aggregates over canonical research episode rows.

Both the whole-capture survival dataset and the per-band age/skew datasets
summarize the same episode rows, so the arithmetic lives here once. Survival
counts are exact integers and profit sums are Decimal strings; percentiles are
floating-point observability summaries, never accounting values.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal
from typing import Any


def survival_by_notional(episodes: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """Fee and executable-size survival per notional from stored pricing ledgers.

    `fee_survival_rate` is survivors over priced observations;
    `executable_size_survival_rate` is survivors over all observations,
    including insufficient-depth outcomes.
    """
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
                **entry,
                "fee_survival_rate": survivors / priced if priced else None,
                "executable_size_survival_rate": survivors / observations if observations else None,
            }
        )
    return rows


def survives_any_notional(episode: dict[str, object]) -> bool:
    """True when at least one stored ledger is priced and net positive."""
    ledgers = episode.get("pricing_ledgers", [])
    if not isinstance(ledgers, list):
        return False
    for ledger in ledgers:
        if not isinstance(ledger, dict) or bool(ledger.get("insufficient_depth")):
            continue
        net = ledger.get("net_executable_spread_pct")
        if net is not None and Decimal(str(net)) > 0:
            return True
    return False


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of `values`; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]
