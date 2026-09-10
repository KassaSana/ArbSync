import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OpportunityFeed } from "./OpportunityFeed";

describe("opportunity feed", () => {
  it("shows each opportunity in its own quote asset", () => {
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

    // USD and USDT are distinct quote assets; neither total may be rendered in
    // the other's units.
    expect(screen.getByText("$2.50")).toBeInTheDocument();
    expect(screen.getByText("3.50 USDT")).toBeInTheDocument();
  });
});
