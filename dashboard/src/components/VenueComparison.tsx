import { useState } from "react";
import type {
  DepthPricing,
  DepthQuote,
  ExecutableRoute,
  Opportunity,
  PairRecord,
} from "../api/client";
import { Async } from "../lib/async";
import { age, nsToMs, price } from "../lib/format";
import { TrackedBookStatus } from "../state/live";
import { Panel } from "./Panel";
import { Placeholder } from "./Placeholder";

type Props = {
  pairs: PairRecord[];
  statuses: Record<string, TrackedBookStatus>;
  pricing: Async<DepthPricing>;
  opportunities: Async<Opportunity[]>;
  nowMs: number;
  onRetry: () => void;
};

type SideView = {
  quote: DepthQuote | null;
  state: "available" | "insufficient" | "unavailable";
};

type VenueRow = {
  exchange: string;
  buy: SideView;
  sell: SideView;
};

type ComparisonRow = {
  pair: string;
  venues: VenueRow[];
  cheapestBuy: VenueRow | null;
  bestSell: VenueRow | null;
  route: ExecutableRoute | null;
  routeState: "available" | "insufficient" | "unavailable";
  lifetime: { label: "current" | "most recent" | "unknown" | "unavailable"; value: string };
};

function bookKey(exchange: string, pair: string): string {
  return `${exchange}:${pair}`;
}

function quoteKey(quote: Pick<DepthQuote, "exchange" | "pair" | "side" | "notional">): string {
  return `${quote.exchange}:${quote.pair}:${quote.side}:${quote.notional}`;
}

function sideView(
  quote: DepthQuote | undefined,
  status: TrackedBookStatus | undefined,
): SideView {
  if (quote === undefined || status?.eligible === false) {
    return { quote: null, state: "unavailable" };
  }
  return {
    quote,
    state: quote.insufficient_depth ? "insufficient" : "available",
  };
}

function mostRecentLifetime(
  pair: string,
  opportunities: Async<Opportunity[]>,
  nowMs: number,
): ComparisonRow["lifetime"] {
  if (opportunities.state !== "ready") {
    return { label: "unavailable", value: "unavailable" };
  }

  const rows = opportunities.data
    .filter((opportunity) => opportunity.pair === pair)
    .sort((left, right) => (BigInt(left.start_ns) > BigInt(right.start_ns) ? -1 : 1));
  const current = rows.find(
    (opportunity) => opportunity.close_reason === null && opportunity.duration_ns === null,
  );
  if (current !== undefined) {
    const elapsedMs = Math.max(0, nowMs - nsToMs(current.start_ns));
    return { label: "current", value: age(elapsedMs) };
  }

  const recent = rows.find((opportunity) => opportunity.duration_ns !== null);
  if (recent !== undefined && recent.duration_ns !== null) {
    return { label: "most recent", value: age(nsToMs(recent.duration_ns)) };
  }
  return rows.length > 0
    ? { label: "unknown", value: "unknown" }
    : { label: "unavailable", value: "unavailable" };
}

function bestQuote(venues: VenueRow[], side: "buy" | "sell"): VenueRow | null {
  const available = venues.filter((venue) => venue[side].state === "available");
  if (available.length === 0) {
    return null;
  }
  return available.reduce((best, candidate) => {
    const bestPrice = Number(best[side].quote?.vwap);
    const candidatePrice = Number(candidate[side].quote?.vwap);
    const wins = side === "buy" ? candidatePrice < bestPrice : candidatePrice > bestPrice;
    return wins ? candidate : best;
  });
}

function routeSelection(
  routes: ExecutableRoute[],
  pair: string,
  notional: string,
): Pick<ComparisonRow, "route" | "routeState"> {
  const candidates = routes.filter(
    (route) => route.pair === pair && route.notional === notional,
  );
  const executable = candidates.filter(
    (route) =>
      !route.insufficient_depth &&
      route.gross_executable_spread_pct !== null &&
      route.net_executable_spread_pct !== null,
  );
  if (executable.length > 0) {
    const best = executable.reduce((current, candidate) => {
      const currentNet = Number(current.net_executable_spread_pct);
      const candidateNet = Number(candidate.net_executable_spread_pct);
      if (candidateNet !== currentNet) {
        return candidateNet > currentNet ? candidate : current;
      }
      return Number(candidate.gross_executable_spread_pct) > Number(current.gross_executable_spread_pct)
        ? candidate
        : current;
    });
    return { route: best, routeState: "available" };
  }
  return {
    route: null,
    routeState: candidates.length > 0 ? "insufficient" : "unavailable",
  };
}

function buildRows(
  pairs: PairRecord[],
  statuses: Record<string, TrackedBookStatus>,
  pricing: DepthPricing,
  opportunities: Async<Opportunity[]>,
  nowMs: number,
  notional: string,
): ComparisonRow[] {
  const venuesByPair = new Map<string, string[]>();
  for (const record of pairs) {
    const venues = venuesByPair.get(record.pair) ?? [];
    if (!venues.includes(record.exchange)) {
      venues.push(record.exchange);
    }
    venuesByPair.set(record.pair, venues);
  }

  const quoteMap = new Map(pricing.quotes.map((quote) => [quoteKey(quote), quote]));
  return [...venuesByPair.entries()].sort(([left], [right]) => left.localeCompare(right)).map(
    ([pair, exchanges]) => {
      const venues = exchanges.sort().map((exchange) => {
        const status = statuses[bookKey(exchange, pair)];
        return {
          exchange,
          buy: sideView(
            quoteMap.get(`${exchange}:${pair}:buy:${notional}`),
            status,
          ),
          sell: sideView(
            quoteMap.get(`${exchange}:${pair}:sell:${notional}`),
            status,
          ),
        };
      });
      const route = routeSelection(pricing.routes, pair, notional);
      return {
        pair,
        venues,
        cheapestBuy: bestQuote(venues, "buy"),
        bestSell: bestQuote(venues, "sell"),
        ...route,
        lifetime: mostRecentLifetime(pair, opportunities, nowMs),
      };
    },
  );
}

function baseAsset(pair: string): string {
  const [base] = pair.split("-");
  return base ?? pair;
}

function priceCell(view: SideView): string {
  if (view.state === "unavailable") {
    return "unavailable";
  }
  if (view.state === "insufficient") {
    return "insufficient depth";
  }
  return price(view.quote?.vwap);
}

function depthCell(view: SideView, pair: string): string {
  if (view.state === "unavailable") {
    return "unavailable";
  }
  const filled = view.quote?.filled_base ?? "0";
  return view.state === "insufficient"
    ? `insufficient · ${filled} ${baseAsset(pair)}`
    : `${filled} ${baseAsset(pair)}`;
}

function spreadCell(value: string | null | undefined, state: ComparisonRow["routeState"]): string {
  if (state === "insufficient") {
    return "insufficient depth";
  }
  if (state === "unavailable" || value === null || value === undefined) {
    return "unavailable";
  }
  return `${Number(value).toFixed(3)}%`;
}

export function VenueComparison({
  pairs,
  statuses,
  pricing,
  opportunities,
  nowMs,
  onRetry,
}: Props) {
  const [selectedNotional, setSelectedNotional] = useState<string | null>(null);

  if (pricing.state === "failed") {
    return (
      <Panel title="Venue comparison" tone="crit">
        <Placeholder
          state="failed"
          title="Could not load executable pricing"
          detail={pricing.error}
          onRetry={onRetry}
        />
      </Panel>
    );
  }
  if (pricing.state === "loading") {
    return (
      <Panel title="Venue comparison">
        <Placeholder state="loading" title="Loading executable pricing" />
      </Panel>
    );
  }
  if (pricing.data.notionals.length === 0) {
    return (
      <Panel title="Venue comparison">
        <Placeholder
          state="empty"
          title="No executable notionals are configured"
          detail="Add a pricing notional to compare venue depth and fee-adjusted spreads."
        />
      </Panel>
    );
  }

  const notional =
    selectedNotional !== null && pricing.data.notionals.includes(selectedNotional)
      ? selectedNotional
      : pricing.data.notionals[0];
  const rows = buildRows(pairs, statuses, pricing.data, opportunities, nowMs, notional);
  return (
    <Panel
      title="Venue comparison"
      meta={
        <label className="flex items-center gap-2">
          <span>Quote notional</span>
          <select
            aria-label="Quote notional"
            className="rounded border border-line bg-raised px-2 py-1 text-ink"
            value={notional}
            onChange={(event) => setSelectedNotional(event.target.value)}
          >
            {pricing.data.notionals.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      }
      className="overflow-hidden"
    >
      <div className="space-y-3 p-3">
        {rows.length === 0 ? (
          <p className="px-1 py-3 text-xs text-ink-3">Waiting for configured pairs.</p>
        ) : null}
        {rows.map((row) => (
          <section key={row.pair} className="overflow-hidden rounded border border-line-soft">
            <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2 border-b border-line-soft bg-raised/40 px-3 py-2 text-xs">
              <h3 className="font-medium text-ink">{row.pair}</h3>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-ink-2">
                <span>
                  Cheapest buy: <strong className="text-ink">{row.cheapestBuy?.exchange ?? "unavailable"}</strong>
                </span>
                <span>
                  Best sell: <strong className="text-ink">{row.bestSell?.exchange ?? "unavailable"}</strong>
                </span>
                <span>
                  Gross: {spreadCell(row.route?.gross_executable_spread_pct, row.routeState)}
                </span>
                <span>
                  Net: {spreadCell(row.route?.net_executable_spread_pct, row.routeState)}
                </span>
                <span>
                  {row.lifetime.label === "unavailable" ? "Lifetime" : `${row.lifetime.label} lifetime`}: {row.lifetime.value}
                </span>
              </div>
            </header>
            <div className="overflow-x-auto">
              <table className="min-w-full border-collapse text-xs">
                <caption className="sr-only">
                  Executable buy and sell prices and depth by venue for {row.pair} at {notional} quote units.
                </caption>
                <thead>
                  <tr className="border-b border-line-soft text-left text-micro text-ink-3">
                    <th scope="col" className="px-3 py-1.5 font-normal">Venue</th>
                    <th scope="col" className="px-3 py-1.5 text-right font-normal">Buy VWAP</th>
                    <th scope="col" className="px-3 py-1.5 text-right font-normal">Sell VWAP</th>
                    <th scope="col" className="px-3 py-1.5 text-right font-normal">Buy depth</th>
                    <th scope="col" className="px-3 py-1.5 text-right font-normal">Sell depth</th>
                  </tr>
                </thead>
                <tbody>
                  {row.venues.map((venue) => (
                    <tr key={venue.exchange} className="border-b border-line-soft/60 last:border-0">
                      <th scope="row" className="px-3 py-1.5 text-left font-medium text-ink">
                        {venue.exchange}
                        {row.cheapestBuy?.exchange === venue.exchange ? (
                          <span className="ml-2 text-micro text-signal">cheapest buy</span>
                        ) : null}
                        {row.bestSell?.exchange === venue.exchange ? (
                          <span className="ml-2 text-micro text-signal">best sell</span>
                        ) : null}
                      </th>
                      <td className="num px-3 py-1.5 text-right text-ink-2">{priceCell(venue.buy)}</td>
                      <td className="num px-3 py-1.5 text-right text-ink-2">{priceCell(venue.sell)}</td>
                      <td className="num px-3 py-1.5 text-right text-ink-3">
                        {depthCell(venue.buy, row.pair)}
                      </td>
                      <td className="num px-3 py-1.5 text-right text-ink-3">
                        {depthCell(venue.sell, row.pair)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      </div>
    </Panel>
  );
}
