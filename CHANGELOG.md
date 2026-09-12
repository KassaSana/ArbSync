# Changelog

Changes intended for the first public alpha release are listed below. No release
date or tag is claimed until the release checklist has been completed.

## Unreleased

### Added

- Public Gemini, Coinbase, and Binance.US L2 ingestion, exchange-owned recovery,
  canonical eligibility checks, and theoretical matching-market detection.
- React monitoring dashboard, REST/WebSocket APIs, readiness and Prometheus metrics.
- Bounded SQLite opportunity persistence, approximate minute statistics, and the
  explicit `arbsync-prune` maintenance command.
- Installed `arbsync` command with explicit configuration selection and safe example
  generation outside a repository checkout.
- Synthetic replay, connected-dashboard profiling, and live-soak observation tools.
- Contribution, security, license, dependency-audit, and maintenance documentation.

### Correctness and reliability

- Preserve USD and USDT as different markets. Opportunities require matching base
  and quote assets; profit aggregates are grouped by quote currency.
- Exclude disconnected, stale, incomplete, crossed, and discontinuous books from
  detection and dashboard spreads. Invalid snapshots require adapter resynchronization.
- Confirm repeated reconciliation mismatches before recovery to reduce false reconnects
  caused by non-atomic REST and stream observations.
- Fail closed after persistence-worker failure and keep shutdown bounded. Report
  unexpected WebSocket sender errors and retain bounded per-client delivery.
- Validate runtime settings and incoming dashboard payloads; recover pair rosters
  during cold start and reconnects.

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

### Known limitations

- Opportunities exclude fees, slippage, latency, inventory, and execution risk.
  ArbSync does not place trades or convert between quote currencies.
- The required uninterrupted 24-hour live soak and reviewed report remain outstanding.
  Short smoke tests and modeled load profiles do not establish day-long reliability.
- See [validation status](docs/VALIDATION.md) for exact evidence and unresolved gaps.

See [the release process](docs/RELEASING.md) for versioning and publication checks.
