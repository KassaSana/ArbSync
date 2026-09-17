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
dashboard build, and coverage gates: 85% over the whole backend package and 75%
statements/lines over every dashboard source file.

### Release preparation verification (2026-09-12)

Local Windows verification passed the backend suite, strict mypy, Ruff, dashboard
typecheck/lint/tests/build, Markdown filesystem-link checks, and a wheel plus source
distribution build and content/hash inspection. The new release checker also rejects
version drift, missing artifact metadata, and accidental runtime/environment files.
Actionlint accepted the expanded Windows/Ubuntu workflow matrix. At the time, hosted
execution of that matrix, a fresh candidate secret/license review, and final publication
were still outstanding; the [2026-09-16 section](#release-candidate-verification-2026-09-16)
below records them. See [the release process](RELEASING.md) for the reproducible commands
and evidence limits.

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

The scheduled/PR audit workflow was committed locally at that point; its hosted
execution is recorded in the 2026-09-16 section below. Advisory results are a
point-in-time check, not evidence that every dependency is safe.
See the [security policy](../SECURITY.md#dependency-maintenance-and-audit-triage) for
coverage limits, reproduction commands, and triage rules.

### Release candidate verification (2026-09-16)

Candidate commit `dc83459cf967cdbd4b96249798fd8f3c9c6261eb` ("Prepare the 0.1.0 alpha
release") was verified on Windows 11 10.0.26200 with Python 3.12.10, Node.js 26.5.1,
uv 0.12.10, and npm 11.17.0, and on hosted runners:

- Hosted CI run [35113421357](https://github.com/KassaSana/ArbSync/actions/runs/35113421357)
  passed `backend` and `frontend` on `ubuntu-latest` and `windows-latest` with Python 3.11
  and Node.js 22; the dependency audit run
  [35113421340](https://github.com/KassaSana/ArbSync/actions/runs/35113421340) passed its
  Linux/Windows Python and npm jobs on the same push.
- Locally: 307 backend tests passed with 94.15% coverage against the 85% gate; strict
  mypy, `ruff check server tools`, `ruff format --check server tools`, and actionlint
  1.7.12 reported no issues. The dashboard passed `npm ci`, typecheck, ESLint, 99 tests
  with 83.06% statement coverage against the 75% gate, and the production build.
- The backend suite also passed in isolated environments on Python 3.13.15 and 3.14.7
  (307 tests each), which is the evidence for those classifiers in `pyproject.toml`.
- `tools/check_release.py --require-clean` passed; the synthetic replay, both documented
  benchmarks, and the installed `arbsync` help, example generation, overwrite refusal,
  and missing-configuration paths behaved as documented. Re-running the benchmarks
  rewrites `artifacts/benchmarks/results.json`; the committed values are the citation.
- A clean checkout built `arbsync-0.1.0-py3-none-any.whl` and
  `arbsync-0.1.0.tar.gz`; the checker's `checks.json` records their
  contents and SHA-256 hashes, which the annotated release tag repeats.
- `pip-audit` 2.10.1 found no known vulnerabilities in the 59-entry all-extras export of
  the unchanged lockfile; `npm audit --include=dev --audit-level=low` found none in the
  383-package dashboard tree.
- Gitleaks 8.30.1 scanned every commit on every ref (`--log-opts=--all`, 149 scanned)
  and the built artifacts with no findings. Its worktree scan reported only four
  false positives inside ignored, untracked tooling caches (its own README and the
  cached Playwright driver bundle), none in tracked content. The GitHub API reported
  secret scanning and push protection enabled with zero alerts. Dependabot security
  alerts are not enabled on the repository; advisory coverage comes from the audit
  workflow.
- The dependency license inventory was re-derived from both lockfiles; see the
  [September 16 refresh](DEPENDENCY_LICENSES.md#lockfile-refresh-september-16-2026).

Documentation corrections found during this pass were committed after the candidate
commit above, so the tagged release commit is later; its own hosted CI run and rebuilt
artifact hashes are recorded in the release notes and tag message.

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

## Live soak (2026-09-16)

A four-hour uninterrupted live soak against all 27 configured books completed on
2026-09-16 and is the current long-duration reliability evidence:

- Report: [`soak_4h_2026-09-16_062525.md`](../artifacts/benchmarks/soak/soak_4h_2026-09-16_062525.md)
  (raw per-sample JSONL retained locally beside it; it is ignored by Git)
- Duration: `14400.1 s` requested and achieved, 60-second samples, status `complete`,
  zero excessive sample gaps
- Backend and observer commit: `a9d7a358e3674fe8e01bdaec11f7998d19792212`, clean checkout
- Environment: Windows 11 10.0.26200, Python 3.12.10 project virtualenv, single developer
  workstation on mains power; backend launched by `tools/run_soak.ps1` under a Windows
  Scheduled Task so that no interactive session owned its process tree
- Configuration SHA-256: `851209a633d95f148f871004fb8cb08b9a749ce60dca47ec5d0a3f16f35d35bb`

Results:

- **Process:** no restart, no counter reset, no background-task failure. RSS ranged
  52.6–119.1 MiB, mean 104.2 MiB, start-to-end change **+7.4 MiB** with no upward trend;
  the minimum coincides with Windows trimming the working set during the outage below,
  and the maximum with the Binance.US resync rebuilding nine books.
- **Ingestion:** 1,376,144 Coinbase, 107,233 Gemini, and 87,836 Binance.US events with
  **zero detected sequence gaps** on any exchange. 246 theoretical opportunities recorded.
- **Eligibility:** every configured book was observed in every successful sample. Each
  book was eligible in 236–237 of 238 samples; every ineligible observation carries the
  reason `disconnected` and lies inside the two events below. No book was ever stale,
  incomplete, discontinuous, or crossed while its adapter was connected. Recovery to
  eligible completed within one 60-second sampling interval in every case.
- **Reconciliation:** the snapshot reconciler raised unconfirmed mismatch warnings at a
  steady 2–5 per minute across all exchanges, the expected noise of comparing a live
  stream against a non-atomic REST snapshot. Only **four** were confirmed after three
  consecutive observations: Gemini `LTC-USD` (11:38Z), Gemini `DOT-USD` (11:55Z and
  12:15Z), and Binance.US `DOT-USDT` (13:18Z), all price divergences on thin markets.
  Each forced an adapter resync; recovery took 1.8 s, 2.3 s, 2.5 s, and 13.9 s.
- **WebSocket delivery:** the built-in consumer received 556,260 frames with zero invalid
  frames and zero stream-sequence gaps; zero client queue overflows and zero sender
  failures. It reconnected once during the outage below, disconnected for at most 31.6 s.

Two events during the run deserve explicit review rather than a summary line:

1. **Host network outage, 13:25–13:32Z.** Keepalive pings timed out on all three exchange
   sockets at once, REST snapshot fetches failed with connection timeouts, and the
   observer's own loopback requests to the backend failed for three consecutive samples
   (two `ReadTimeout`, one `RemoteProtocolError`). Windows logged power-source changes at
   13:32:32Z and 13:33:00Z. Three independent exchanges do not fail simultaneously; the
   workstation's connectivity did. The backend did not restart. Disconnected books were
   excluded from detection for the duration, all adapters reconnected with exponential
   backoff, and every book was eligible again by 13:32:38Z. The four samples reporting the
   backend not ready fall in this window. This is environmental, but it exercised the
   disconnect, exclusion, and recovery path under real conditions.
2. **Whole-exchange resync on Binance.US, 13:18:27–13:18:41Z.** One confirmed `DOT-USDT`
   price drift caused all nine Binance.US books to report `disconnected` for one sample,
   because that adapter carries every pair on a single combined stream and resyncs the
   whole socket. Detection excluded them correctly and recovery took 13.9 s, but a
   single thin pair should not blink the whole venue; see ARB-028.

The carry-forward observation from the September 13–14 attempt is resolved: `gemini:DOT-USD`
never went stale in this run. Its p95 age was 6.2 s and maximum 32.8 s, both under the
60-second limit; its two confirmed price drifts were caught and repaired by reconciliation.

Earlier attempts are retained as diagnostic evidence only. The September 12–13 run
observed 923 samples with no restart, reset, gap, or unbounded RSS trend, but contained
nine sample gaps including one of 9.5 hours. The September 13–14 run was cut short at 107
minutes when its launcher process was killed. The two short smoke runs,
[`soak_smoke_5m_2026-09-05.md`](../artifacts/benchmarks/soak_smoke_5m_2026-09-05.md) and
[`soak_validation_60s_window.md`](../artifacts/benchmarks/soak_validation_60s_window.md),
validated the observer and the 60-second book-age limit.

### Fee-adjusted survival of the soak's opportunities

The 246 opportunities recorded during the soak window were re-read from the soak
database with [`tools/fee_survival.py`](../tools/fee_survival.py) and charged an
assumed taker fee on both legs. The fee tiers are illustrative base-tier public
schedules, not live quotes; the conclusion does not depend on their exact values
because the deficit is a multiple of the spread, not a fraction of it.

| Per-side taker fee (Coinbase / Gemini) | Rows surviving | Distinct quote pairs | Net at top-of-book size |
| --- | ---: | ---: | ---: |
| 0.60% / 0.40% (base retail) | 0 / 246 | 0 | $0.00 |
| 0.35% / 0.25% (mid-volume tier) | 0 / 246 | 0 | $0.00 |
| 0.10% / 0.10% (institutional) | 36 / 246 | 22 | $3.91 |
| 0% (theoretical upper bound) | 246 / 246 | 108 | $76.21 |

What the rows say beyond the fee table:

- The spread distribution is p50 0.124%, p99 0.35%, maximum 0.353%. A round trip on two
  US retail venues costs 0.5–1.2%, so no threshold setting closes the gap.
- Every opportunity is Coinbase–Gemini in USD. During this soak Binance.US was
  configured for its USDT markets, and USD and USDT are distinct markets, so it contributed
  none; the roster was effectively two venues for detection. The shipped configuration now
  subscribes to Binance.US's USD markets, which the adapter normalizes to the same canonical
  pairs, so later runs compare all three venues.
- 76% of rows are DOT, UNI, and AAVE; BTC has four and LTC one. Median capturable
  notional at top of book is $97, maximum $4,229. These are thin-book dislocations of
  the kind the reconciler also flagged, not liquid mispricing.
- The 246 rows collapse to 108 distinct resting-quote pairs. The detector reports a
  persisting spread on every book update, which is right for observability but means
  the naive sum of `theoretical_profit` ($149.55) double counts levels that could be
  taken once. The tool pays each distinct pair once.

Survival here is an upper bound. Slippage, latency, inventory pre-positioning on both
venues, partial fills, and withdrawal costs are still excluded. The number that is
useful to a person is therefore not the arbitrage but the venue price difference
against the venue fee difference, which is larger; see the scope section of the README.

## Remaining validation gap

The four-hour requirement is met. The following would strengthen the evidence but are not
prerequisites for the first alpha release:

- A 24-hour run on a dedicated machine. Four hours establishes recovery behavior and the
  absence of short-horizon leaks; it cannot rule out slower growth or daily-cycle effects.
- A run with independently connected dashboard clients under real browser load, in addition
  to the observer's lightweight consumer. Rendering evidence remains the connected-dashboard
  benchmark above.
- Host-connectivity attribution in the observer (ARB-029), so an outage like the one above
  is recorded as such rather than inferred afterwards from correlated failures.
- Per-pair resynchronization on Binance.US (ARB-028), so one confirmed drift on a thin
  pair no longer removes the whole venue from detection for the recovery interval.
