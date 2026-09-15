import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Timeseries } from "../api/client";
import { failed, loading, ready } from "../lib/async";
import { OpportunitiesChart } from "./OpportunitiesChart";

function series(counts: number[]): Timeseries {
  return {
    window: "1h",
    bucket_seconds: 60,
    points: counts.map((count, index) => ({
      bucket_start_ns: `${BigInt(1_700_000_000_000 + index * 60_000) * 1_000_000n}`,
      count,
      max_spread_pct: "0.5",
    })),
  };
}

describe("opportunities chart", () => {
  it("offers a retry when the timeseries request failed", () => {
    const onRetry = vi.fn();
    render(
      <OpportunitiesChart
        data={failed(new Error("boom"))}
        window="1h"
        windowLabel="1 hour"
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText("Could not load the timeseries")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders a loading panel", () => {
    render(
      <OpportunitiesChart data={loading()} window="1h" windowLabel="1 hour" onRetry={vi.fn()} />,
    );
    expect(screen.getByText("Loading timeseries.")).toBeInTheDocument();
  });

  it.each([
    ["no buckets", series([])],
    ["only empty buckets", series([0, 0, 0])],
  ])("treats %s as an empty window without a retry", (_case, data) => {
    render(
      <OpportunitiesChart data={ready(data)} window="1h" windowLabel="1 hour" onRetry={vi.fn()} />,
    );

    expect(screen.getByText("No opportunities recorded in this window")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("refuses to plot a single bucket as a trend", () => {
    render(
      <OpportunitiesChart data={ready(series([7]))} window="1h" windowLabel="1 hour" onRetry={vi.fn()} />,
    );

    expect(
      screen.getByText("7 opportunities so far, all inside one bucket"),
    ).toBeInTheDocument();
  });

  it("titles the chart by window and summarizes buckets and detections", () => {
    render(
      <OpportunitiesChart
        data={ready(series([3, 0, 4]))}
        window="1d"
        windowLabel="1 day"
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByText("Opportunities over the last 1 day")).toBeInTheDocument();
    expect(screen.getByText("3 buckets · 7 detections")).toBeInTheDocument();
  });
});
