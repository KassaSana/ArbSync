"""Apply per-venue taker fees to stored opportunities and count the survivors.

Stored opportunities are theoretical and pre-fee by design. This script answers the
question the detector deliberately does not: how many of them would still have been
positive after paying a taker fee on both legs, and how much would that have been worth
at the top-of-book size the detector recorded.

Fees are percentages of notional per side. The built-in scenarios are illustrative
base-tier public schedules, not live quotes; pass `--fee EXCHANGE=PCT` to use your own.
Everything else the detector excludes (slippage, latency, inventory, partial fills,
withdrawal costs) is still excluded, so a survivor is an upper bound, not a trade.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

BUILT_IN_SCENARIOS: dict[str, dict[str, Decimal]] = {
    "base_retail_taker": {
        "coinbase": Decimal("0.60"),
        "gemini": Decimal("0.40"),
        "binance": Decimal("0.60"),
    },
    "mid_volume_taker": {
        "coinbase": Decimal("0.35"),
        "gemini": Decimal("0.25"),
        "binance": Decimal("0.35"),
    },
    "vip_taker": {
        "coinbase": Decimal("0.10"),
        "gemini": Decimal("0.10"),
        "binance": Decimal("0.10"),
    },
    "zero_fee_upper_bound": {
        "coinbase": Decimal("0"),
        "gemini": Decimal("0"),
        "binance": Decimal("0"),
    },
}


@dataclass(frozen=True)
class OpportunityRow:
    timestamp_ns: int
    pair: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    max_size: Decimal
    quote_asset: str
    theoretical_profit: Decimal

    @property
    def notional(self) -> Decimal:
        """Quote-currency value of the buy leg at the recorded top-of-book size."""
        return self.buy_price * self.max_size

    def quote_key(self) -> tuple[str, str, str, Decimal, Decimal, Decimal]:
        """Identity of the pair of resting quotes that produced this row.

        The detector emits one row per book update while a spread persists, so a
        single resting order re-detected across cycles appears many times. Rows with
        the same key could be captured at most once.
        """
        return (
            self.pair,
            self.buy_exchange,
            self.sell_exchange,
            self.buy_price,
            self.sell_price,
            self.max_size,
        )


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    fees_pct: dict[str, Decimal]
    survivors: int
    distinct_survivors: int
    net_profit_by_quote: dict[str, Decimal]
    missing_fee_exchanges: tuple[str, ...]


def parse_utc(value: str) -> int:
    """Parse an ISO 8601 timestamp into nanoseconds; naive values are treated as UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def load_rows(database: Path, start_ns: int | None, end_ns: int | None) -> list[OpportunityRow]:
    clauses: list[str] = []
    params: list[int] = []
    if start_ns is not None:
        clauses.append("timestamp_ns >= ?")
        params.append(start_ns)
    if end_ns is not None:
        clauses.append("timestamp_ns <= ?")
        params.append(end_ns)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        "SELECT timestamp_ns, pair, buy_exchange, sell_exchange, buy_price, sell_price, "
        "spread_pct, max_size, quote_asset, theoretical_profit FROM opportunities"
        f"{where} ORDER BY timestamp_ns, id"
    )
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return [
            OpportunityRow(
                timestamp_ns=int(row[0]),
                pair=str(row[1]),
                buy_exchange=str(row[2]),
                sell_exchange=str(row[3]),
                buy_price=Decimal(row[4]),
                sell_price=Decimal(row[5]),
                spread_pct=Decimal(row[6]),
                max_size=Decimal(row[7]),
                quote_asset=str(row[8]),
                theoretical_profit=Decimal(row[9]),
            )
            for row in connection.execute(query, params)
        ]


def net_spread_pct(row: OpportunityRow, fees_pct: dict[str, Decimal]) -> Decimal | None:
    """Spread after a taker fee on each leg, or None when a venue has no fee entry."""
    buy_fee = fees_pct.get(row.buy_exchange)
    sell_fee = fees_pct.get(row.sell_exchange)
    if buy_fee is None or sell_fee is None:
        return None
    return row.spread_pct - buy_fee - sell_fee


def apply_scenario(
    name: str, fees_pct: dict[str, Decimal], rows: list[OpportunityRow]
) -> ScenarioResult:
    survivors = 0
    seen_quotes: set[tuple[str, str, str, Decimal, Decimal, Decimal]] = set()
    profit_by_quote: dict[str, Decimal] = {}
    missing: set[str] = set()
    for row in rows:
        net = net_spread_pct(row, fees_pct)
        if net is None:
            missing.update(
                exchange
                for exchange in (row.buy_exchange, row.sell_exchange)
                if exchange not in fees_pct
            )
            continue
        if net <= 0:
            continue
        survivors += 1
        key = row.quote_key()
        if key in seen_quotes:
            continue
        seen_quotes.add(key)
        # Profit is counted once per distinct resting-quote pair: the level is gone
        # after the first fill, however many times the detector reported it.
        profit_by_quote[row.quote_asset] = profit_by_quote.get(
            row.quote_asset, Decimal(0)
        ) + row.notional * net / Decimal(100)
    return ScenarioResult(
        name=name,
        fees_pct=dict(fees_pct),
        survivors=survivors,
        distinct_survivors=len(seen_quotes),
        net_profit_by_quote=profit_by_quote,
        missing_fee_exchanges=tuple(sorted(missing)),
    )


def percentile(values: list[Decimal], fraction: float) -> Decimal:
    """Nearest-rank percentile over an already sorted list."""
    if not values:
        raise ValueError("percentile of empty list")
    index = min(len(values) - 1, int(fraction * len(values)))
    return values[index]


def summarize(rows: list[OpportunityRow]) -> dict[str, object]:
    spreads = sorted(row.spread_pct for row in rows)
    notionals = sorted(row.notional for row in rows)
    routes: dict[str, int] = {}
    pairs: dict[str, int] = {}
    pre_fee: dict[str, Decimal] = {}
    for row in rows:
        route = f"{row.buy_exchange}->{row.sell_exchange} {row.quote_asset}"
        routes[route] = routes.get(route, 0) + 1
        pairs[row.pair] = pairs.get(row.pair, 0) + 1
        pre_fee[row.quote_asset] = pre_fee.get(row.quote_asset, Decimal(0)) + row.theoretical_profit
    summary: dict[str, object] = {
        "opportunities": len(rows),
        "distinct_quote_pairs": len({row.quote_key() for row in rows}),
        "routes": dict(sorted(routes.items(), key=lambda item: -item[1])),
        "pairs": dict(sorted(pairs.items(), key=lambda item: -item[1])),
        "pre_fee_profit_by_quote": {asset: str(value) for asset, value in pre_fee.items()},
    }
    if rows:
        summary["spread_pct"] = {
            "min": str(spreads[0]),
            "p50": str(percentile(spreads, 0.5)),
            "p90": str(percentile(spreads, 0.9)),
            "p99": str(percentile(spreads, 0.99)),
            "max": str(spreads[-1]),
        }
        summary["notional_at_top_of_book"] = {
            "p50": str(percentile(notionals, 0.5)),
            "max": str(notionals[-1]),
        }
    return summary


def parse_fee_argument(value: str) -> tuple[str, Decimal]:
    exchange, separator, pct = value.partition("=")
    if not separator or not exchange or not pct:
        raise argparse.ArgumentTypeError(f"expected EXCHANGE=PCT, got {value!r}")
    fee = Decimal(pct)
    if fee < 0:
        raise argparse.ArgumentTypeError(f"fee must be non-negative, got {value!r}")
    return exchange, fee


def render_text(summary: dict[str, object], results: list[ScenarioResult]) -> str:
    lines = [f"opportunities: {summary['opportunities']}"]
    lines.append(f"distinct resting-quote pairs: {summary['distinct_quote_pairs']}")
    spread = summary.get("spread_pct")
    if isinstance(spread, dict):
        lines.append(
            "spread_pct: "
            + "  ".join(f"{key} {Decimal(str(value)):.4f}" for key, value in spread.items())
        )
    notional = summary.get("notional_at_top_of_book")
    if isinstance(notional, dict):
        lines.append(
            "notional at top of book: "
            + "  ".join(f"{key} {Decimal(str(value)):.2f}" for key, value in notional.items())
        )
    lines.append(f"routes: {summary['routes']}")
    lines.append(f"pairs: {summary['pairs']}")
    lines.append(f"pre-fee theoretical profit: {summary['pre_fee_profit_by_quote']}")
    lines.append("")
    for result in results:
        fees = ", ".join(f"{name} {pct}" for name, pct in result.fees_pct.items())
        profit = (
            ", ".join(f"{value:.4f} {asset}" for asset, value in result.net_profit_by_quote.items())
            or "0"
        )
        lines.append(
            f"{result.name} ({fees}): {result.survivors}/{summary['opportunities']} survive, "
            f"{result.distinct_survivors} distinct; net profit once per quote pair = {profit}"
        )
        if result.missing_fee_exchanges:
            lines.append(
                "  skipped rows touching venues with no fee entry: "
                + ", ".join(result.missing_fee_exchanges)
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--database", type=Path, default=Path("var/arb.sqlite3"), help="SQLite file to read"
    )
    parser.add_argument("--start", help="ISO 8601 UTC lower bound (inclusive)")
    parser.add_argument("--end", help="ISO 8601 UTC upper bound (inclusive)")
    parser.add_argument(
        "--fee",
        action="append",
        type=parse_fee_argument,
        metavar="EXCHANGE=PCT",
        help="per-side taker fee in percent; repeat per venue. Replaces the built-in scenarios.",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON document instead of text")
    return parser


def main() -> None:
    args = _parser().parse_args()
    rows = load_rows(
        args.database,
        parse_utc(args.start) if args.start else None,
        parse_utc(args.end) if args.end else None,
    )
    scenarios = {"custom": dict(args.fee)} if args.fee else BUILT_IN_SCENARIOS
    summary = summarize(rows)
    results = [apply_scenario(name, fees, rows) for name, fees in scenarios.items()]
    if args.json:
        document = {
            "database": str(args.database),
            "start": args.start,
            "end": args.end,
            "summary": summary,
            "scenarios": [
                {
                    "name": result.name,
                    "fees_pct": {name: str(pct) for name, pct in result.fees_pct.items()},
                    "survivors": result.survivors,
                    "distinct_survivors": result.distinct_survivors,
                    "net_profit_by_quote": {
                        asset: str(value) for asset, value in result.net_profit_by_quote.items()
                    },
                    "missing_fee_exchanges": list(result.missing_fee_exchanges),
                }
                for result in results
            ],
        }
        print(json.dumps(document, indent=2))
    else:
        print(render_text(summary, results))


if __name__ == "__main__":
    main()
