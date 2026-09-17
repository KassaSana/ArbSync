import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Opportunity } from "../api/client";
import { OpportunityFeed } from "./OpportunityFeed";

function episode(overrides: Partial<Opportunity>): Opportunity {
  return {
    start_ns: "1",
    end_ns: null,
    duration_ns: null,
    pair: "BTC-USD",
    quote_asset: "USD",
    buy_exchange: "gemini",
    sell_exchange: "coinbase",
    buy_price: "100",
    sell_price: "101",
    spread_pct: "1",
    max_size: "1",
    theoretical_profit: "2.5",
    peak_spread_pct: "1",
    peak_size: "1",
    peak_profit: "2.5",
    pricing_ledgers: [],
    close_spread_pct: null,
    close_reason: null,
    ...overrides,
  };
}

describe("opportunity feed", () => {
  it("shows each episode's peak profit in its own quote asset", () => {
    render(
      <OpportunityFeed
        opportunities={{
          state: "ready",
          data: [
            episode({ theoretical_profit: "1", peak_profit: "2.5" }),
            episode({
              start_ns: "2",
              pair: "BTC-USDT",
              quote_asset: "USDT",
              buy_exchange: "binance",
              sell_exchange: "other",
              theoretical_profit: "3.5",
              peak_profit: "3.5",
            }),
          ],
        }}
        onRetry={() => undefined}
      />,
    );

    // USD and USDT are distinct quote assets; neither total may be rendered in
    // the other's units, and the peak is what is shown, not the open value.
    expect(screen.getByText("$2.50")).toBeInTheDocument();
    expect(screen.queryByText("$1.00")).not.toBeInTheDocument();
    expect(screen.getByText("3.50 USDT")).toBeInTheDocument();
    expect(screen.getByText("2 episodes")).toBeInTheDocument();
  });

  it("shows an open episode as open and a closed one by its lifetime", () => {
    render(
      <OpportunityFeed
        opportunities={{
          state: "ready",
          data: [
            episode({ start_ns: "2" }),
            episode({
              start_ns: "1",
              end_ns: "4500000001",
              duration_ns: "4500000000",
              close_spread_pct: "0",
              close_reason: "book_ineligible",
            }),
          ],
        }}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("4.5s")).toHaveAttribute("title", "book dropped");
  });

  it("shows stored net executable spread separately from the theoretical peak", () => {
    render(
      <OpportunityFeed
        opportunities={{
          state: "ready",
          data: [
            episode({
              peak_spread_pct: "1.5",
              pricing_ledgers: [
                {
                  notional: "1000",
                  top_of_book_spread_pct: "1.5",
                  buy_vwap: "100",
                  sell_vwap: "100.8",
                  gross_executable_spread_pct: "0.8",
                  depth_impact_pct: "-0.7",
                  buy_taker_fee_pct: "0.4",
                  sell_taker_fee_pct: "0.6",
                  fee_impact_pct: "-1.006",
                  net_executable_spread_pct: "-0.206",
                  insufficient_depth: false,
                },
              ],
            }),
          ],
        }}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("1.500%")).toBeInTheDocument();
    expect(screen.getByText("-0.206%")).toHaveAttribute(
      "title",
      "1000 USD; includes measured depth impact and configured taker fees",
    );
  });
});
