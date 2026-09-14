# Validation status

This page records what has been verified and what evidence is still missing. It is
not an implementation checklist; completed design decisions belong in the code,
tests, and focused documents such as [`RESYNC.md`](RESYNC.md).

## Automated verification

The backend suite passes in full on every supported commit; run
`uv run pytest -q server/tests` for the current count rather than relying on a
number recorded here, which cannot stay accurate between edits.

The suite covers:

- snapshot and delta application
- duplicate, overlapping, out-of-order, and missing exchange updates
- exchange-specific snapshot recovery
- disconnect invalidation and cold-start delta rejection
- incomplete and crossed snapshot rejection, including adapter resynchronization before
  later deltas can restore eligibility
- reconciliation price and aggregate-size comparison, transient mismatch reset, confirmed
  invalidation, recovery cooldown, completion and timeout observation, REST failure isolation,
  and multi-pair cadence
- stale, incomplete, and crossed-book exclusion
- detection using only eligible venues
- synthetic fixture replay for Gemini, Coinbase, and Binance.US
- bounded persistence and WebSocket queues, including initialization, flush, commit,
  full-queue, worker-failure, and shutdown fault paths
- WebSocket sender cleanup for expected disconnects, unexpected serialization failures,
  queue overflow, and cancellation, including metric and payload-safe logging behavior
- coalesced dashboard delivery, immediate invalidation ordering, and suppression
  of unchanged book updates, including a sequence-gap storm
- cached top-of-book invalidation across size changes, level deletion, sequence
  gaps, snapshot recovery, and disconnect
- per-minute statistics rollup: backfill of pre-rollup history, consistency
  across separate write batches, exact cutoff membership for a window starting
  mid-minute, documented binary64 tolerance, and sub-minute buckets bypassing the rollup
- WebSocket reconnection and state restoration
- dashboard boundary validation for every REST shape and live message type, including
  malformed JSON, missing fields, invalid decimal and nanosecond strings, and unknown types
- REST, readiness, metrics, persistence, and statistics behavior
- runtime configuration type, range, exchange, symbol, and normalized-duplicate validation
- installed-wheel metadata, package contents, console startup outside the checkout,
  missing-configuration errors, and safe example generation
- explicit history pruning: exact boundaries, partial-minute rollup repair, active writes,
  restart/statistics, lock contention, query-budget expiry, and atomic failure rollback

CI also runs strict mypy, Ruff, frontend type checking, ESLint, the production
dashboard build, and coverage checks for the order-book and detector modules.

### Release preparation verification (2026-09-12)

Local Windows verification passed the backend suite, strict mypy, Ruff, dashboard
typecheck/lint/tests/build, Markdown filesystem-link checks, and a wheel plus source
distribution build and content/hash inspection. The new release checker also rejects
version drift, missing artifact metadata, and accidental runtime/environment files.
Actionlint accepted the expanded Windows/Ubuntu workflow matrix. Hosted execution of
that matrix, a fresh candidate secret/license review, and final publication remain
outstanding; these checks do not establish a published release. See
[the release process](RELEASING.md) for the reproducible commands and evidence limits.

### Dependency maintenance verification (2026-09-12)

The ARB-017 refresh passed dashboard type checking, ESLint, tests (including direct
statistics loading and navigation after the React Router upgrade), and production
build on Windows with Node.js 26.5.1. A fresh `npm ci --ignore-scripts --no-audit`
also passed, followed by `npm audit --include=dev --audit-level=low` with no known
vulnerabilities. The initial npm audit reported 14 affected packages; patched direct
and transitive dependencies resolved the findings without suppressions.

`pip-audit` 2.10.1 found no known vulnerabilities in the all-extras export of the
unchanged Python lockfile, including a run explicitly using local Python 3.12.
Actionlint 1.7.12 accepted both workflows, and static checks verified Dependabot
ecosystems, SHA pins, read-only permissions, and disabled checkout credential storage.
GitHub secret scanning and push protection were confirmed enabled by the repository API.

The new scheduled/PR audit workflow is committed locally; hosted execution on Linux
and Windows with Python 3.11 and Node.js 20 remains to be verified after pushing.
Advisory results are a point-in-time check, not evidence that every dependency is safe.
See the [security policy](../SECURITY.md#dependency-maintenance-and-audit-triage) for
coverage limits, reproduction commands, and triage rules.

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

The [2026-09-12 decision record](../artifacts/benchmarks/performance/ARB-022.md)
adds four valid short runs and three 60-second repeated profiles against the current
USD/USDT roster and dashboard, with no missing events or queue losses. It records
process CPU, scheduling lag, exclusive function-time attribution, SQLite worker-thread
CPU, and explicit gates for future optimizations. The worker used 0.094-0.203 seconds
of CPU per 60-second run at 1,100 events/second, so persistence does not cross its
investigation gate. The main-thread profile reports elapsed self time rather than exact
per-stage CPU; the worker measurement uses a quantized Windows per-thread CPU clock.

The [2026-09-13 optimization measurement](../artifacts/benchmarks/performance/optimization-20260913.md)
compares the commit before four optimization commits against the commit after them, both
measured on the same host in the same session. At 1,100 events/second mean process CPU fell
from 27.13-29.35% to 9.75-14.87% of one core and send-to-detection p99 fell from 298.5-644.7 ms
to 46.5-176.6 ms, with no missing events or queue losses in any run. At the representative
110/s rate the difference is within run-to-run spread. Two of thirteen runs on the changed
code failed to exit within the harness's 60-second graceful window, against zero of nine on
the baseline; measurements were complete in both cases. A further 144 shutdowns did not
reproduce it, including 20 at the same 66,000-event volume as the runs that hung, and tests
now rule out the candidate mechanism in the reused SQLite writer connection. No attempt left
a lingering non-daemon thread or failed to exit. The benchmark backend now
times each shutdown phase, dumps every thread's stack from inside a hang, and records
non-daemon threads that outlast a brief join, so a single future occurrence is diagnosable
without repeating runs. `tools/shutdown_probe.py` exercises that path at about 1.1 seconds
per shutdown; it reports lifecycle behavior only and is not a capacity benchmark.

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

Canonical opportunity columns remain decimal text and round-trip without binary-float
conversion. Aggregate spread and profit statistics intentionally use SQLite `REAL`
(binary64), matching the full-scan queries they replaced. They are suitable for dashboard
observability but may contain normal floating-point rounding and are not exact accounting
values.

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
- built-in WebSocket delivery frames, sequence continuity, reconnects, and outages
- background-task and observer HTTP failures
- process crashes or restarts

Run the observer as described in [`BENCHMARKS.md`](BENCHMARKS.md) and commit the
generated report only after the full run completes.

An attempted run on September 12-13 observed 923 successful samples and no process
restart, counter reset, sequence gap, background failure, or unbounded RSS trend, but it
contained nine sample gaps over two minutes, including a 9.5-hour gap. It is retained as
local diagnostic evidence and does not satisfy the uninterrupted requirement. The observer
now fails fast and labels such a run `interrupted` instead of allowing elapsed wall time to
produce a misleading `complete` status.
