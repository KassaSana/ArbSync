import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

function Panel({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) {
    throw new Error("chart exploded");
  }
  return <p>panel content</p>;
}

const swallowWindowError = (event: ErrorEvent): void => event.preventDefault();

describe("error boundary", () => {
  beforeEach(() => {
    // React logs the caught error through console.error and jsdom re-reports
    // it as an uncaught window error; silence both so the test asserts only
    // the boundary's own report.
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    window.addEventListener("error", swallowWindowError);
  });

  afterEach(() => {
    window.removeEventListener("error", swallowWindowError);
    vi.restoreAllMocks();
  });

  it("renders children while nothing throws", () => {
    render(
      <ErrorBoundary label="Spreads">
        <Panel shouldThrow={false} />
      </ErrorBoundary>,
    );

    expect(screen.getByText("panel content")).toBeInTheDocument();
    expect(screen.queryByText(/stopped rendering/)).not.toBeInTheDocument();
  });

  it("contains a render failure to a labelled fallback and reports it", () => {
    render(
      <ErrorBoundary label="Spreads">
        <Panel shouldThrow />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Spreads stopped rendering")).toBeInTheDocument();
    expect(screen.getByText("chart exploded")).toBeInTheDocument();
    expect(screen.queryByText("panel content")).not.toBeInTheDocument();
    expect(console.error).toHaveBeenCalledWith(
      "[Spreads] render failed",
      expect.any(Error),
      expect.any(String),
    );
  });

  it("retries the panel once the cause is gone", () => {
    const { rerender } = render(
      <ErrorBoundary label="Spreads">
        <Panel shouldThrow />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Spreads stopped rendering")).toBeInTheDocument();

    // The retry alone re-renders the same failing child and lands back on the
    // fallback; it only helps once the underlying cause has changed.
    fireEvent.click(screen.getByRole("button", { name: "Retry this panel" }));
    expect(screen.getByText("Spreads stopped rendering")).toBeInTheDocument();

    rerender(
      <ErrorBoundary label="Spreads">
        <Panel shouldThrow={false} />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry this panel" }));

    expect(screen.getByText("panel content")).toBeInTheDocument();
    expect(screen.queryByText(/stopped rendering/)).not.toBeInTheDocument();
  });
});
