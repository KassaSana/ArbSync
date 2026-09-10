import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BookStatus, PairRecord, TopOfBook } from "../api/client";
import { LiveSpreads } from "./LiveSpreads";

const NOW_MS = 1_000_000;
const TIMESTAMP_NS = String(BigInt(NOW_MS) * 1_000_000n);

function book(exchange: string, bid: string, ask: string): TopOfBook {
  return {
    exchange,
    pair: "BTC-USD",
    best_bid_price: bid,
    best_bid_size: "1",
    best_ask_price: ask,
    best_ask_size: "1",
    sequence: 1,
    timestamp_ns: TIMESTAMP_NS,
  };
}

function status(exchange: string, eligible: boolean): BookStatus {
  return {
    exchange,
    pair: "BTC-USD",
    initialized: true,
    continuous: true,
    connected: eligible,
    age_ms: 0,
    max_age_ms: 60_000,
    eligible,
    reason: eligible ? null : "disconnected",
  };
}

const PAIRS: PairRecord[] = [
  { exchange: "gemini", pair: "BTC-USD" },
  { exchange: "coinbase", pair: "BTC-USD" },
];

function renderSpreads(statuses: BookStatus[]) {
  // A wide, obviously-arbitrageable cross: gemini bids 110 while coinbase asks 100.
  const books = {
    "gemini:BTC-USD": book("gemini", "110", "111"),
    "coinbase:BTC-USD": book("coinbase", "99", "100"),
  };
  render(
    <LiveSpreads
      pairs={PAIRS}
      books={books}
      statuses={Object.fromEntries(statuses.map((s) => [`${s.exchange}:${s.pair}`, s]))}
      nowMs={NOW_MS}
      feedLive
    />,
  );
  const row = screen.getByRole("row", { name: /BTC-USD/ });
  return within(row).getAllByRole("cell");
}

describe("live spreads", () => {
  it("derives a spread when both venues are eligible", () => {
    const cells = renderSpreads([status("gemini", true), status("coinbase", true)]);
    // cells: venues, best bid, best ask, spread, age
    expect(cells[1]).toHaveTextContent("110.0000");
    expect(cells[2]).toHaveTextContent("100.0000");
    expect(cells[3]).toHaveTextContent("10.000%");
  });

  it("ignores an ineligible book instead of pricing off its stale quote", () => {
    const cells = renderSpreads([status("gemini", true), status("coinbase", false)]);
    // One eligible venue cannot produce a cross-exchange spread, so every
    // derived column must fall back to a dash rather than reusing the last
    // quote the dropped venue happened to leave behind.
    expect(cells[1]).toHaveTextContent("—");
    expect(cells[2]).toHaveTextContent("—");
    expect(cells[3]).toHaveTextContent("—");
  });

  it("reports no spread when a status has not arrived for a venue", () => {
    const cells = renderSpreads([status("gemini", true)]);
    expect(cells[3]).toHaveTextContent("—");
  });
});
