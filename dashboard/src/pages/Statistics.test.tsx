import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SystemOverview, Timeseries, WindowStats } from "../api/client";
import Statistics from "./Statistics";

const api = vi.hoisted(() => ({
  fetchSystemOverview: vi.fn(),
  fetchSystemStats: vi.fn(),
  fetchSystemTimeseries: vi.fn(),
}));

vi.mock("../api/client", () => api);
// UptimeCard borrows the provider's clock; fix it so uptime is deterministic.
vi.mock("../state/live", () => ({ useLive: () => ({ nowMs: 1_800_000_000_000 }) }));

const overview: SystemOverview = {
  started_at_ns: `${BigInt(1_800_000_000_000 - 3_725_000) * 1_000_000n}`,
  uptime_seconds: 3_725,
  all_time_count: 12_345,
  all_time_max_spread_pct: "1.25",
  all_time_peak_minute: { minute_start_ns: "1700000000000000000", count: 42 },
  open_count: 1,
  all_time_lifetime: null,
};

const windowStats: WindowStats = {
  window: "1h",
  count: 17,
  max_spread_pct: "0.9",
  mean_spread_pct: "0.3",
  theoretical_profit_by_quote: { USD: "5" },
  top_pair: "ETH-USD",
  peak_minute: null,
  lifetime: { closed_count: 12, p50_seconds: 2.5, p90_seconds: 75, max_seconds: 3_700 },
};

const timeseries: Timeseries = { window: "1h", bucket_seconds: 60, points: [] };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("statistics page", () => {
  beforeEach(() => {
    api.fetchSystemOverview.mockReset().mockResolvedValue(overview);
    api.fetchSystemStats.mockReset().mockResolvedValue(windowStats);
    api.fetchSystemTimeseries.mockReset().mockResolvedValue(timeseries);
  });

  afterEach(() => vi.useRealTimers());

  it("starts loading, then renders overview, window stats, and the chart state", async () => {
    render(<Statistics />);

    expect(screen.getByText("Loading window stats.")).toBeInTheDocument();
    expect(screen.getByText("Loading timeseries.")).toBeInTheDocument();

    expect(await screen.findByText("12,345")).toBeInTheDocument();
    expect(screen.getByText("System uptime")).toBeInTheDocument();
    expect(screen.getByText("1h 2m 5s")).toBeInTheDocument();
    expect(screen.getByText("1.250%")).toBeInTheDocument();
    expect(screen.getByText("ETH-USD")).toBeInTheDocument();
    expect(screen.getByText("17")).toBeInTheDocument();
    // Lifetime p50 / p90 from the window stats, with max and closed count beneath.
    expect(screen.getByText("2.5s / 1m")).toBeInTheDocument();
    expect(screen.getByText("max 1h over 12 closed")).toBeInTheDocument();
    expect(screen.getByText("No opportunities recorded in this window")).toBeInTheDocument();
    expect(api.fetchSystemStats).toHaveBeenCalledWith("1h");
    expect(api.fetchSystemTimeseries).toHaveBeenCalledWith("1h", 60);
  });

  it("refetches every series for the selected window", async () => {
    render(<Statistics />);
    await screen.findByText("12,345");

    fireEvent.click(screen.getByRole("button", { name: "1 day" }));

    expect(screen.getByRole("button", { name: "1 day" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "1 hour" })).toHaveAttribute("aria-pressed", "false");
    expect(api.fetchSystemStats).toHaveBeenLastCalledWith("1d");
    expect(api.fetchSystemTimeseries).toHaveBeenLastCalledWith("1d", 900);
  });

  it("ignores an older window response that resolves after the selected window", async () => {
    const oldStats = deferred<WindowStats>();
    const oldSeries = deferred<Timeseries>();
    const nextStats = deferred<WindowStats>();
    const nextSeries = deferred<Timeseries>();
    api.fetchSystemStats
      .mockReturnValueOnce(oldStats.promise)
      .mockReturnValueOnce(nextStats.promise);
    api.fetchSystemTimeseries
      .mockReturnValueOnce(oldSeries.promise)
      .mockReturnValueOnce(nextSeries.promise);

    render(<Statistics />);
    fireEvent.click(screen.getByRole("button", { name: "1 day" }));

    expect(screen.getByText("Loading window stats.")).toBeInTheDocument();
    await act(async () => {
      nextStats.resolve({ ...windowStats, window: "1d", count: 24 });
      nextSeries.resolve({ ...timeseries, window: "1d", bucket_seconds: 900 });
    });
    expect(await screen.findByText("24")).toBeInTheDocument();

    await act(async () => {
      oldStats.resolve({ ...windowStats, count: 1 });
      oldSeries.resolve(timeseries);
    });
    expect(screen.getByText("24")).toBeInTheDocument();
    expect(screen.queryByText("1")).not.toBeInTheDocument();
  });

  it("does not start another polling group while the current group is pending", async () => {
    vi.useFakeTimers();
    api.fetchSystemOverview.mockReturnValue(new Promise(() => undefined));
    api.fetchSystemStats.mockReturnValue(new Promise(() => undefined));
    api.fetchSystemTimeseries.mockReturnValue(new Promise(() => undefined));

    render(<Statistics />);
    await act(async () => vi.advanceTimersByTime(15_000));

    expect(api.fetchSystemOverview).toHaveBeenCalledTimes(1);
    expect(api.fetchSystemStats).toHaveBeenCalledTimes(1);
    expect(api.fetchSystemTimeseries).toHaveBeenCalledTimes(1);
  });

  it("reports an overview failure with a retry and keeps the window controls", async () => {
    api.fetchSystemOverview.mockRejectedValue(new Error("overview 502"));
    render(<Statistics />);

    expect(await screen.findByText("Could not load the system overview")).toBeInTheDocument();
    expect(screen.getByText("overview 502")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Time window" })).toBeInTheDocument();

    api.fetchSystemOverview.mockResolvedValue(overview);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("12,345")).toBeInTheDocument();
  });
});
