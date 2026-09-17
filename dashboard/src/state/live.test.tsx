import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveProvider, useLive } from "./live";

/** Captures every socket the provider opens so tests can drive them. */
class MockSocket {
  static instances: MockSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    MockSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.onclose?.();
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  emitRaw(data: string): void {
    this.onmessage?.({ data });
  }
}

function book(exchange: string, price: string, sequence: number) {
  return {
    exchange,
    pair: "BTC-USD",
    best_bid_price: price,
    best_bid_size: "1",
    best_ask_price: String(Number(price) + 1),
    best_ask_size: "1",
    sequence,
    timestamp_ns: "1000000",
  };
}

function episode(startNs: string, closed = false) {
  return {
    start_ns: startNs,
    end_ns: closed ? String(BigInt(startNs) + 2_000_000_000n) : null,
    duration_ns: closed ? "2000000000" : null,
    pair: "BTC-USD",
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
    close_spread_pct: closed ? "0" : null,
    close_reason: closed ? "spread_closed" : null,
  };
}

function status(exchange: string, eligible: boolean) {
  return {
    exchange,
    pair: "BTC-USD",
    initialized: true,
    continuous: true,
    connected: eligible,
    age_ms: 10,
    max_age_ms: 60_000,
    eligible,
    reason: eligible ? null : "disconnected",
  };
}

/** Renders the books the provider currently holds, newest state each render. */
function Probe() {
  const live = useLive();
  return (
    <div>
      <span data-testid="books">
        {Object.keys(live.books).sort().join(",") || "(none)"}
      </span>
      <span data-testid="statuses">
        {Object.keys(live.bookStatuses).sort().join(",") || "(none)"}
      </span>
      <span data-testid="connection">{live.status}</span>
      <span data-testid="episodes">
        {live.opportunities.state === "ready"
          ? live.opportunities.data
              .map((o) => `${o.start_ns}:${o.close_reason ?? "open"}`)
              .join(",") || "(empty)"
          : live.opportunities.state}
      </span>
      <span data-testid="invalid-frames">{live.invalidFrameCount}</span>
      <span data-testid="pairs">
        {live.pairs.state === "ready"
          ? live.pairs.data.map((p) => `${p.exchange}:${p.pair}`).join(",") || "(empty)"
          : live.pairs.state}
      </span>
      <button type="button" onClick={live.refreshPairs}>
        refresh pairs
      </button>
    </div>
  );
}

function pairsRequestCount(): number {
  const calls = (globalThis.fetch as unknown as { mock: { calls: [string][] } }).mock.calls;
  return calls.filter(([url]) => url.includes("/api/pairs")).length;
}

beforeEach(() => {
  MockSocket.instances = [];
  vi.stubGlobal("WebSocket", MockSocket);
  // The provider also loads pairs, statuses, opportunities, stats and adapters
  // over REST on mount. None of those matter here; keep them empty and quiet.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => ({
      ok: true,
      json: async () => (input.includes("/api/stats") ? {} : []),
      text: async () => "",
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function socketCount(count: number): Promise<MockSocket> {
  // Reconnect backoff is ~1s with jitter, so allow more than waitFor's default.
  await waitFor(() => expect(MockSocket.instances).toHaveLength(count), { timeout: 4_000 });
  return MockSocket.instances[count - 1];
}

/** Exact match: `toHaveTextContent` is a substring test and would pass on stale state. */
async function expectBooks(keys: string): Promise<void> {
  await waitFor(() => expect(screen.getByTestId("books").textContent).toBe(keys));
}

describe("live state", () => {
  it("replaces held books when a state snapshot omits one", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const socket = await socketCount(1);
    socket.onopen?.();

    socket.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: {
        books: [book("gemini", "100", 1), book("coinbase", "101", 1)],
        statuses: [status("gemini", true), status("coinbase", true)],
      },
    });
    await expectBooks("coinbase:BTC-USD,gemini:BTC-USD");

    // A later snapshot without coinbase is the server saying coinbase is no
    // longer eligible. Merging would keep its last quote on screen forever.
    socket.emit({
      type: "state_snapshot",
      stream_sequence: 2,
      payload: {
        books: [book("gemini", "102", 2)],
        statuses: [status("gemini", true), status("coinbase", false)],
      },
    });
    await expectBooks("gemini:BTC-USD");
    // Statuses are a complete account of tracked books, so coinbase stays
    // listed - as ineligible.
    expect(screen.getByTestId("statuses").textContent).toBe("coinbase:BTC-USD,gemini:BTC-USD");
  });

  it("replaces an open episode with its close instead of adding a row", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const socket = await socketCount(1);
    socket.onopen?.();
    socket.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: { books: [], statuses: [] },
    });

    socket.emit({ type: "opportunity", stream_sequence: 2, payload: episode("1000") });
    socket.emit({ type: "opportunity", stream_sequence: 3, payload: episode("2000") });
    await waitFor(() =>
      expect(screen.getByTestId("episodes").textContent).toBe("2000:open,1000:open"),
    );

    // The close carries the same identity, so it takes the open row's place;
    // the feed stays newest-first by start time, not by arrival.
    socket.emit({ type: "opportunity", stream_sequence: 4, payload: episode("1000", true) });
    await waitFor(() =>
      expect(screen.getByTestId("episodes").textContent).toBe("2000:open,1000:spread_closed"),
    );
  });

  it("still merges ordinary incremental updates", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const socket = await socketCount(1);
    socket.onopen?.();

    socket.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: { books: [book("gemini", "100", 1)], statuses: [status("gemini", true)] },
    });
    await expectBooks("gemini:BTC-USD");

    socket.emit({ type: "top_of_book", stream_sequence: 2, payload: book("coinbase", "101", 1) });
    await expectBooks("coinbase:BTC-USD,gemini:BTC-USD");
  });

  it("quarantines malformed frames without advancing the stream", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const socket = await socketCount(1);
    socket.onopen?.();
    socket.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: { books: [book("gemini", "100", 1)], statuses: [status("gemini", true)] },
    });
    await expectBooks("gemini:BTC-USD");

    socket.emitRaw("{");
    socket.emit({ type: "top_of_book", stream_sequence: 2 });
    socket.emit({
      type: "top_of_book",
      stream_sequence: 3,
      payload: { ...book("coinbase", "101", 1), best_bid_price: "NaN" },
    });
    socket.emit({
      type: "top_of_book",
      stream_sequence: 4,
      payload: { ...book("coinbase", "101", 1), timestamp_ns: "1.5" },
    });
    socket.emit({ type: "mystery", stream_sequence: 5, payload: {} });

    await waitFor(() => expect(screen.getByTestId("invalid-frames")).toHaveTextContent("5"));
    expect(screen.getByTestId("books").textContent).toBe("gemini:BTC-USD");

    // Invalid sequence values were quarantined, so the next valid sequence is
    // still 2 and must be accepted.
    socket.emit({ type: "top_of_book", stream_sequence: 2, payload: book("coinbase", "101", 1) });
    await expectBooks("coinbase:BTC-USD,gemini:BTC-USD");
  });

  it("drops a held quote as soon as a status reports the book ineligible", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const socket = await socketCount(1);
    socket.onopen?.();

    socket.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: {
        books: [book("gemini", "100", 1), book("coinbase", "101", 1)],
        statuses: [status("gemini", true), status("coinbase", true)],
      },
    });
    await expectBooks("coinbase:BTC-USD,gemini:BTC-USD");

    // A disconnect arrives as an incremental status, not a snapshot.
    socket.emit({
      type: "book_status",
      stream_sequence: 2,
      payload: status("coinbase", false),
    });
    await expectBooks("gemini:BTC-USD");

    // Recovery re-populates it: the server only sends a quote once eligible.
    socket.emit({ type: "top_of_book", stream_sequence: 3, payload: book("coinbase", "102", 2) });
    socket.emit({ type: "book_status", stream_sequence: 4, payload: status("coinbase", true) });
    await expectBooks("coinbase:BTC-USD,gemini:BTC-USD");
  });

  it("re-requests the pair roster on every connection", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const first = await socketCount(1);
    await waitFor(() => expect(pairsRequestCount()).toBe(1));

    // A dashboard opened before the backend was answering gets another chance
    // as soon as the socket comes up, and again after any reconnect.
    first.onopen?.();
    await waitFor(() => expect(pairsRequestCount()).toBe(2));

    first.close();
    const second = await socketCount(2);
    second.onopen?.();
    await waitFor(() => expect(pairsRequestCount()).toBe(3));
  });

  it("recovers an empty pair roster when asked to retry", async () => {
    let pairs: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => ({
        ok: true,
        json: async () => {
          if (input.includes("/api/pairs")) {
            return pairs;
          }
          return input.includes("/api/stats") ? {} : [];
        },
        text: async () => "",
      })),
    );

    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("pairs")).toHaveTextContent("(empty)"));

    // The roster is configuration, so a later request can succeed where the
    // first returned nothing. Retrying must actually re-read it.
    pairs = [{ exchange: "gemini", pair: "BTC-USD" }];
    screen.getByRole("button", { name: "refresh pairs" }).click();

    await waitFor(() =>
      expect(screen.getByTestId("pairs")).toHaveTextContent("gemini:BTC-USD"),
    );
  });

  it("rebuilds state from the snapshot delivered after a reconnect", async () => {
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
    );
    const first = await socketCount(1);
    first.onopen?.();
    first.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: {
        books: [book("gemini", "100", 1), book("binance", "103", 1)],
        statuses: [status("gemini", true), status("binance", true)],
      },
    });
    await expectBooks("binance:BTC-USD,gemini:BTC-USD");

    first.close();
    await waitFor(() => expect(screen.getByTestId("connection")).toHaveTextContent("reconnecting"));

    // The new connection restarts stream sequences at 1. That must not be
    // mistaken for a replay of the previous connection and dropped.
    const second = await socketCount(2);
    second.onopen?.();
    second.emit({
      type: "state_snapshot",
      stream_sequence: 1,
      payload: { books: [book("gemini", "105", 9)], statuses: [status("gemini", true)] },
    });

    // binance is gone because the reconnect snapshot did not report it.
    await expectBooks("gemini:BTC-USD");
  });
});
