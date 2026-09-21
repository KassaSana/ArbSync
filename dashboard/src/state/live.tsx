import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AdapterStatus,
  BookStatus,
  DepthPricing,
  fetchAdapterStatus,
  fetchBookStatus,
  fetchDepthPricing,
  fetchPairs,
  fetchRecentOpportunities,
  fetchStats,
  Opportunity,
  PairRecord,
  Stats,
  TopOfBook,
} from "../api/client";
import { decodeLiveEnvelope, type LiveEnvelope } from "../api/schema";
import { ConnectionStatus, useWebSocket } from "../hooks/useWebSocket";
import { Async, failed, loading, ready } from "../lib/async";

/**
 * A canonical book status plus the moment we received it.
 *
 * `age_ms` is the backend's monotonic receipt age at the instant it was sent,
 * which is the only freshness clock the server trusts. It is a fixed reading,
 * so displaying it alone would freeze; adding the time elapsed here keeps a
 * canonical baseline while still advancing.
 */
export type TrackedBookStatus = BookStatus & { receivedAtMs: number };

export type TrackedDepthPricing = DepthPricing & {
  receivedAtMs: number;
  generation: number;
};

function track(statuses: Iterable<BookStatus>, receivedAtMs: number): TrackedBookStatus[] {
  return [...statuses].map((status) => ({ ...status, receivedAtMs }));
}

/** Age to display for one contributing book: canonical reading plus local elapsed. */
export function contributingAgeMs(status: TrackedBookStatus, nowMs: number): number {
  return (status.age_ms ?? 0) + Math.max(0, nowMs - status.receivedAtMs);
}

type LiveValue = {
  status: ConnectionStatus;
  feedLive: boolean;
  nowMs: number;
  lastTickAgeMs: number | null;
  invalidFrameCount: number;
  books: Record<string, TopOfBook>;
  bookStatuses: Record<string, TrackedBookStatus>;
  opportunities: Async<Opportunity[]>;
  stats: Async<Stats>;
  pairs: Async<PairRecord[]>;
  adapters: Async<AdapterStatus[]>;
  depthPricing: Async<TrackedDepthPricing>;
  refreshStats: () => void;
  refreshOpportunities: () => void;
  refreshAdapters: () => void;
  refreshPairs: () => void;
  refreshDepthPricing: () => void;
};

const LiveContext = createContext<LiveValue | null>(null);

const ADAPTER_POLL_MS = 5_000;
const STATS_POLL_MS = 30_000;
export const DEPTH_PRICING_POLL_MS = 5_000;
const MAX_FEED_ROWS = 50;

function bookKey(entry: { exchange: string; pair: string }): string {
  return `${entry.exchange}:${entry.pair}`;
}

// An episode's close arrives as a second message with the same identity, so
// keying on it makes the close replace the open row instead of adding one.
function opportunityKey(opportunity: Opportunity): string {
  return [
    opportunity.start_ns,
    opportunity.pair,
    opportunity.buy_exchange,
    opportunity.sell_exchange,
  ].join(":");
}

function mergeOpportunities(current: Opportunity[], incoming: Opportunity[]): Opportunity[] {
  const unique = new Map<string, Opportunity>();
  for (const opportunity of [...current, ...incoming]) {
    unique.set(opportunityKey(opportunity), opportunity);
  }
  return [...unique.values()]
    .sort((left, right) => {
      const leftTimestamp = BigInt(left.start_ns);
      const rightTimestamp = BigInt(right.start_ns);
      if (leftTimestamp === rightTimestamp) {
        return 0;
      }
      return leftTimestamp > rightTimestamp ? -1 : 1;
    })
    .slice(0, MAX_FEED_ROWS);
}

/**
 * Owns the live socket for the whole app. It used to live inside the Dashboard
 * page, which meant switching to Statistics tore the socket down and threw away
 * every book we had accumulated.
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const [books, setBooks] = useState<Record<string, TopOfBook>>({});
  const [bookStatuses, setBookStatuses] = useState<Record<string, TrackedBookStatus>>({});
  const [opportunities, setOpportunities] = useState<Async<Opportunity[]>>(loading);
  const [stats, setStats] = useState<Async<Stats>>(loading);
  const [pairs, setPairs] = useState<Async<PairRecord[]>>(loading);
  const [adapters, setAdapters] = useState<Async<AdapterStatus[]>>(loading);
  const [depthPricing, setDepthPricing] = useState<Async<TrackedDepthPricing>>(loading);
  const depthPricingGeneration = useRef(0);
  const lastAppliedDepthPricingGeneration = useRef(0);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [invalidFrameCount, setInvalidFrameCount] = useState(0);
  const [nowMs, setNowMs] = useState(() => Date.now());

  // Ages are only meaningful if something re-renders to recompute them.
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  const refreshStats = useCallback(() => {
    fetchStats()
      .then((data) => setStats(ready(data)))
      .catch((error: unknown) => setStats(failed(error)));
  }, []);

  const refreshOpportunities = useCallback(() => {
    fetchRecentOpportunities()
      .then((fetched) =>
        setOpportunities((current) =>
          ready(mergeOpportunities(current.state === "ready" ? current.data : [], fetched)),
        ),
      )
      .catch((error: unknown) => setOpportunities(failed(error)));
  }, []);

  const refreshAdapters = useCallback(() => {
    fetchAdapterStatus()
      .then((data) => setAdapters(ready(data)))
      .catch((error: unknown) => setAdapters(failed(error)));
  }, []);

  const refreshPairs = useCallback(() => {
    fetchPairs()
      .then((data) => setPairs(ready(data)))
      .catch((error: unknown) => setPairs(failed(error)));
  }, []);

  const refreshDepthPricing = useCallback(() => {
    const generation = ++depthPricingGeneration.current;
    fetchDepthPricing()
      .then((data) => {
        if (generation <= lastAppliedDepthPricingGeneration.current) {
          return;
        }
        lastAppliedDepthPricingGeneration.current = generation;
        setDepthPricing(ready({ ...data, receivedAtMs: Date.now(), generation }));
      })
      .catch((error: unknown) => {
        if (generation <= lastAppliedDepthPricingGeneration.current) {
          return;
        }
        lastAppliedDepthPricingGeneration.current = generation;
        setDepthPricing(failed(error));
      });
  }, []);

  useEffect(() => {
    refreshPairs();

    // One fallback read so the table is populated even if the socket never opens.
    fetchBookStatus()
      .then((statuses) =>
        setBookStatuses(
          Object.fromEntries(track(statuses, Date.now()).map((s) => [bookKey(s), s])),
        ),
      )
      .catch(() => undefined);

    refreshStats();
    refreshOpportunities();
    refreshAdapters();
    refreshDepthPricing();

    const adapterTimer = window.setInterval(refreshAdapters, ADAPTER_POLL_MS);
    const statsTimer = window.setInterval(refreshStats, STATS_POLL_MS);
    const depthPricingTimer = window.setInterval(refreshDepthPricing, DEPTH_PRICING_POLL_MS);
    return () => {
      window.clearInterval(adapterTimer);
      window.clearInterval(statsTimer);
      window.clearInterval(depthPricingTimer);
    };
  }, [
    refreshAdapters,
    refreshDepthPricing,
    refreshOpportunities,
    refreshPairs,
    refreshStats,
  ]);

  // Socket frames are coalesced into one state commit per animation frame.
  // At 27 subscriptions a setState per tick is the fastest way to fail INP.
  const pending = useRef({
    books: new Map<string, TopOfBook>(),
    statuses: new Map<string, BookStatus>(),
    opportunities: [] as Opportunity[],
    tickAt: 0,
    // Books to drop because a status in this batch declared them ineligible.
    evicted: new Set<string>(),
    // Set by a state snapshot, which is a complete account of book state and so
    // replaces what we hold instead of being merged into it.
    authoritative: false,
  });
  const frame = useRef<number | null>(null);

  const flush = useCallback(() => {
    frame.current = null;
    const batch = pending.current;
    pending.current = {
      books: new Map(),
      statuses: new Map(),
      opportunities: [],
      tickAt: 0,
      evicted: new Set(),
      authoritative: false,
    };

    const receivedAtMs = Date.now();
    const stamped = Object.fromEntries(
      track(batch.statuses.values(), receivedAtMs).map((s) => [bookKey(s), s]),
    );
    if (batch.authoritative) {
      // Replace, so a book the server no longer reports eligible disappears
      // rather than lingering at whatever quote it last had.
      setBooks(Object.fromEntries(batch.books));
      setBookStatuses(stamped);
    } else {
      if (batch.books.size > 0 || batch.evicted.size > 0) {
        setBooks((current) => {
          const next = { ...current, ...Object.fromEntries(batch.books) };
          for (const key of batch.evicted) {
            delete next[key];
          }
          return next;
        });
      }
      if (batch.statuses.size > 0) {
        setBookStatuses((current) => ({ ...current, ...stamped }));
      }
    }
    if (batch.opportunities.length > 0) {
      setOpportunities((current) =>
        ready(
          mergeOpportunities(
            current.state === "ready" ? current.data : [],
            batch.opportunities,
          ),
        ),
      );
    }
    if (batch.tickAt > 0) {
      setLastTickAt(batch.tickAt);
    }
  }, []);

  const schedule = useCallback(() => {
    if (frame.current === null) {
      frame.current = window.requestAnimationFrame(flush);
    }
  }, [flush]);

  const lastStream = useRef({ connectionId: 0, sequence: 0 });

  const handleMessage = useCallback(
    (event: MessageEvent<unknown>, connectionId: number) => {
      let message: LiveEnvelope;
      try {
        if (typeof event.data !== "string") {
          throw new TypeError("live frames are JSON text");
        }
        message = decodeLiveEnvelope(JSON.parse(event.data));
      } catch {
        setInvalidFrameCount((count) => count + 1);
        return;
      }

      if (lastStream.current.connectionId !== connectionId) {
        lastStream.current = { connectionId, sequence: 0 };
      }
      if (message.stream_sequence <= lastStream.current.sequence) {
        return;
      }
      lastStream.current.sequence = message.stream_sequence;
      pending.current.tickAt = Date.now();

      if (message.type === "state_snapshot") {
        // Anything queued earlier in this frame predates the snapshot, so the
        // snapshot supersedes it. Updates arriving after it still apply.
        pending.current.books.clear();
        pending.current.statuses.clear();
        pending.current.evicted.clear();
        pending.current.authoritative = true;
        for (const book of message.payload.books) {
          pending.current.books.set(bookKey(book), book);
        }
        for (const status of message.payload.statuses) {
          pending.current.statuses.set(bookKey(status), status);
        }
      } else if (message.type === "top_of_book") {
        const key = bookKey(message.payload);
        pending.current.books.set(key, message.payload);
        pending.current.evicted.delete(key);
      } else if (message.type === "book_status") {
        const key = bookKey(message.payload);
        pending.current.statuses.set(key, message.payload);
        if (message.payload.eligible) {
          pending.current.evicted.delete(key);
        } else {
          // Drop the quote outright rather than trusting every reader to check
          // eligibility first. A held quote for an ineligible book is exactly
          // what let a dropped venue keep pricing the spreads table.
          pending.current.books.delete(key);
          pending.current.evicted.add(key);
        }
      } else if (message.type === "opportunity") {
        pending.current.opportunities.push(message.payload);
      }

      schedule();
    },
    [schedule],
  );

  const websocket = useWebSocket(handleMessage);

  useEffect(
    () => () => {
      if (frame.current !== null) {
        window.cancelAnimationFrame(frame.current);
      }
    },
    [],
  );

  // A fresh connection means we may have missed writes while we were away. It
  // also means the backend is answering now, which may not have been true when
  // the pair roster was first requested.
  useEffect(() => {
    if (websocket.status === "connected") {
      refreshOpportunities();
      refreshStats();
      refreshPairs();
      refreshDepthPricing();
    }
  }, [
    websocket.connectionId,
    websocket.status,
    refreshDepthPricing,
    refreshOpportunities,
    refreshPairs,
    refreshStats,
  ]);

  const value = useMemo<LiveValue>(
    () => ({
      status: websocket.status,
      feedLive: websocket.status === "connected",
      nowMs,
      lastTickAgeMs: lastTickAt === null ? null : Math.max(0, nowMs - lastTickAt),
      invalidFrameCount,
      books,
      bookStatuses,
      opportunities,
      stats,
      pairs,
      adapters,
      depthPricing,
      refreshStats,
      refreshOpportunities,
      refreshAdapters,
      refreshPairs,
      refreshDepthPricing,
    }),
    [
      websocket.status,
      nowMs,
      lastTickAt,
      invalidFrameCount,
      books,
      bookStatuses,
      opportunities,
      stats,
      pairs,
      adapters,
      depthPricing,
      refreshStats,
      refreshOpportunities,
      refreshAdapters,
      refreshPairs,
      refreshDepthPricing,
    ],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveValue {
  const value = useContext(LiveContext);
  if (value === null) {
    throw new Error("useLive must be used inside LiveProvider");
  }
  return value;
}
