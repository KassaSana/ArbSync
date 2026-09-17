# Changelog

## Unreleased

### Correctness

- Accept crash-recovered episodes with `close_reason="orphaned"` on the API and
  dashboard. They were still open when the previous process died, so their
  lifetime stays unknown and they must not appear as currently open.
- Price each executable route by spending the quote notional on the buy venue and
  selling exactly the acquired base quantity, rather than independently spending the
  same quote amount on both legs.
- Broadcast book ineligibility before later snapshots and preserve snapshot ordering,
  so clients cannot retain or revive a quote the backend has rejected.
- Reconcile minute rollups from canonical episode rows after updates, and prevent an
  older statistics response from replacing a newer dashboard request.

### Added

- `arbsync capture` records real three-venue traffic (WebSocket texts plus REST
  snapshot payloads) to gzipped JSONL through a bounded non-blocking writer, and
  `arbsync replay` replays it through the production adapters, books, and detector
  with no network access. Replaying a capture twice yields identical transitions
  and detector outputs; `replay --serve` drives the dashboard from a capture.
  A 150-second three-venue sample is committed under
  `server/tests/fixtures/captured/`. Capture files use plain JSONL or gzip;
  zstd stays a future option.
- Store one opportunity episode per route dislocation, including peak and close state,
  lifetime, and crash-recovered `orphaned` closure (SQLite schema version 3).
- Walk eligible L2 books for configured quote notionals, expose venue VWAP/fill-rate
  results and explicit insufficient depth, and price sells using matched base quantity.
- Persist exact-string schema-v4 pricing ledgers that separate top-of-book theoretical,
  gross depth-executable, fee impact, and net executable spread. The dashboard opportunity
  feed shows peak theoretical values, the first notional's stored net spread, and lifetime.

### Upgrade notes

- Schema version 3 replaces legacy per-update opportunities with episodes and cannot fold
  old rows into episode lifetimes; startup drops that legacy history. Schema version 4
  migrates version 3 in place by adding stored pricing ledgers. Back up first if history
  matters.
- Every configured exchange now requires a `fees.<exchange>.taker_pct` entry. There is no
  implicit zero-fee default.

## 0.1.0 — 2026-09-17

First public alpha. Detection-only: ArbSync observes public order books and reports
theoretical cross-exchange spreads; it does not trade.

### Added

- Public Gemini, Coinbase, and Binance.US L2 ingestion, exchange-owned recovery,
  canonical eligibility checks, and theoretical matching-market detection.
- React monitoring dashboard, REST/WebSocket APIs, readiness and Prometheus metrics.
- Bounded SQLite opportunity persistence, approximate minute statistics, and the
  explicit `arbsync-prune` maintenance command.
- Installed `arbsync` command with explicit configuration selection and safe example
  generation outside a repository checkout.
- Synthetic replay, connected-dashboard profiling, and live-soak observation tools.
- `tools/fee_survival.py` charges stored opportunities an assumed taker fee on both
  legs and counts survivors once per distinct resting-quote pair. Applied to the
  four-hour soak, no opportunity survives retail fees; the analysis is recorded in
  [validation status](docs/VALIDATION.md).
- Contribution, security, license, dependency-audit, and maintenance documentation.

### Correctness and reliability

- Preserve USD and USDT as different markets. Opportunities require matching base
  and quote assets; profit aggregates are grouped by quote currency. The shipped
  configuration subscribes to Binance.US's USD markets, and the adapter normalizes
  `…USD`, `…USDC`, and `…BUSD` symbols to hyphenated canonical pairs, so all three
  venues compare the same nine USD markets; USDT symbols still form their own market.
- Exclude disconnected, stale, incomplete, crossed, and discontinuous books from
  detection and dashboard spreads. Invalid snapshots require adapter resynchronization.
- Confirm repeated reconciliation mismatches before recovery to reduce false reconnects
  caused by non-atomic REST and stream observations.
- Fail closed after persistence-worker failure and keep shutdown bounded. Report
  unexpected WebSocket sender errors and retain bounded per-client delivery.
- Validate runtime settings and incoming dashboard payloads; recover pair rosters
  during cold start and reconnects.
- Shut down in dependency order: adapters and the reconciler are awaited before the
  broadcaster closes, so their final disconnected statuses reach connected clients,
  and a closed broadcaster no longer respawns its flush loop.
- Decode exchange price levels through one shared boundary so the decimal-string
  guarantee is a single function rather than a convention at twelve call sites.

### Verification

- A four-hour uninterrupted live soak against all 27 configured books completed on
  2026-09-16: no restart, zero sequence gaps across 1.57 million events, RSS +7.4 MiB
  start to end, and full recovery from a seven-minute host network outage. The reviewed
  report and its two follow-up tickets are in [validation status](docs/VALIDATION.md).
- Backend coverage is gated at 85% over the whole package (94% measured); dashboard
  coverage is gated at 75% over every source file (83% measured). Both run in CI on
  Windows and Ubuntu.
- The CI attribution check survives force-pushed branches, so rebased dependency
  updates are judged on their content.

### Upgrade and deployment notes

- **Legacy history:** startup clears opportunity history from the old schema that
  mislabeled Binance.US USDT opportunities as USD. Back up an older database before
  upgrading; discarded rows cannot be treated as valid USD accounting history.
- Canonical prices, sizes, spreads, and profits remain decimal strings. Derived
  statistics use approximate SQLite `REAL` values.
- The unauthenticated uptime-reset route has been removed. Local binding defaults
  to loopback; public deployments require the documented proxy and origin controls.
- Retention is opt-in through explicit pruning; upgrading does not start automatic
  history deletion on a schedule.
- The Python distribution is named `arbsync`, matching the command; the import package
  is `arb`. Built artifacts are `arbsync-0.1.0-py3-none-any.whl` and `arbsync-0.1.0.tar.gz`.
- The dashboard toolchain now requires Node.js 22 or newer (Vite 8, jsdom 30); the
  dashboard package declares this in its `engines` field. The backend requires Python
  3.11 or newer as before, and its suite has been run on 3.11 through 3.14.

### Known limitations

- Opportunities exclude fees, slippage, latency, inventory, and execution risk.
  ArbSync does not place trades or convert between quote currencies.
- Long-duration evidence is a single four-hour run on one workstation. It establishes
  recovery behavior and the absence of short-horizon leaks; it does not rule out slower
  growth or daily-cycle effects, and it exercised only the observer's lightweight
  WebSocket consumer rather than real browser clients.
- The Binance.US adapter resynchronizes its whole stream when one pair's drift is
  confirmed, so all its books leave detection for the recovery interval (13.9 s observed).
- See [validation status](docs/VALIDATION.md) for exact evidence and unresolved gaps.

See [the release process](docs/RELEASING.md) for versioning and publication checks.
