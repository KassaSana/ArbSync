import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, fetchPairs } from "./client";

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
