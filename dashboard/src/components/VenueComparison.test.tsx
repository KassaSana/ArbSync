import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DepthPricing, Opportunity, PairRecord } from "../api/client";
import { ready } from "../lib/async";
import { VenueComparison } from "./VenueComparison";

const pairs: PairRecord[] = [
  { exchange: "coinbase", pair: "BTC-USD" },
  { exchange: "gemini", pair: "BTC-USD" },
  { exchange: "kraken", pair: "BTC-USD" },
];

function status(exchange: string, eligible: boolean) {
  return {
    exchange,
    pair: "BTC-USD",
    initialized: true,
    continuous: true,
    connected: eligible,
    age_ms: 10,
    max_age_ms: 60_000,
    eligible,
    reason: eligible ? null : "disconnected",
    receivedAtMs: 1_000,
  };
}

const pricing: DepthPricing = {
  notionals: ["100", "1000"],
  quotes: [
    {
      exchange: "gemini",
      pair: "BTC-USD",
      side: "buy",
      notional: "100",
      vwap: "100",
      insufficient_depth: false,
      filled_notional: "100",
      filled_base: "1",
      levels_used: 1,
      subscribed_depth_levels: null,
    },
    {
      exchange: "gemini",
      pair: "BTC-USD",
      side: "sell",
      notional: "100",
      vwap: "101",
      insufficient_depth: false,
      filled_notional: "101",
      filled_base: "1",
      levels_used: 1,
      subscribed_depth_levels: null,
    },
    {
      exchange: "coinbase",
      pair: "BTC-USD",
      side: "buy",
      notional: "100",
      vwap: "99",
      insufficient_depth: false,
      filled_notional: "100",
      filled_base: "1.01",
      levels_used: 1,
      subscribed_depth_levels: null,
    },
    {
      exchange: "coinbase",
      pair: "BTC-USD",
      side: "sell",
      notional: "100",
      vwap: null,
      insufficient_depth: true,
      filled_notional: "50",
      filled_base: "0.5",
      levels_used: 1,
      subscribed_depth_levels: 50,
    },
    {
      exchange: "gemini",
      pair: "BTC-USD",
      side: "buy",
      notional: "1000",
      vwap: "110",
      insufficient_depth: false,
      filled_notional: "1000",
      filled_base: "9.09",
      levels_used: 2,
      subscribed_depth_levels: null,
    },
    {
      exchange: "gemini",
      pair: "BTC-USD",
      side: "sell",
      notional: "1000",
      vwap: "111",
      insufficient_depth: false,
      filled_notional: "1110",
      filled_base: "9.09",
      levels_used: 2,
      subscribed_depth_levels: null,
    },
  ],
  routes: [
    {
      pair: "BTC-USD",
      buy_exchange: "coinbase",
      sell_exchange: "gemini",
      notional: "100",
      top_of_book_spread_pct: "2",
      buy_vwap: "99",
      sell_vwap: "101",
      gross_executable_spread_pct: "2",
      depth_impact_pct: "0",
      buy_taker_fee_pct: "0.4",
      sell_taker_fee_pct: "0.6",
      fee_impact_pct: "-1",
      net_executable_spread_pct: "1",
      insufficient_depth: false,
      buy_age_ms: 30,
      sell_age_ms: 45,
      age_skew_ms: 15,
    },
    {
      pair: "BTC-USD",
      buy_exchange: "gemini",
      sell_exchange: "coinbase",
      notional: "100",
      top_of_book_spread_pct: "-1",
      buy_vwap: null,
      sell_vwap: null,
      gross_executable_spread_pct: null,
      depth_impact_pct: null,
      buy_taker_fee_pct: "0.4",
      sell_taker_fee_pct: "0.6",
      fee_impact_pct: null,
      net_executable_spread_pct: null,
      insufficient_depth: true,
      buy_age_ms: 45,
      sell_age_ms: 30,
      age_skew_ms: 15,
    },
  ],
};

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    start_ns: "900000000",
    end_ns: null,
    duration_ns: null,
    pair: "BTC-USD",
    quote_asset: "USD",
    buy_exchange: "coinbase",
    sell_exchange: "gemini",
    buy_price: "99",
    sell_price: "101",
    spread_pct: "2",
    max_size: "1",
    theoretical_profit: "2",
    peak_spread_pct: "2",
    peak_size: "1",
    peak_profit: "2",
    pricing_ledgers: [],
    close_spread_pct: null,
    close_reason: null,
    ...overrides,
  };
}

describe("VenueComparison", () => {
  it("shows executable prices, route economics, lifetime, and distinct unavailable states", () => {
    render(
      <VenueComparison
        pairs={pairs}
        statuses={{
          "coinbase:BTC-USD": status("coinbase", true),
          "gemini:BTC-USD": status("gemini", true),
          "kraken:BTC-USD": status("kraken", false),
        }}
        pricing={ready(pricing)}
        opportunities={ready([opportunity()])}
        nowMs={1_000}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("Cheapest buy:")).toBeInTheDocument();
    expect(screen.getByText("coinbase", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Best sell:")).toBeInTheDocument();
    expect(screen.getByText("Gross: 2.000%")).toBeInTheDocument();
    expect(screen.getByText("Net: 1.000%")).toBeInTheDocument();
    expect(screen.getByText("current lifetime: 100ms")).toBeInTheDocument();
    expect(screen.getByText("insufficient depth", { selector: "td" })).toBeInTheDocument();
    expect(screen.getAllByText("unavailable", { selector: "td" }).length).toBeGreaterThan(0);
  });

  it("recomputes the table for the selected configured notional", () => {
    render(
      <VenueComparison
        pairs={[{ exchange: "gemini", pair: "BTC-USD" }]}
        statuses={{ "gemini:BTC-USD": status("gemini", true) }}
        pricing={ready(pricing)}
        opportunities={ready([])}
        nowMs={1_000}
        onRetry={() => undefined}
      />,
    );

    fireEvent.change(screen.getByRole("combobox", { name: "Quote notional" }), {
      target: { value: "1000" },
    });
    expect(screen.getByText("110.0000")).toBeInTheDocument();
    expect(screen.getByText("111.0000")).toBeInTheDocument();
  });
});
