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
- filtered history pagination: every filter and their conjunction, start-range boundaries,
  orphaned rows as closed, id tiebreaks across page breaks, stability across concurrent
  inserts and closes, malformed/foreign cursors, limits, the query budget, index-backed
  plans without sorts, and bounded JSONL export with truncation, resume, and serialization
- runtime configuration type, range, exchange, symbol, and normalized-duplicate validation
- installed-wheel metadata, package contents, console startup outside the checkout,
  missing-configuration errors, and safe example generation
- explicit history pruning: exact boundaries, partial-minute rollup repair, active writes,
  restart/statistics, lock contention, query-budget expiry, and atomic failure rollback
- deterministic capture/replay, recorded-time snapshot completion and buffering,
  disconnect/reconnect generations, scoped recovery, exact age-expiry boundaries, episode
  boundaries and orphan recovery, decimal-exact depth walking, matched-route quantity,
  explicit insufficient depth, fee-aware ledgers, schema-v4 persistence, and dashboard
  rendering of peak, net, and lifetime tiers
- fill-rate statistics: every configured book counted per sample with per-reason
  ineligibility (missing, never-initialized, disconnected, too old), capped versus
  full-depth shortfalls, the sample grid and missed ticks, quiet books, restarts and
  configuration changes across a window, filtered windows equal to the reference reducer,
  the schema v4 to v5 migration, pruning, the shutdown flush, and replay/live agreement on
  one observation sequence

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
  and missing-configuration paths behaved as documented. (The two synthetic benchmark
  scripts and their `results.json` were retired on 2026-09-21; see `BENCHMARKS.md`.)
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

## Connected-dashboard performance

A burst investigation dated 2026-09-08 measured the production ingestion path with
a real headless browser running the built dashboard, at 110, 1,100 and 5,500
events/s. Method, full results and limits are in
[`../artifacts/benchmarks/performance/README.md`](../artifacts/benchmarks/performance/README.md).

This closed a gap the earlier, since-retired synthetic figures concealed. Detector-only timing
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

The [2026-09-18 depth-ledger and capture-writer investigation](../artifacts/benchmarks/performance/optimization-20260918.md)
found that one requested three-venue route redundantly priced all six directed routes:
route-ledger median latency fell 82–84% after removing that amplification. Capture writing
kept up at modeled 110 and 1,100 frames/s both before and after, but a large frame or queued
gzip drain blocked the event loop for 25–88 ms; bounded batched thread writes reduced the
measured heartbeat gaps to 11–16 ms without changing the queue or drop policy.

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

## History query scaling

`tools/perf_history.py` seeded a 1M-episode, 2.2 GB database over 30 days with the venue
mix of a real local database and a production-size pricing ledger per row, without
`ANALYZE` statistics (production databases never collect them). An earlier run, before
`idx_episodes_close_start` existed and with an earlier revision of the tool that also ran
`ANALYZE`, took 1.6–14.3 s for `state=open` pages and 1.5 s for a close reason matching
nothing, both past the 0.5 s page budget
([before](../artifacts/benchmarks/performance/history-20260922-before-index.json)). With the
index ([after](../artifacts/benchmarks/performance/history-20260922.json)), every measured
shape (unfiltered, last hour, one day mid-history, pair, pair and day, dense and sparse
routes, open, closed, rare and absent close reasons) served first and mid-traversal 100-row
pages in 2–15 ms from an index in result order with no sort. The remaining slow case is a
buy/sell venue pairing with no rows, scanned across a full day of other traffic: 804 and
812 ms in two runs, which the budget turns into a fast 503.
Adding the index to that existing 1M-row file, as startup does on upgrade, took 1.8 s
([upgrade run](../artifacts/benchmarks/performance/history-20260922-index-upgrade.json)).

A 100,000-row export produced 194 MB in 2.1 s. Its on-loop work, encoding one 250-row page
with the stored ledger JSON spliced in, took at most 1.43 ms. A 1 ms sleeper sampled
event-loop lag throughout: p99 1.44 ms and maximum 23 ms during the export, against an idle
baseline of p50 14.9 ms and maximum 28 ms that reflects Windows timer resolution rather than
load. This bounds the export's own blocking; it is not an ingestion-latency measurement under
live traffic. Measured on the development workstation with SQLite 3.49.1; absolute times
depend on storage and cache state.

## Live soak (2026-09-22)

A four-hour uninterrupted live soak of the current code against all 27 configured books
completed on 2026-09-22. It is the current long-duration reliability evidence and the first
run to cover schema-v4 depth/fee ledgers, Binance.US single-pair resynchronization
(ARB-028), the soak host-connectivity probe (ARB-029), the research-correctness contracts of
ARB-036 through ARB-041, and schema-v5 fill-rate buckets (ARB-042).

- Report: [`soak_4h_2026-09-22_153534.md`](../artifacts/benchmarks/soak/soak_4h_2026-09-22_153534.md)
  (raw per-sample JSONL retained locally beside it; it is ignored by Git)
- Window: 2026-09-22 19:35:44Z to 23:35:45Z; `14400.6 s` achieved of `14400 s` requested,
  60-second samples, status `complete`, zero excessive sample gaps
- Backend and observer commit: `012537c1e75dd66490fa327be2db48039b640582`, clean checkout
- Environment: Windows 11 10.0.26200, Python 3.12.10 project virtualenv, single developer
  workstation; `tools/run_soak.ps1` launched under a Windows Scheduled Task, backend and
  observer verified to descend from the Task Scheduler service rather than any interactive
  or tool session
- Configuration: the shipped `config.toml` with only `server.database_path` pointed at a
  fresh `var/soak_arb045.sqlite3`, so every stored row belongs to this run; SHA-256
  `7bb2f63f471b84f786004ea812d5a1988580d15c3a238a8855c5f0461fc3c7f1`. Binance.US used its
  USD markets, so all three venues took part in detection.

Results:

- **Process:** no restart, no counter reset, no background-task failure, no HTTP failure.
  RSS ranged 86.2–110.5 MiB, mean 103.5 MiB, start-to-end change **−18.3 MiB**.
- **Readiness and eligibility:** 241 of 241 samples ready. Every configured book was
  observed and eligible in all 241 samples, with no missing configured-book observation.
  The oldest eligible receipt was Binance.US `DOT-USD` at 38.7 s, inside the 60-second limit.
- **Ingestion:** 1,535,669 Coinbase, 112,388 Gemini, and 92,286 Binance.US events with
  **zero detected sequence gaps**. 497 theoretical opportunity episodes were recorded, all
  closed as `spread_closed`.
- **Host connectivity:** the probe reached `1.1.1.1:443` and `8.8.8.8:443` in 241 of 241
  samples (maximum 72 ms); no sample saw the backend unreachable, so no outage needed
  attribution by hand.
- **Recovery:** Binance.US repaired two confirmed `UNI-USD` drifts through
  `arb_adapter_pair_resyncs_total{trigger="external"}` = 2 with **zero** Binance.US
  reconnects, so its other eight books kept flowing, which is the ARB-028 behavior the
  2026-09-16 run could not show. Gemini had six confirmed price drifts (`DOT-USD` four
  times, `LTC-USD` twice); Gemini has no scoped resync, so each one reconnected that venue
  and completed in 2.0–2.7 s. These six are the six `reason="RuntimeError"` Gemini
  reconnects in the report: the label was then the adapter's reconnect-request exception
  class, not an unexplained failure. Since ARB-046 the label names the cause (here it would
  read `confirmed_drift`), and Gemini recovers such a drift by resubscribing only the pair. Coinbase reconnected once after `ConnectionClosedError`.
  Unconfirmed mismatch warnings (mostly Coinbase and Gemini size-only) remained the
  expected non-atomic comparison noise.
- **WebSocket delivery:** 604,034 frames on one connection with zero invalid frames,
  stream-sequence gaps, reconnects, client queue overflows, or sender failures.
- **Episodes:** peak spread p50 0.127%, p99 0.840%, maximum 1.089%; lifetime p50 0.36 s,
  p90 3.9 s, maximum 261 s. Venue pairs: Coinbase–Gemini 251, Binance.US–Coinbase 142,
  Binance.US–Gemini 104. `UNI-USD` accounts for 302 of 497.

### Stored net survival (2026-09-22)

Unlike the 2026-09-16 table below, this is read from the schema-v4 pricing ledgers the
product stored at each episode's open and peak, with the configured taker fees (Gemini
0.40%, Coinbase and Binance.US 0.60%) and matched depth, via
`uv run python tools/fee_survival.py --database var/soak_arb045.sqlite3 --start
2026-09-22T19:35:44Z --end 2026-09-22T23:35:46Z`:

| Notional | Routes with positive stored net | Insufficient depth | Best stored net spread |
| ---: | ---: | ---: | ---: |
| 100 | 0 / 497 | 0 | −0.18% |
| 1,000 | 0 / 497 | 0 | −0.34% |
| 10,000 | 0 / 497 | 0 | −0.63% |
| 50,000 | 0 / 381 priced | 116 | −0.85% |

No stored route was net-positive at any notional, consistent with ARB-041's offline
finding. The best net spread worsens with size because depth impact adds to the fee.

### Fill rates over the soak window (2026-09-22)

The soak database's fill-rate buckets, summed with `/api/pricing/fill-rates` semantics over
the whole minutes inside the soak window (19:36Z to 23:35Z), give one session and one
configuration, **2,868 samples, zero missed, coverage 1.0**. Per venue across its nine
pairs and both sides (51,624 samples per notional):

| Venue | 100 | 1,000 | 10,000 | 50,000 | Ineligible book-samples |
| --- | ---: | ---: | ---: | ---: | --- |
| Binance.US | 100% | 100% | 88.9% | 53.5% | none |
| Coinbase | 100% | 100% | 100% | 100% | 9 `uninitialized` (its reconnect) |
| Gemini | 100% | 100% | 100% | 94.8% | 9 `disconnected`, 2 `uninitialized` (drift recoveries) |

Rates are `filled / (filled + insufficient_depth)` over eligible samples; the raw counts are
in the API response. Binance.US `AAVE-USD`, `DOT-USD`, and `LTC-USD` never filled 50,000
on either side. No shortfall occurred at the Binance.US 5,000-level cap
(`insufficient_at_depth_cap` = 0), so these are liquidity limits rather than subscription
limits. The ineligible samples line up with the reconnects above; none were read as depth
shortfalls.

## Live soak (2026-09-16)

A four-hour uninterrupted live soak against all 27 configured books completed on
2026-09-16. It was the evidence for the 0.1.0 release and is now historical: it predates
schema-v4 ledgers, ARB-028, and ARB-029, and the [2026-09-22 soak](#live-soak-2026-09-22)
supersedes it for current claims.

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
   because the version under test carried every pair on one recovery path and resynchronized
   the whole socket. Detection excluded them correctly and recovery took 13.9 s. ARB-028
   has since added scoped pair resynchronization; this soak predates that change.

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

This table predates schema-v4 fee ledgers and is historical evidence. The tool now reads
stored product net values by default; reproducing this older counterfactual requires
explicit `--fee` arguments because these episodes contain no measured depth ledger.

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
- The 246 rows collapse to 108 distinct resting-quote pairs. At the time the detector
  reported a persisting spread on every book update, so the naive sum of
  `theoretical_profit` ($149.55) double counted levels that could be taken once, and the
  tool paid each distinct pair once. Storage has since moved to episodes (ARB-031), which
  record that collapse directly; the tool now reads episodes and this analysis is not
  reproducible from a schema version 3 database.

Survival here is an upper bound. Slippage, latency, inventory pre-positioning on both
venues, partial fills, and withdrawal costs are still excluded. The number that is
useful to a person is therefore not the arbitrage but the venue price difference
against the venue fee difference, which is larger; see the scope section of the README.

## Gemini book audit (2026-09-22)

ARB-047 measured whether Gemini's stream-built books are wrong, and where, using Gemini's
`@depth20` top-20 snapshots, whose `lastUpdateId` shares the `@depth` id space. The
capture was a normal 45-minute pipeline run (all venues, reconciler, detector) with that
stream and `@trade` added (`tools/gemini_audit_capture.py`); `tools/gemini_book_audit.py`
compared each snapshot with the incremental book only when the book had applied exactly
that update id.

| Measurement | Result |
| --- | ---: |
| Aligned comparisons (all nine pairs) | 21,797 |
| Comparisons matching Gemini's top 20 exactly (price and size) | 21,797 |
| Snapshots skipped because the book never stopped on their id | 2,485 |
| Gemini trades | 1,214 |
| Trades better than the book's best whose price the stream showed within 2 s | 160 of 160 |
| REST comparison sides whose best price beat the stream book's | 112 of 720 |
| Of those, prices the stream had deleted / never announced | 99 / 13 |
| Of those, traded within 60 s (stream-book best: 21 of 720, 2.9 %) | 0 |
| Replayed episodes with a Gemini leg | 38 |
| Gemini price confirmed by the latest aligned snapshot / phantom | 37 / 0 |

Conclusion: the stream-built Gemini book is correct at every update id Gemini lets us check,
and trades never hit the extra levels Gemini's REST book shows; ARB-046's
stream-divergence diagnosis was a timing artifact of comparing across a one-second batch.
The Gemini-leg research data in this window is sound. One episode had no aligned snapshot
within 2 s of opening and was not judged. The episode replay started Binance.US at its
second connection: its first ended when an initial-sync REST fetch failed, which leaves no
capture frame, so that connection (about 40 s) cannot be replayed.

## Remaining validation gap

The four-hour requirement is met on the current code by the
[2026-09-22 soak](#live-soak-2026-09-22). The following would strengthen the evidence:

- A 24-hour run on a dedicated machine. Four hours establishes recovery behavior and the
  absence of short-horizon leaks; it cannot rule out slower growth or daily-cycle effects.
- A run with independently connected dashboard clients under real browser load, in addition
  to the observer's lightweight consumer. Rendering evidence remains the connected-dashboard
  benchmark above.
- Gemini's REST-confirmed drifts are false positives from stale REST levels (see the
  [Gemini book audit](#gemini-book-audit-2026-09-22)), so recovery triggered by them only
  rebuilds a book that was already right. ARB-048 replaces it with continuous exact-id
  verification; until then no soak has shown Gemini book verification running live for hours.
- A soak whose host network actually drops. The ARB-029 probe recorded no outage on
  2026-09-22, so its outage-window attribution is exercised only by automated tests.
