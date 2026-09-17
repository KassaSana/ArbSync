"""Report fee survival from the product's stored executable-pricing ledgers.

Schema-v4 episodes carry the exact fee schedule and measured depth impact used by
the product. The default report reads those stored decimal strings, so rerunning it
does not substitute today's fees or reconstruct executable values from top of book.

`--fee EXCHANGE=PCT` remains an explicitly counterfactual top-of-book scenario for
older research data. It is not the product result. Latency, inventory and withdrawal
costs remain excluded, so a survivor is not a trade recommendation.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class EpisodeRow:
    start_ns: int
    end_ns: int | None
    pair: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    max_size: Decimal
    quote_asset: str
    theoretical_profit: Decimal
    peak_spread_pct: Decimal
    peak_size: Decimal
    peak_profit: Decimal
    close_reason: str | None
    pricing_ledgers: tuple[dict[str, object], ...] = ()

    @property
    def peak_notional(self) -> Decimal:
        """Quote-currency value of the buy leg at the size recorded at peak spread.

        The buy price at peak is not stored; the open price is the closest
        recorded figure and differs from it by at most the spread's movement.
        """
        return self.buy_price * self.peak_size

    @property
    def duration_seconds(self) -> Decimal | None:
        if self.end_ns is None:
            return None
        return Decimal(self.end_ns - self.start_ns) / Decimal(1_000_000_000)


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    fees_pct: dict[str, Decimal]
    survivors: int
    net_profit_by_quote: dict[str, Decimal]
    missing_fee_exchanges: tuple[str, ...]


def parse_utc(value: str) -> int:
    """Parse an ISO 8601 timestamp into nanoseconds; naive values are treated as UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


BASE_EPISODE_COLUMNS = (
    "start_ns, end_ns, pair, buy_exchange, sell_exchange, buy_price, sell_price, spread_pct, "
    "max_size, quote_asset, theoretical_profit, peak_spread_pct, peak_size, peak_profit, "
    "close_reason"
)


def load_rows(database: Path, start_ns: int | None, end_ns: int | None) -> list[EpisodeRow]:
    """Episodes that started inside the inclusive window, in start order."""
    clauses: list[str] = []
    params: list[int] = []
    if start_ns is not None:
        clauses.append("start_ns >= ?")
        params.append(start_ns)
    if end_ns is not None:
        clauses.append("start_ns <= ?")
        params.append(end_ns)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(opportunity_episodes)")
        }
        ledger_column = "pricing_ledgers" if "pricing_ledgers" in columns else "'[]'"
        query = (
            f"SELECT {BASE_EPISODE_COLUMNS}, {ledger_column} FROM opportunity_episodes"
            f"{where} ORDER BY start_ns, id"
        )
        return [
            EpisodeRow(
                start_ns=int(row[0]),
                end_ns=None if row[1] is None else int(row[1]),
                pair=str(row[2]),
                buy_exchange=str(row[3]),
                sell_exchange=str(row[4]),
                buy_price=Decimal(row[5]),
                sell_price=Decimal(row[6]),
                spread_pct=Decimal(row[7]),
                max_size=Decimal(row[8]),
                quote_asset=str(row[9]),
                theoretical_profit=Decimal(row[10]),
                peak_spread_pct=Decimal(row[11]),
                peak_size=Decimal(row[12]),
                peak_profit=Decimal(row[13]),
                close_reason=None if row[14] is None else str(row[14]),
                pricing_ledgers=tuple(json.loads(str(row[15]))),
            )
            for row in connection.execute(query, params)
        ]


def net_spread_pct(row: EpisodeRow, fees_pct: dict[str, Decimal]) -> Decimal | None:
    """Peak spread after a taker fee on each leg, or None when a venue has no fee entry."""
    buy_fee = fees_pct.get(row.buy_exchange)
    sell_fee = fees_pct.get(row.sell_exchange)
    if buy_fee is None or sell_fee is None:
        return None
    return row.peak_spread_pct - buy_fee - sell_fee


def apply_scenario(
    name: str, fees_pct: dict[str, Decimal], rows: list[EpisodeRow]
) -> ScenarioResult:
    survivors = 0
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
        # Each episode is one dislocation, paid once at its widest moment; the
        # level is gone after the first fill however long the spread rested.
        profit_by_quote[row.quote_asset] = profit_by_quote.get(
            row.quote_asset, Decimal(0)
        ) + row.peak_notional * net / Decimal(100)
    return ScenarioResult(
        name=name,
        fees_pct=dict(fees_pct),
        survivors=survivors,
        net_profit_by_quote=profit_by_quote,
        missing_fee_exchanges=tuple(sorted(missing)),
    )


def stored_survival(rows: list[EpisodeRow]) -> list[dict[str, object]]:
    """Aggregate stored net results by configured notional without recomputing fees."""
    totals: dict[str, dict[str, object]] = {}
    for row in rows:
        for ledger in row.pricing_ledgers:
            notional = str(ledger["notional"])
            entry = totals.setdefault(
                notional,
                {
                    "notional": notional,
                    "priced": 0,
                    "insufficient_depth": 0,
                    "survivors": 0,
                    "net_profit_by_quote": {},
                },
            )
            net_literal = ledger.get("net_executable_spread_pct")
            if net_literal is None:
                entry["insufficient_depth"] = int(entry["insufficient_depth"]) + 1
                continue
            entry["priced"] = int(entry["priced"]) + 1
            net = Decimal(str(net_literal))
            if net <= 0:
                continue
            entry["survivors"] = int(entry["survivors"]) + 1
            profits = entry["net_profit_by_quote"]
            assert isinstance(profits, dict)
            profits[row.quote_asset] = str(
                Decimal(str(profits.get(row.quote_asset, "0")))
                + Decimal(notional) * net / Decimal(100)
            )
    return [totals[key] for key in sorted(totals, key=Decimal)]


def percentile(values: list[Decimal], fraction: float) -> Decimal:
    """Nearest-rank percentile over an already sorted list."""
    if not values:
        raise ValueError("percentile of empty list")
    index = min(len(values) - 1, int(fraction * len(values)))
    return values[index]


def summarize(rows: list[EpisodeRow]) -> dict[str, object]:
    spreads = sorted(row.peak_spread_pct for row in rows)
    notionals = sorted(row.peak_notional for row in rows)
    lifetimes = sorted(
        duration for duration in (row.duration_seconds for row in rows) if duration is not None
    )
    routes: dict[str, int] = {}
    pairs: dict[str, int] = {}
    close_reasons: dict[str, int] = {}
    pre_fee: dict[str, Decimal] = {}
    for row in rows:
        route = f"{row.buy_exchange}->{row.sell_exchange} {row.quote_asset}"
        routes[route] = routes.get(route, 0) + 1
        pairs[row.pair] = pairs.get(row.pair, 0) + 1
        reason = row.close_reason or "open"
        close_reasons[reason] = close_reasons.get(reason, 0) + 1
        pre_fee[row.quote_asset] = pre_fee.get(row.quote_asset, Decimal(0)) + row.peak_profit
    summary: dict[str, object] = {
        "episodes": len(rows),
        "routes": dict(sorted(routes.items(), key=lambda item: -item[1])),
        "pairs": dict(sorted(pairs.items(), key=lambda item: -item[1])),
        "close_reasons": dict(sorted(close_reasons.items(), key=lambda item: -item[1])),
        "pre_fee_peak_profit_by_quote": {asset: str(value) for asset, value in pre_fee.items()},
    }
    if rows:
        summary["peak_spread_pct"] = {
            "min": str(spreads[0]),
            "p50": str(percentile(spreads, 0.5)),
            "p90": str(percentile(spreads, 0.9)),
            "p99": str(percentile(spreads, 0.99)),
            "max": str(spreads[-1]),
        }
        summary["notional_at_peak"] = {
            "p50": str(percentile(notionals, 0.5)),
            "max": str(notionals[-1]),
        }
    if lifetimes:
        summary["lifetime_seconds"] = {
            "closed": len(lifetimes),
            "p50": str(percentile(lifetimes, 0.5)),
            "p90": str(percentile(lifetimes, 0.9)),
            "max": str(lifetimes[-1]),
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
    lines = [f"episodes: {summary['episodes']}"]
    spread = summary.get("peak_spread_pct")
    if isinstance(spread, dict):
        lines.append(
            "peak spread_pct: "
            + "  ".join(f"{key} {Decimal(str(value)):.4f}" for key, value in spread.items())
        )
    notional = summary.get("notional_at_peak")
    if isinstance(notional, dict):
        lines.append(
            "notional at peak: "
            + "  ".join(f"{key} {Decimal(str(value)):.2f}" for key, value in notional.items())
        )
    lifetime = summary.get("lifetime_seconds")
    if isinstance(lifetime, dict):
        lines.append(
            "lifetime seconds: "
            + "  ".join(
                f"{key} {value}" if key == "closed" else f"{key} {Decimal(str(value)):.3f}"
                for key, value in lifetime.items()
            )
        )
    lines.append(f"routes: {summary['routes']}")
    lines.append(f"pairs: {summary['pairs']}")
    lines.append(f"close reasons: {summary['close_reasons']}")
    lines.append(f"pre-fee theoretical profit at peak: {summary['pre_fee_peak_profit_by_quote']}")
    lines.append("")
    for result in results:
        fees = ", ".join(f"{name} {pct}" for name, pct in result.fees_pct.items())
        profit = (
            ", ".join(f"{value:.4f} {asset}" for asset, value in result.net_profit_by_quote.items())
            or "0"
        )
        lines.append(
            f"{result.name} ({fees}): {result.survivors}/{summary['episodes']} survive at peak; "
            f"net profit once per episode = {profit}"
        )
        if result.missing_fee_exchanges:
            lines.append(
                "  skipped episodes touching venues with no fee entry: "
                + ", ".join(result.missing_fee_exchanges)
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--database", type=Path, default=Path("var/arb.sqlite3"), help="SQLite file to read"
    )
    parser.add_argument("--start", help="ISO 8601 UTC lower bound on episode start (inclusive)")
    parser.add_argument("--end", help="ISO 8601 UTC upper bound on episode start (inclusive)")
    parser.add_argument(
        "--fee",
        action="append",
        type=parse_fee_argument,
        metavar="EXCHANGE=PCT",
        help="counterfactual per-side taker fee in percent; repeat per venue",
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
    summary = summarize(rows)
    if not args.fee:
        document = {
            "database": str(args.database),
            "start": args.start,
            "end": args.end,
            "summary": summary,
            "stored_fee_survival": stored_survival(rows),
        }
        if args.json:
            print(json.dumps(document, indent=2))
        else:
            print(render_stored(summary, document["stored_fee_survival"]))
        return
    results = [apply_scenario("counterfactual", dict(args.fee), rows)]
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


def render_stored(summary: dict[str, object], rows: object) -> str:
    lines = [f"episodes: {summary['episodes']}", "stored product fee survival:"]
    assert isinstance(rows, list)
    if not rows:
        lines.append("  no stored pricing ledgers (schema-v3 or empty database)")
    for row in rows:
        assert isinstance(row, dict)
        lines.append(
            f"  {row['notional']}: {row['survivors']}/{row['priced']} priced routes survive; "
            f"{row['insufficient_depth']} insufficient depth; net profit {row['net_profit_by_quote']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
