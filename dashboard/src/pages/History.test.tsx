import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Opportunity, OpportunityHistoryPage } from "../api/client";
import History, { localInputToNs } from "./History";

const api = vi.hoisted(() => ({
  fetchOpportunityHistory: vi.fn(),
  opportunityExportUrl: vi.fn(
    (filters: Record<string, string>) =>
      `/api/opportunities/export?${new URLSearchParams(filters).toString()}`,
  ),
}));

vi.mock("../api/client", () => api);
vi.mock("../state/live", () => ({
  useLive: () => ({
    pairs: {
      state: "ready",
      data: [
        { exchange: "coinbase", pair: "BTC-USD" },
        { exchange: "gemini", pair: "BTC-USD" },
        { exchange: "gemini", pair: "ETH-USD" },
      ],
    },
  }),
}));

function episode(startNs: string, pair = "BTC-USD"): Opportunity {
  return {
    start_ns: startNs,
    end_ns: null,
    duration_ns: null,
    pair,
    quote_asset: "USD",
    buy_exchange: "gemini",
    sell_exchange: "coinbase",
    buy_price: "100",
    sell_price: "101",
    spread_pct: "1",
    max_size: "1",
    theoretical_profit: "1",
    peak_spread_pct: "1",
    peak_size: "1",
    peak_profit: "1",
    pricing_ledgers: [],
    close_spread_pct: null,
    close_reason: null,
  };
}

function page(items: Opportunity[], nextCursor: string | null): OpportunityHistoryPage {
  return { items, next_cursor: nextCursor, order: "start_ns_desc_id_desc" };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((next, fail) => {
    resolve = next;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const rowCount = () => screen.getAllByRole("rowheader").length;

describe("history page", () => {
  beforeEach(() => {
    api.fetchOpportunityHistory.mockReset();
  });

  it("loads the newest page and appends the next one through its cursor", async () => {
    api.fetchOpportunityHistory
      .mockResolvedValueOnce(page([episode("1720000002000000000")], "cursor-1"))
      .mockResolvedValueOnce(page([episode("1720000001000000000", "ETH-USD")], null));

    render(<History />);
    expect(screen.getByText("Loading opportunity history.")).toBeInTheDocument();
    expect(await screen.findByText("1 shown, more available")).toBeInTheDocument();
    expect(api.fetchOpportunityHistory).toHaveBeenLastCalledWith({}, null, 100);

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("2 shown")).toBeInTheDocument();
    expect(api.fetchOpportunityHistory).toHaveBeenLastCalledWith({}, "cursor-1", 100);
    expect(rowCount()).toBe(2);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("applies filters as a new traversal and ignores the previous one's late pages", async () => {
    const stale = deferred<OpportunityHistoryPage>();
    api.fetchOpportunityHistory
      .mockResolvedValueOnce(page([episode("1720000002000000000")], "cursor-1"))
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(page([episode("1720000003000000000", "ETH-USD")], null));

    render(<History />);
    await screen.findByText("1 shown, more available");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    const form = screen.getByRole("form", { name: "History filters" });
    fireEvent.change(within(form).getByLabelText("Pair"), { target: { value: "ETH-USD" } });
    fireEvent.change(within(form).getByLabelText("Buy venue"), { target: { value: "gemini" } });
    fireEvent.change(within(form).getByLabelText("State"), { target: { value: "closed" } });
    fireEvent.click(within(form).getByRole("button", { name: "Apply" }));

    const filters = { pair: "ETH-USD", buy_exchange: "gemini", state: "closed" };
    expect(api.fetchOpportunityHistory).toHaveBeenLastCalledWith(filters, null, 100);
    expect(await screen.findByText("1 shown")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "ETH-USD" })).toBeInTheDocument();

    await act(async () => {
      stale.resolve(page([episode("1720000001000000000", "BTC-USD")], "cursor-2"));
      await stale.promise;
    });
    expect(rowCount()).toBe(1);
    expect(screen.queryByRole("rowheader", { name: "BTC-USD" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export JSONL" })).toHaveAttribute(
      "href",
      "/api/opportunities/export?pair=ETH-USD&buy_exchange=gemini&state=closed",
    );
  });

  it("converts the time range and rejects an empty or reversed one without a request", async () => {
    api.fetchOpportunityHistory.mockResolvedValue(page([], null));
    render(<History />);
    await screen.findByText("No stored episodes match these filters.");
    api.fetchOpportunityHistory.mockClear();

    const form = screen.getByRole("form", { name: "History filters" });
    fireEvent.change(within(form).getByLabelText("Started from"), {
      target: { value: "2024-07-03T10:30" },
    });
    fireEvent.change(within(form).getByLabelText("Started before"), {
      target: { value: "2024-07-03T10:30" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert")).toHaveTextContent("must be before its end");
    expect(api.fetchOpportunityHistory).not.toHaveBeenCalled();

    fireEvent.change(within(form).getByLabelText("Started before"), {
      target: { value: "2024-07-03T11:30" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(api.fetchOpportunityHistory).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const [filters] = api.fetchOpportunityHistory.mock.calls[0] as [Record<string, string>];
    expect(BigInt(filters.to_ns) - BigInt(filters.from_ns)).toBe(3_600_000_000_000n);
    expect(filters.from_ns).toBe(localInputToNs("2024-07-03T10:30"));
  });

  it("keeps loaded rows when a later page fails and offers a retry when the first fails", async () => {
    api.fetchOpportunityHistory
      .mockResolvedValueOnce(page([episode("1720000002000000000")], "cursor-1"))
      .mockRejectedValueOnce(new Error("Request failed (503)"))
      .mockRejectedValueOnce(new Error("Request failed (500)"))
      .mockResolvedValueOnce(page([episode("1720000004000000000")], null));

    render(<History />);
    await screen.findByText("1 shown, more available");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Request failed (503)");
    expect(rowCount()).toBe(1);
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(await screen.findByText("Could not load opportunity history")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("1 shown")).toBeInTheDocument();
  });

  it("rejects unusable date input", () => {
    expect(localInputToNs("not a date")).toBeNull();
    expect(localInputToNs("1960-01-01T00:00")).toBeNull();
  });
});
