import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OpportunityFeed } from "./OpportunityFeed";
import { StatsCards } from "./StatsCards";

describe("quote-denominated profit display", () => {
  it("shows each opportunity in its quote asset", () => {
    render(
      <OpportunityFeed
        opportunities={{
          state: "ready",
          data: [
            {
              timestamp_ns: "1",
              pair: "BTC-USD",
              quote_asset: "USD",
              buy_exchange: "gemini",
              sell_exchange: "coinbase",
              buy_price: "100",
              sell_price: "101",
              spread_pct: "1",
              max_size: "1",
              theoretical_profit: "2.5",
            },
            {
              timestamp_ns: "2",
              pair: "BTC-USDT",
              quote_asset: "USDT",
              buy_exchange: "binance",
              sell_exchange: "other",
              buy_price: "100",
              sell_price: "101",
              spread_pct: "1",
              max_size: "1",
              theoretical_profit: "3.5",
            },
          ],
        }}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("$2.50")).toBeInTheDocument();
    expect(screen.getByText("3.50 USDT")).toBeInTheDocument();
  });

  it("keeps grouped quote totals separate", () => {
    render(
      <StatsCards
        stats={{
          state: "ready",
          data: {
            count: 2,
            max_spread_pct: "1",
            theoretical_profit_by_quote: { USD: "2.5", USDT: "3.5" },
          },
        }}
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("Theoretical profit (USD, 1h)")).toBeInTheDocument();
    expect(screen.getByText("Theoretical profit (USDT, 1h)")).toBeInTheDocument();
    expect(screen.getByText("$2.50")).toBeInTheDocument();
    expect(screen.getByText("3.50 USDT")).toBeInTheDocument();
  });
});
