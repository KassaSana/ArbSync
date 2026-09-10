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
    </div>
  );
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
