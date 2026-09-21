import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { failed, loading, ready } from "../lib/async";
import Dashboard from "./Dashboard";

type LiveValue = ReturnType<typeof import("../state/live").useLive>;

const live = vi.hoisted(() => ({ current: {} }));

vi.mock("../state/live", () => ({
  useLive: () => live.current,
}));

function liveValue(overrides: Partial<LiveValue> = {}): LiveValue {
  return {
    status: "connected",
    feedLive: true,
    nowMs: 1_000,
    lastTickAgeMs: 50,
    invalidFrameCount: 0,
    books: {},
    bookStatuses: {},
    opportunities: ready([]),
    stats: loading(),
    pairs: ready([{ exchange: "gemini", pair: "BTC-USD" }]),
    adapters: loading(),
    depthPricing: loading(),
    refreshStats: vi.fn(),
    refreshOpportunities: vi.fn(),
    refreshAdapters: vi.fn(),
    refreshPairs: vi.fn(),
    refreshDepthPricing: vi.fn(),
    ...overrides,
  };
}

describe("dashboard page", () => {
  beforeEach(() => {
    live.current = liveValue();
  });

  it("composes every panel and shows no stale notice while the feed is live", () => {
    render(<Dashboard />);

    expect(screen.getByText("Checking adapters.")).toBeInTheDocument();
    expect(screen.getByText("BTC-USD")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps the last values on screen and says how old they are when the feed drops", () => {
    live.current = liveValue({ feedLive: false, lastTickAgeMs: 90_000 });
    render(<Dashboard />);

    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent("Live feed interrupted");
    expect(notice).toHaveTextContent("1m ago");
    expect(screen.getByText("BTC-USD")).toBeInTheDocument();
  });

  it("distinguishes a failed pair list from an empty one", () => {
    const refreshPairs = vi.fn();
    live.current = liveValue({ pairs: failed(new Error("pairs 500")), refreshPairs });
    const { rerender } = render(<Dashboard />);

    expect(screen.getByText("Could not load the tracked pair list")).toBeInTheDocument();
    expect(screen.getByText("pairs 500")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(refreshPairs).toHaveBeenCalledTimes(1);

    live.current = liveValue({ pairs: ready([]), refreshPairs });
    rerender(<Dashboard />);
    expect(screen.getByText("No pairs are being tracked yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(refreshPairs).toHaveBeenCalledTimes(2);
  });
});
