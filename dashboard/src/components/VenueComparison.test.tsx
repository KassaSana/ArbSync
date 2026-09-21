import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DepthPricing, Opportunity, PairRecord } from "../api/client";
import { ready } from "../lib/async";
import type { TrackedBookStatus, TrackedDepthPricing } from "../state/live";
import { VenueComparison } from "./VenueComparison";

const pairs: PairRecord[] = [
  { exchange: "coinbase", pair: "BTC-USD" },
  { exchange: "gemini", pair: "BTC-USD" },
  { exchange: "kraken", pair: "BTC-USD" },
];

function status(
  exchange: string,
  eligible: boolean,
  overrides: Partial<TrackedBookStatus> = {},
): TrackedBookStatus {
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
    ...overrides,
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

function tracked(data: DepthPricing, receivedAtMs = 1_000, generation = 1): TrackedDepthPricing {
  return { ...data, receivedAtMs, generation };
}

const threeVenuePricing = tracked({
  notionals: ["100"],
  quotes: [
    ["gemini", "buy", "100"],
    ["gemini", "sell", "105"],
    ["coinbase", "buy", "101"],
    ["coinbase", "sell", "104"],
    ["kraken", "buy", "99"],
    ["kraken", "sell", "103"],
  ].map(([exchange, side, vwap]) => ({
    exchange,
    pair: "BTC-USD",
    side: side as "buy" | "sell",
    notional: "100",
    vwap,
    insufficient_depth: false,
    filled_notional: "100",
    filled_base: "1",
    levels_used: 1,
    subscribed_depth_levels: null,
  })),
  routes: [
    {
      pair: "BTC-USD",
      buy_exchange: "gemini",
      sell_exchange: "coinbase",
      notional: "100",
      top_of_book_spread_pct: "3",
      buy_vwap: "100",
      sell_vwap: "104",
      gross_executable_spread_pct: "4",
      depth_impact_pct: "0",
      buy_taker_fee_pct: "0.1",
      sell_taker_fee_pct: "0.1",
      fee_impact_pct: "-0.2",
      net_executable_spread_pct: "3.8",
      insufficient_depth: false,
      buy_age_ms: 10,
      sell_age_ms: 12,
      age_skew_ms: 2,
    },
    {
      pair: "BTC-USD",
      buy_exchange: "kraken",
      sell_exchange: "gemini",
      notional: "100",
      top_of_book_spread_pct: "6",
      buy_vwap: "99",
      sell_vwap: "105",
      gross_executable_spread_pct: "6",
      depth_impact_pct: "0",
      buy_taker_fee_pct: "2.5",
      sell_taker_fee_pct: "2.5",
      fee_impact_pct: "-5",
      net_executable_spread_pct: "1",
      insufficient_depth: false,
      buy_age_ms: 10,
      sell_age_ms: 12,
      age_skew_ms: 2,
    },
  ],
});

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
        pricing={ready(tracked(pricing))}
        opportunities={ready([opportunity()])}
        nowMs={1_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText(/Route: buy coinbase → sell gemini/)).toBeInTheDocument();
    expect(screen.getByText(/Gross 2.000%/)).toBeInTheDocument();
    expect(screen.getByText(/Net 1.000%/)).toBeInTheDocument();
    expect(screen.getByText("Pair lifetime: 100ms")).toBeInTheDocument();
    expect(screen.getByText("cheapest buy")).toBeInTheDocument();
    expect(screen.getByText("best sell")).toBeInTheDocument();
    expect(screen.getByText("insufficient depth", { selector: "td" })).toBeInTheDocument();
    expect(screen.getAllByText("unavailable", { selector: "td" }).length).toBeGreaterThan(0);
  });

  it("recomputes the table for the selected configured notional", () => {
    render(
      <VenueComparison
        pairs={[{ exchange: "gemini", pair: "BTC-USD" }]}
        statuses={{ "gemini:BTC-USD": status("gemini", true) }}
        pricing={ready(tracked(pricing))}
        opportunities={ready([])}
        nowMs={1_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );

    fireEvent.change(screen.getByRole("combobox", { name: "Quote notional" }), {
      target: { value: "1000" },
    });
    expect(screen.getByText("110.0000")).toBeInTheDocument();
    expect(screen.getByText("111.0000")).toBeInTheDocument();
  });

  it("gates the displayed route by the same status used by venue cells", () => {
    const healthy = {
      "coinbase:BTC-USD": status("coinbase", true),
      "gemini:BTC-USD": status("gemini", true),
      "kraken:BTC-USD": status("kraken", true),
    };
    const { rerender } = render(
      <VenueComparison
        pairs={pairs}
        statuses={{ ...healthy, "gemini:BTC-USD": status("gemini", false) }}
        pricing={ready(threeVenuePricing)}
        opportunities={ready([])}
        nowMs={1_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("Route: unavailable")).toBeInTheDocument();
    const geminiRow = screen.getByText("gemini", { selector: "th" }).closest("tr");
    expect(geminiRow).not.toBeNull();
    expect(within(geminiRow as HTMLElement).getAllByText("unavailable")).toHaveLength(4);
    expect(screen.getByText("99.0000")).toBeInTheDocument();

    rerender(
      <VenueComparison
        pairs={pairs}
        statuses={healthy}
        pricing={ready(threeVenuePricing)}
        opportunities={ready([])}
        nowMs={1_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText(/Route: buy gemini → sell coinbase/)).toBeInTheDocument();
  });

  it.each(["crossed", "incomplete"])(
    "treats a %s book as unavailable for route pricing",
    (reason) => {
      render(
        <VenueComparison
          pairs={pairs}
          statuses={{
            "coinbase:BTC-USD": status("coinbase", true),
            "gemini:BTC-USD": status("gemini", false, { reason }),
            "kraken:BTC-USD": status("kraken", true),
          }}
          pricing={ready(threeVenuePricing)}
          opportunities={ready([])}
          nowMs={1_000}
          feedLive={true}
          onRetry={() => undefined}
        />,
      );
      expect(screen.getByText("Route: unavailable")).toBeInTheDocument();
    },
  );

  it("expires an old eligible status for both the route and its venue cells", () => {
    render(
      <VenueComparison
        pairs={pairs}
        statuses={{
          "coinbase:BTC-USD": status("coinbase", true),
          "gemini:BTC-USD": status("gemini", true, {
            receivedAtMs: 0,
            max_age_ms: 1_000,
          }),
          "kraken:BTC-USD": status("kraken", true),
        }}
        pricing={ready(threeVenuePricing)}
        opportunities={ready([])}
        nowMs={5_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("Route: unavailable")).toBeInTheDocument();
    const geminiRow = screen.getByText("gemini", { selector: "th" }).closest("tr");
    expect(within(geminiRow as HTMLElement).getAllByText("unavailable")).toHaveLength(4);
  });

  it("shows stale pricing without showing cached route economics", () => {
    render(
      <VenueComparison
        pairs={pairs}
        statuses={{
          "coinbase:BTC-USD": status("coinbase", true),
          "gemini:BTC-USD": status("gemini", true),
          "kraken:BTC-USD": status("kraken", true),
        }}
        pricing={ready(tracked(threeVenuePricing, 0))}
        opportunities={ready([])}
        nowMs={12_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("Route: pricing stale")).toBeInTheDocument();
    expect(screen.queryByText(/Gross/)).not.toBeInTheDocument();
  });

  it("keeps independent per-side best badges separate from the selected route", () => {
    render(
      <VenueComparison
        pairs={pairs}
        statuses={{
          "coinbase:BTC-USD": status("coinbase", true),
          "gemini:BTC-USD": status("gemini", true),
          "kraken:BTC-USD": status("kraken", true),
        }}
        pricing={ready(threeVenuePricing)}
        opportunities={ready([])}
        nowMs={1_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText(/Route: buy gemini → sell coinbase/)).toBeInTheDocument();
    expect(screen.getByText("cheapest buy")).toBeInTheDocument();
    expect(screen.getByText("best sell")).toBeInTheDocument();
    expect(screen.getByText("kraken", { selector: "th" })).toHaveTextContent(
      "cheapest buy",
    );
    expect(screen.getByText("gemini", { selector: "th" })).toHaveTextContent("best sell");
  });

  it("allows the same venue to be both per-side bests", () => {
    const sameVenuePricing = tracked({
      ...threeVenuePricing,
      quotes: threeVenuePricing.quotes.map((quote) =>
        quote.exchange === "kraken" && quote.side === "sell"
          ? { ...quote, vwap: "106" }
          : quote,
      ),
    });
    render(
      <VenueComparison
        pairs={pairs}
        statuses={{
          "coinbase:BTC-USD": status("coinbase", true),
          "gemini:BTC-USD": status("gemini", true),
          "kraken:BTC-USD": status("kraken", true),
        }}
        pricing={ready(sameVenuePricing)}
        opportunities={ready([])}
        nowMs={1_000}
        feedLive={true}
        onRetry={() => undefined}
      />,
    );
    const krakenRow = screen.getByText("kraken", { selector: "th" });
    expect(krakenRow).toHaveTextContent("cheapest buy");
    expect(krakenRow).toHaveTextContent("best sell");
    expect(screen.getByText(/Route: buy gemini → sell coinbase/)).toBeInTheDocument();
  });

  it("makes a down feed unavailable even when cached pricing is fresh", () => {
    render(
      <VenueComparison
        pairs={pairs}
        statuses={{
          "coinbase:BTC-USD": status("coinbase", true),
          "gemini:BTC-USD": status("gemini", true),
          "kraken:BTC-USD": status("kraken", true),
        }}
        pricing={ready(tracked(threeVenuePricing))}
        opportunities={ready([])}
        nowMs={1_000}
        feedLive={false}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("Route: unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Route: pricing stale")).not.toBeInTheDocument();
  });
});
