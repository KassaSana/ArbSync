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
  fetchAdapterStatus,
  fetchBookStatus,
  fetchPairs,
  fetchRecentOpportunities,
  fetchStats,
  Opportunity,
  PairRecord,
  Stats,
  TopOfBook,
} from "../api/client";
import { ConnectionStatus, useWebSocket } from "../hooks/useWebSocket";
import { Async, failed, loading, ready } from "../lib/async";

type LivePayload =
  | { type: "top_of_book"; payload: TopOfBook }
  | { type: "opportunity"; payload: Opportunity }
  | { type: "book_status"; payload: BookStatus }
  | {
      type: "state_snapshot";
      payload: { books: TopOfBook[]; statuses: BookStatus[] };
    };

type LiveEnvelope = LivePayload & { stream_sequence: number };

/**
 * A canonical book status plus the moment we received it.
 *
 * `age_ms` is the backend's monotonic receipt age at the instant it was sent,
 * which is the only freshness clock the server trusts. It is a fixed reading,
 * so displaying it alone would freeze; adding the time elapsed here keeps a
 * canonical baseline while still advancing.
 */
export type TrackedBookStatus = BookStatus & { receivedAtMs: number };

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
  books: Record<string, TopOfBook>;
  bookStatuses: Record<string, TrackedBookStatus>;
  opportunities: Async<Opportunity[]>;
  stats: Async<Stats>;
  pairs: Async<PairRecord[]>;
  adapters: Async<AdapterStatus[]>;
  refreshStats: () => void;
  refreshOpportunities: () => void;
  refreshAdapters: () => void;
};

const LiveContext = createContext<LiveValue | null>(null);

const ADAPTER_POLL_MS = 5_000;
const STATS_POLL_MS = 30_000;
const MAX_FEED_ROWS = 50;

function bookKey(entry: { exchange: string; pair: string }): string {
  return `${entry.exchange}:${entry.pair}`;
}

function opportunityKey(opportunity: Opportunity): string {
  return [
    opportunity.timestamp_ns,
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
      const leftTimestamp = BigInt(left.timestamp_ns);
      const rightTimestamp = BigInt(right.timestamp_ns);
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
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
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

  useEffect(() => {
    fetchPairs()
      .then((data) => setPairs(ready(data)))
      .catch((error: unknown) => setPairs(failed(error)));

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

    const adapterTimer = window.setInterval(refreshAdapters, ADAPTER_POLL_MS);
    const statsTimer = window.setInterval(refreshStats, STATS_POLL_MS);
    return () => {
      window.clearInterval(adapterTimer);
      window.clearInterval(statsTimer);
    };
  }, [refreshAdapters, refreshOpportunities, refreshStats]);

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
    (event: MessageEvent<string>, connectionId: number) => {
      let message: LiveEnvelope;
      try {
        message = JSON.parse(event.data) as LiveEnvelope;
      } catch {
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

  // A fresh connection means we may have missed writes while we were away.
  useEffect(() => {
    if (websocket.status === "connected") {
      refreshOpportunities();
      refreshStats();
    }
  }, [websocket.connectionId, websocket.status, refreshOpportunities, refreshStats]);

  const value = useMemo<LiveValue>(
    () => ({
      status: websocket.status,
      feedLive: websocket.status === "connected",
      nowMs,
      lastTickAgeMs: lastTickAt === null ? null : Math.max(0, nowMs - lastTickAt),
      books,
      bookStatuses,
      opportunities,
      stats,
      pairs,
      adapters,
      refreshStats,
      refreshOpportunities,
      refreshAdapters,
    }),
    [
      websocket.status,
      nowMs,
      lastTickAt,
      books,
      bookStatuses,
      opportunities,
      stats,
      pairs,
      adapters,
      refreshStats,
      refreshOpportunities,
      refreshAdapters,
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
