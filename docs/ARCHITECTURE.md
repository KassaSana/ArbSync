# ArbSync architecture

ArbSync is a detection-only market-data application. It consumes public level-2 order
books from Gemini, Coinbase, and Binance.US, maintains trusted in-memory books, detects
theoretical cross-exchange opportunities, stores those opportunities in SQLite, and
streams live state to a React dashboard. It does not authenticate with exchanges or
place trades.

![ArbSync market-data pipeline](architecture.svg)

This document explains how the application fits together and where important decisions
are made. See [`RESYNC.md`](RESYNC.md) for the authoritative recovery design and
[`VALIDATION.md`](VALIDATION.md) for the behavior that has been verified.

## Design principles

Four rules shape the implementation:

1. Exchange adapters own exchange-specific sequence validation and recovery.
2. `OrderBookManager` is the single authority on whether a book may contribute to
   detection, readiness, metrics, or dashboard calculations.
3. Persistence and client delivery are bounded consumers and must not block market-data
   ingestion.
4. Canonical market and opportunity values remain decimal-exact strings at storage and
   API boundaries. Derived dashboard statistics are approximate observability values.

## End-to-end data flow

For each exchange message, the application follows this path:

1. An exchange adapter receives a public WebSocket frame and records its local receipt
   time.
2. The adapter parses the exchange-specific payload, validates native sequence rules,
   and emits one or more normalized `MarketEvent` values.
3. `OrderBookManager` applies the event to the matching `(exchange, pair)` L2 book.
4. The manager evaluates the canonical book eligibility rules.
5. An accepted and eligible top-of-book update is coalesced for live dashboard delivery.
6. The detector reads all eligible books for the same normalized pair and compares
   ordered buy/sell venue combinations.
7. Each opportunity is offered to the bounded persistence queue and the bounded live
   client queues.
8. The SQLite worker writes accepted opportunities in batches, while the React dashboard
   incorporates live messages into its application-wide state.

The pipeline is event-driven: an accepted market event can update a book and trigger
detection immediately. SQLite is not consulted during detection, and order books are
never persisted there.

## Component ownership

| Component | Responsibility | Does not own |
| --- | --- | --- |
| Exchange adapters | Protocol parsing, native sequence validation, reconnects, and snapshot recovery | Cross-exchange eligibility or detection |
| `OrderBookManager` | In-memory L2 state, normalized continuity, freshness, and canonical eligibility | Exchange-native recovery |
| `ArbitrageDetector` | Pairwise spread, maximum-size, and theoretical-profit calculations | Book trust or trade execution |
| `SnapshotReconciler` | Confirmed live-versus-REST divergence and recovery coordination | Exchange-native recovery |
| `OpportunityStore` | Bounded queuing, batched SQLite writes, and statistics queries | Order-book storage |
| `LiveBroadcaster` | State envelopes, book-update coalescing, and bounded per-client delivery | Market-data ingestion or detection |
| FastAPI application | REST, WebSocket, health, readiness, and metrics interfaces | Exchange protocol semantics |
| React `LiveProvider` | Shared browser connection, frame batching, reconnect refreshes, and live UI state | Deciding whether a book is eligible |

The ownership boundaries are deliberate. In particular, code outside
`OrderBookManager` consumes its eligibility verdict instead of recreating a weaker
version of that decision.

## Adapter normalization and recovery

All adapters expose the same normalized event shape, but their synchronization rules are
different:

- **Gemini** treats the first differential-depth frame for a pair after connecting as a
  full stream snapshot, validates later exchange update ranges, and assigns a consecutive
  local sequence.
- **Coinbase** waits for a fresh Level 2 stream snapshot. It does not mix Advanced Trade
  WebSocket sequence numbers with REST Exchange sequence numbers.
- **Binance.US** buffers WebSocket depth updates while fetching a REST snapshot, discards
  updates already covered by the snapshot, aligns the first retained update, and then
  validates every later update range.

When an adapter detects a gap or cannot establish a trustworthy baseline, it requests a
reconnect. The shared adapter loop clears per-connection state, reconnects with bounded
exponential backoff and jitter, and notifies the book manager about connection changes.
The affected books remain ineligible while reconstruction is in progress.

Detailed recovery behavior and its tradeoffs are documented in
[`RESYNC.md`](RESYNC.md).

## Book lifecycle and eligibility

`OrderBookManager` keeps one in-memory `OrderBook` for every exchange and normalized
pair it has seen. A book normally moves through these states:

```text
missing or disconnected
        |
        v
waiting for a trusted snapshot
        |
        v
initialized + continuous + fresh + complete + uncrossed
        |
        v
eligible for detection and display
```

A book is eligible only when all of the following are true:

- its adapter is connected;
- a snapshot has initialized it;
- normalized event sequences remain continuous;
- its most recent receipt time is inside the configured age limit;
- both bid and ask sides have a top level; and
- the best bid is strictly below the best ask.

A disconnect or normalized sequence gap clears the affected book, so later deltas cannot
silently continue an untrusted chain. A crossed book produced by a delta is also cleared.
An incomplete snapshot or one whose best bid is greater than or equal to its best ask is
rejected and cleared as an invalid baseline. These chain-invalidating outcomes signal the
originating adapter to reconnect, and recovery requires a new adapter-established snapshot
boundary. An incomplete state produced by a contiguous delta remains ineligible but may be
repopulated by a later contiguous delta.

Freshness uses monotonic receipt time rather than an exchange timestamp. That avoids
clock-skew and wall-clock adjustment errors when deciding whether local data is too old.
The configured limit is `order_books.max_age_seconds` in [`config.toml`](../config.toml).

The same eligibility result feeds four consumers:

- the detector receives only eligible top-of-book values;
- `/readyz` reports the service ready only when expected books and adapters are healthy
  and supervised background tasks have not failed;
- Prometheus metrics expose book eligibility and age; and
- the live API sends `book_status` values that the dashboard uses both to decide whether a
  quote may contribute to displayed spreads and to age the books that do.

## Detection model

Detection is performed independently for each normalized `BASE-QUOTE` pair. USD and
USDT are distinct quote assets; ArbSync does not assume parity or convert between them.

For every ordered pair of eligible venues, the detector checks whether the selling
venue's best bid exceeds the buying venue's best ask. If it does, it calculates:

```text
spread % = (sell bid - buy ask) / buy ask * 100
maximum size = min(size available at buy ask, size available at sell bid)
theoretical profit = maximum size * (sell bid - buy ask)
```

An opportunity is emitted when the spread meets the configured threshold. Prices,
sizes, spreads, and theoretical profits use Python `Decimal`; API and WebSocket payloads
serialize them as decimal strings.

These calculations exclude fees, slippage, latency, inventory, partial fills, transfer
constraints, and execution risk. They are observations, not executable trade quotes.

## Confirmed reconciliation recovery

The reconciler checks one target at a time and spreads those checks across the configured
`reconciliation.cycle_seconds`, so the setting describes a nominal full pass rather than
the delay between individual books. Network time can make a pass slightly longer.

Each comparison reads the top ten live and REST levels. It treats ordered price divergence
above 0.5% or aggregate side-size divergence above 50% as a mismatch. The wider size
tolerance accounts for normal depth churn between non-atomic reads. A matching comparison
or fetch failure resets the consecutive-mismatch streak.

After `confirmation_count` consecutive mismatches for the same book, the reconciler clears
that canonical chain and broadcasts its ineligibility before asking the adapter to reconnect.
The adapter still owns connection reset, snapshot acquisition, and native sequence recovery.
A cooldown prevents another recovery storm for the same target and is also the deadline for
reporting a started recovery as unresolved. Started, completed, timed-out, confirmed-mismatch,
raw-mismatch, and reconciliation-failure events are logged and counted.

## Persistence and statistics

`OpportunityStore` separates ingestion from SQLite writes with a bounded `asyncio`
queue. Its worker drains that queue in configurable batches or after a configurable
flush interval. SQLite runs in write-ahead logging mode and stores:

- canonical opportunity rows, with price and amount values stored as exact text; and
- per-minute, per-pair rollups used by statistics endpoints.

The canonical opportunity table is the source of truth. Rollup spread and profit values
use SQLite `REAL`/binary64 and are intentionally approximate. A write batch updates its
canonical rows and rollups in one transaction, so the two representations cannot commit
different subsets of that batch.

If the persistence queue is full, ingestion does not wait for disk capacity. The new
opportunity is rejected from the queue and a reason-labelled drop metric is incremented.
An initialization or worker failure is terminal for that store: the exception and failure
phase are retained, all later rows are rejected immediately, and the accepted-but-unflushed
count remains available as a metric and shutdown log field. This preserves the market-data
path at the cost of an explicitly observable persistence gap.

## Live API and backpressure

FastAPI exposes recent opportunities and statistics, adapter and book status, health,
readiness, Prometheus metrics, and the `/ws/live` stream. The generated OpenAPI reference
is available at `/docs` while the backend is running.

Each WebSocket connection receives a `state_snapshot` before incremental messages. The
snapshot contains the canonical status of every tracked book and top-of-book values only
for books that are eligible at connection time. Later envelopes carry one of:

- `top_of_book`;
- `book_status`; or
- `opportunity`.

Every envelope has a monotonically increasing stream sequence. Book and status traffic
is level-triggered: updates are coalesced by message type, exchange, and pair, and
unchanged display state is suppressed. Opportunity messages are delivered immediately.

Each client has its own bounded outgoing queue and sender task. If a client cannot keep
up, it is removed and its socket is closed with a retry-later status. A slow browser can
therefore lose its connection, but it cannot stall market-data processing or other
clients.

## Dashboard state

The React `LiveProvider` owns one WebSocket for the whole application so navigation does
not discard live state. On startup it also requests tracked pairs, current book status,
recent opportunities, summary statistics, and adapter status through REST. The tracked
pair roster is requested again whenever a socket connects, so a dashboard opened before
the backend was answering recovers without a reload.

Incoming WebSocket frames are collected and committed to React state once per animation
frame. This limits rendering pressure during market bursts. The browser rejects replayed
or out-of-order stream envelopes within a connection and refreshes persisted
opportunities and statistics after reconnecting.

The dashboard holds only quotes it is allowed to display. A `state_snapshot` replaces the
books and statuses it holds rather than merging into them, and a `book_status` reporting a
book ineligible discards that book's quote immediately. Displayed book age comes from the
canonical `age_ms` plus locally elapsed time, not from the exchange timestamp on a quote,
so the dashboard and the backend judge freshness by the same clock. When the browser
socket itself is interrupted, the dashboard labels the remaining values as last-known
state while reconnecting.

## Observability and failure behavior

The system makes degraded state visible instead of treating it as valid market data:

| Condition | Behavior |
| --- | --- |
| Adapter disconnect | Its books are cleared and immediately marked ineligible |
| Native or normalized sequence gap | The chain is invalidated; later deltas are rejected until a new snapshot arrives |
| Incomplete or crossed snapshot | The baseline is rejected and adapter-owned reconnection is requested |
| Old, incomplete, or crossed book | The book is excluded from detection, readiness, metrics eligibility, and spread calculations |
| Transient REST reconciliation mismatch | The mismatch is counted but the book remains eligible while confirmation is pending |
| Confirmed REST reconciliation mismatch | The affected book is cleared before adapter-owned reconnection; cooldown suppresses recovery storms |
| Full persistence queue | The row is dropped and counted without blocking ingestion |
| Persistence initialization or worker failure | The store enters a terminal failed state, rejects and counts later rows by reason, and reports unflushed work |
| Full client queue | The slow WebSocket client is disconnected and counted |
| Supervised background task exits | The failure is recorded, logged, counted, and exposed through readiness |
| Browser socket disconnects | Last-known values are visibly marked stale while reconnecting |

`/healthz` answers whether the HTTP process is alive. `/readyz` is the stronger operational
signal: it checks adapter connections, canonical status for every expected book, and
supervised background-task failures.

## Runtime composition and shutdown

`arb.main` loads configuration and constructs the adapters, book manager, detector,
opportunity store, broadcaster, reconciler, FastAPI application, and background-task
supervisor. Adapter consumers, persistence, reconciliation, and the HTTP server share one
asyncio event loop.

During shutdown, adapter and reconciliation tasks are cancelled, pending coalesced live
state is flushed, and a healthy persistence store drains accepted work before the process
exits. If the worker fails while shutdown is waiting to enqueue its sentinel, a failure
event releases that wait immediately and the unflushed count is logged.

## Related documentation

- [`../README.md`](../README.md) — installation, configuration, interfaces, and project scope
- [`RESYNC.md`](RESYNC.md) — exchange-specific recovery decision
- [`VALIDATION.md`](VALIDATION.md) — current verification evidence and remaining gaps
- [`BENCHMARKS.md`](BENCHMARKS.md) — benchmark, replay, profiling, and soak methodology
- [`DEPENDENCY_LICENSES.md`](DEPENDENCY_LICENSES.md) — dependency license audit
- [`../todolist.md`](../todolist.md) — prioritized correctness and release-readiness work
