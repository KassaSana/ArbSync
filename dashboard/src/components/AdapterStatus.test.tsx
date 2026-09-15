import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AdapterStatus, BookStatus } from "../api/client";
import { failed, loading, ready } from "../lib/async";
import { AdapterStatusBanner } from "./AdapterStatus";

function adapter(overrides: Partial<AdapterStatus> = {}): AdapterStatus {
  return {
    exchange: "gemini",
    connected: true,
    last_message_age_ms: 250,
    gap_count: 0,
    reconnect_count: 0,
    last_error: null,
    ...overrides,
  };
}

function book(pair: string, eligible: boolean, exchange = "gemini"): BookStatus {
  return {
    exchange,
    pair,
    initialized: true,
    continuous: true,
    connected: true,
    age_ms: 10,
    max_age_ms: 60_000,
    eligible,
    reason: eligible ? null : "stale",
  };
}

const twoLiveBooks = {
  "gemini:BTC-USD": book("BTC-USD", true),
  "gemini:ETH-USD": book("ETH-USD", true),
};

describe("adapter status banner", () => {
  it("shows a retryable failure when the health endpoint is unreachable", () => {
    const onRetry = vi.fn();
    render(
      <AdapterStatusBanner
        adapters={failed(new Error("503 from /api/adapters"))}
        books={{}}
        feedLive
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText("Cannot reach the adapter health endpoint")).toBeInTheDocument();
    expect(screen.getByText("503 from /api/adapters")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("shows a loading state and an empty roster distinctly", () => {
    const { rerender } = render(
      <AdapterStatusBanner adapters={loading()} books={{}} feedLive onRetry={() => undefined} />,
    );
    expect(screen.getByText("Checking adapters.")).toBeInTheDocument();

    rerender(
      <AdapterStatusBanner adapters={ready([])} books={{}} feedLive onRetry={() => undefined} />,
    );
    expect(screen.getByText("No adapters reported.")).toBeInTheDocument();
    expect(screen.getByText("0 venues nominal")).toBeInTheDocument();
  });

  it.each<[string, Partial<AdapterStatus>, Record<string, BookStatus>, string]>([
    ["disconnected socket", { connected: false }, twoLiveBooks, "Disconnected"],
    ["no message yet", { last_message_age_ms: null }, twoLiveBooks, "Waiting for first message"],
    ["silent for too long", { last_message_age_ms: 30_001 }, twoLiveBooks, "Stale"],
    ["no books", {}, {}, "Rebuilding books"],
    [
      "every book ineligible",
      {},
      { "gemini:BTC-USD": book("BTC-USD", false) },
      "Rebuilding books",
    ],
    [
      "some books ineligible",
      {},
      { "gemini:BTC-USD": book("BTC-USD", true), "gemini:ETH-USD": book("ETH-USD", false) },
      "Partially eligible",
    ],
    ["recent reconnect", { reconnect_count: 1 }, twoLiveBooks, "Recovered from interruption"],
    ["recent gap", { gap_count: 2 }, twoLiveBooks, "Recovered from interruption"],
    ["slow but connected", { last_message_age_ms: 5_001 }, twoLiveBooks, "Recovered from interruption"],
    ["healthy", {}, twoLiveBooks, "Live"],
  ])("labels %s as %s", (_case, overrides, books, label) => {
    render(
      <AdapterStatusBanner
        adapters={ready([adapter(overrides)])}
        books={books}
        feedLive
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("counts venues needing attention and only the venue's own books", () => {
    render(
      <AdapterStatusBanner
        adapters={ready([
          adapter(),
          adapter({ exchange: "coinbase", connected: false, last_error: "handshake timeout" }),
        ])}
        books={{
          ...twoLiveBooks,
          "coinbase:BTC-USD": book("BTC-USD", true, "coinbase"),
        }}
        feedLive
        onRetry={() => undefined}
      />,
    );

    expect(screen.getByText("1 of 2 venues need attention")).toBeInTheDocument();
    expect(screen.getByText("2/2")).toBeInTheDocument();
    expect(screen.getByText("1/1")).toBeInTheDocument();
    expect(screen.getByText("handshake timeout")).toBeInTheDocument();
  });
});
