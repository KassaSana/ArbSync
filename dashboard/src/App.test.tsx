import type { ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

// Keep the real application routes, navigation, and lazy loading; isolate market data.
vi.mock("./state/live", () => ({
  LiveProvider: ({ children }: { children: ReactNode }) => children,
  useLive: () => ({ status: "connected", lastTickAgeMs: 0, invalidFrameCount: 0 }),
}));
vi.mock("./pages/Dashboard", () => ({
  default: () => <h1>Dashboard content</h1>,
}));
vi.mock("./pages/Statistics", () => ({
  default: () => <h1>Statistics content</h1>,
}));

afterEach(() => window.history.replaceState(null, "", "/"));

describe("application navigation", () => {
  it("navigates between dashboard and lazy statistics with the correct active link", async () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "Dashboard content" })).toBeInTheDocument();
    const dashboard = screen.getByRole("link", { name: "Dashboard" });
    const statistics = screen.getByRole("link", { name: "Statistics" });
    expect(dashboard).toHaveAttribute("aria-current", "page");

    fireEvent.click(statistics);
    expect(await screen.findByRole("heading", { name: "Statistics content" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/stats");
    expect(statistics).toHaveAttribute("aria-current", "page");
    expect(dashboard).not.toHaveAttribute("aria-current");

    fireEvent.click(dashboard);
    expect(await screen.findByRole("heading", { name: "Dashboard content" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(dashboard).toHaveAttribute("aria-current", "page");
    expect(statistics).not.toHaveAttribute("aria-current");
  });

  it("opens statistics directly from its URL", async () => {
    window.history.replaceState(null, "", "/stats");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Statistics content" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Dashboard content" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Statistics" })).toHaveAttribute("aria-current", "page");
  });
});
