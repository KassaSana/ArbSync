# Validation status

This page records what has been verified and what evidence is still missing. It is
not an implementation checklist; completed design decisions belong in the code,
tests, and focused documents such as [`RESYNC.md`](RESYNC.md).

## Automated verification

Current baseline: 176 backend tests passing as of 2026-09-09.

The suite covers:

- snapshot and delta application
- duplicate, overlapping, out-of-order, and missing exchange updates
- exchange-specific snapshot recovery
- disconnect invalidation and cold-start delta rejection
- stale, incomplete, and crossed-book exclusion
- detection using only eligible venues
- synthetic fixture replay for Gemini, Coinbase, and Binance.US
- bounded persistence and WebSocket queues
- coalesced dashboard delivery, immediate invalidation ordering, and suppression
  of unchanged book updates, including a sequence-gap storm
- cached top-of-book invalidation across size changes, level deletion, sequence
  gaps, snapshot recovery, and disconnect
- per-minute statistics rollup: backfill of pre-rollup history, consistency
  across separate write batches, exactness for a window starting mid-minute,
  and sub-minute buckets bypassing the rollup
- WebSocket reconnection and state restoration
- REST, readiness, metrics, persistence, and statistics behavior

CI also runs strict mypy, Ruff, frontend type checking, ESLint, the production
dashboard build, and coverage checks for the order-book and detector modules.

## Synthetic performance

The committed detector and ingest-to-detection measurements are documented in
[`BENCHMARKS.md`](BENCHMARKS.md), with raw values in
[`../artifacts/benchmarks/results.json`](../artifacts/benchmarks/results.json).

These measurements are local and synthetic. They do not include internet latency or
prove sustained behavior against live exchanges.

## Connected-dashboard performance

A burst investigation dated 2026-09-08 measured the production ingestion path with
a real headless browser running the built dashboard, at 110, 1,100 and 5,500
events/s. Method, full results and limits are in
[`../artifacts/benchmarks/performance/README.md`](../artifacts/benchmarks/performance/README.md).

This closed a gap the earlier synthetic figures concealed. Detector-only timing
had not shown that, under bursts above the current rate, the baseline dropped
persistence rows (9,062–9,104 per run at 5,500/s) and evicted the dashboard when
its outgoing queue filled. Both are now zero at every measured rate, stored rows
at 5,500/s rose from ~15,500 to 24,571, and backend CPU fell at every rate.

Receive-to-detection stayed at or below 1.5 ms p99 in every run at every stage,
including the baseline. The measured limits are delivery policy and single-event-loop
throughput, not detection cost.

These runs use modeled local bursts, a single dashboard client, and a fresh
database per scenario. They are not live-traffic evidence and do not replace the
soak below.

## Statistics query scaling

The statistics endpoints previously aggregated every stored opportunity inside
the requested window on each five-second poll, and `/api/system/stats` requests
two all-history aggregates. Measured on synthetic databases of 100,000, 1M and
4M opportunities spread over 30 days:

| Stored opportunities | `/api/system/stats` before | after |
| ---: | ---: | ---: |
| 100,000 | 532 ms | 100 ms |
| 1,000,000 | 6.8 s | 600 ms |
| 4,000,000 | 51.0 s | 631 ms |

A per-minute, per-pair rollup is now maintained as opportunities are written, so
these queries read one row per minute and pair. The rollup grows with elapsed
time rather than opportunity volume: it held 388,796 rows for the 4M-row
database, and a higher rate over the same 30 days would not enlarge it. A
database written before the rollup existed is backfilled once on startup, which
took 0.3 s, 4.0 s and 12.9 s for the three sizes.

Results were checked against the previous full-scan queries on all three
databases, with time frozen so both sides used identical window cutoffs. All
eight query variants matched, including windows starting mid-minute and
sub-minute timeseries buckets, which do not use the rollup.

These are synthetic databases with uniformly distributed timestamps across nine
pairs. Real history may cluster differently, and no measurement covers a
database larger than 4M rows or the rollup's own growth beyond 30 days.

## Live observations

Two short live runs validate the observer and the current 60-second book-age limit:

- [`soak_smoke_5m_2026-09-05.md`](../artifacts/benchmarks/soak_smoke_5m_2026-09-05.md)
- [`soak_validation_60s_window.md`](../artifacts/benchmarks/soak_validation_60s_window.md)

The five-minute run observed 29,001 Coinbase events, 2,042 Gemini events, and 1,760
Binance.US events with no adapter reconnects or detected sequence gaps. A subsequent
90-second run kept all 27 configured books eligible for every successful sample.

These are smoke tests, not long-duration reliability evidence.

## Remaining validation gap

A documented 24-hour live soak is still required. It should capture:

- memory usage and start-to-end drift
- reconnect and sequence-gap counts per exchange
- book eligibility and maximum update age
- recovery duration after disconnects
- persistence queue drops and WebSocket client overflows
- background-task and observer HTTP failures
- process crashes or restarts

Run the observer as described in [`BENCHMARKS.md`](BENCHMARKS.md) and commit the
generated report only after the full run completes.
