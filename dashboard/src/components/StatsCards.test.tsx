import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatsCards } from "./StatsCards";

describe("stats cards", () => {
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
