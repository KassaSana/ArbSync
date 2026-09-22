import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  fetchOpportunityHistory,
  fetchPairs,
  opportunityExportUrl,
} from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("REST payload validation", () => {
  it("reports malformed JSON as a typed endpoint failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError("bad JSON");
        },
      })),
    );

    await expect(fetchPairs()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      path: "/api/pairs",
      message: "Invalid JSON response from /api/pairs",
    });
  });

  it("reports structurally invalid JSON as a typed endpoint failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => [{ exchange: "gemini" }] })),
    );

    const failure = await fetchPairs().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({
      status: 200,
      path: "/api/pairs",
      message: "Invalid response payload from /api/pairs",
    });
  });
});

describe("opportunity history requests", () => {
  it("sends only set filters, the limit, and the cursor", async () => {
    const fetchMock = vi.fn<(url: string) => Promise<unknown>>(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ items: [], next_cursor: null, order: "start_ns_desc_id_desc" }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    await fetchOpportunityHistory(
      { pair: "BTC-USD", buy_exchange: "", state: "closed", from_ns: "1720000000000000000" },
      "abc_-",
      25,
    );

    const url = new URL(fetchMock.mock.calls[0][0], "http://localhost");
    expect(url.pathname).toBe("/api/opportunities");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      pair: "BTC-USD",
      state: "closed",
      from_ns: "1720000000000000000",
      limit: "25",
      cursor: "abc_-",
    });
  });

  it("surfaces a rejected cursor as an API error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 400,
        text: async () => '{"detail":"cursor was issued for different filters"}',
      })),
    );

    await expect(fetchOpportunityHistory({}, "stale", 100)).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
    });
  });

  it("builds an export URL with the filters and a row cap", () => {
    const url = new URL(
      opportunityExportUrl({ sell_exchange: "gemini", close_reason: "orphaned" }, 500),
      "http://localhost",
    );
    expect(url.pathname).toBe("/api/opportunities/export");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      sell_exchange: "gemini",
      close_reason: "orphaned",
      max_rows: "500",
    });
  });
});
