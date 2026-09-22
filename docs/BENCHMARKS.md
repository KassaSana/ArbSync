# Benchmarks

ArbSync's performance evidence is the connected-dashboard harness described below. The
earlier detector microbenchmark and in-process synthetic websocket benchmark
(`tools/benchmark.py`, `tools/bench_e2e.py`) were retired on 2026-09-21: their committed
September 8 figures predated episode tracking and depth/fee ledgers, so they no longer
measured the current opportunity path, and the harness below covers everything they did
plus delivery, persistence, and browser cost. Their last results survive in git history
under `artifacts/benchmarks/results.json`.

## Connected-dashboard burst profiling

For process CPU/RSS, receive-to-detection and sender-to-detection p95/p99,
queue drops, browser frame/long-task measurements, and separate Python/React
profiles, see
[the connected-dashboard investigation](../artifacts/benchmarks/performance/README.md).
This exercises the production handler and an actual production-built React
dashboard.

The current harness preserves Binance.US USDT markets and derives its 27-book roster
from the adapters (18 distinct base/quote dashboard rows). In `--profile` mode it
also writes `stages.json` with exclusive function times for JSON, Decimal conversion,
sorted-level mutation, detection, persistence on the main thread, and delivery.
These are elapsed self times, not exact stage CPU measurements. Event-loop waits are
reported separately; SQLite worker CPU is outside cProfile. Negative profiler times
invalidate attribution. Process CPU and event-loop lag come from unprofiled runs.
The current dashboard has no mounted React profiling wrappers, so
`react_profile_available: false` explicitly records that missing measurement.

The current verification summary, the four-hour live soak, and the remaining evidence
gaps are tracked in [VALIDATION.md](VALIDATION.md).

The [2026-09-18 focused investigation](../artifacts/benchmarks/performance/optimization-20260918.md)
measures schema-v4 route-ledger computation and capture writing. Pricing only the requested
route reduced ledger latency by 82–84%; batching capture writes off the event loop reduced
the measured large-frame/backlog heartbeat stalls while preserving comparable drain time.
It also records the workloads where neither change matters and the profiler follow-up.

## Fee-adjusted survival

`tools/fee_survival.py` re-reads the exact net executable values stored with schema-v4
episodes and reports survival, insufficient-depth counts, and net value by configured
notional. It does not reconstruct current product results from top-of-book prices or a
new fee schedule. `--start`/`--end` bound the window and `--json` emits a document. It
opens the database read-only and can run against a live one. Repeated `--fee
EXCHANGE=PCT` arguments explicitly request the older counterfactual top-of-book analysis
for historical research; that output is not labelled as product pricing.

```bash
uv run python tools/fee_survival.py --database var/soak_arb045.sqlite3 --start 2026-09-22T19:35:44Z --end 2026-09-22T23:35:46Z
```

The result for the 2026-09-22 four-hour soak is recorded in
[VALIDATION.md](VALIDATION.md#stored-net-survival-2026-09-22).

## Live soak observer

On Windows, run the launcher. It starts the backend, waits for readiness, blocks
system sleep for the duration, samples, and stops the backend on exit:

```powershell
.\tools\run_soak.ps1
```

It defaults to a 24-hour run at a 60-second sample interval, which gives roughly 1,441
samples. The default report name always reads `soak_24h_<stamp>.md` whatever the
duration, so pass `-Output` (for example `soak_4h_<stamp>.md`) for shorter runs, and
`-Config` to point a run at its own database. Shorter runs take `-DurationSeconds` and `-SampleSeconds`; the required bar is
four uninterrupted hours, so `-DurationSeconds 14400` is the shortest qualifying run.
Sample starts may be at most twice the configured interval apart by default. A larger
gap stops the run immediately, exits nonzero, and leaves the report marked `interrupted`;
`-MaxSampleGapSeconds` sets a different explicit limit.
Use `-Config <path>` to select a configuration (paths containing spaces are supported).
It defaults to the repository's `config.toml`. Default report and backend log names
include the start time to distinguish runs on the same day.
The launcher also writes a sibling `.jsonl` file containing each successful sample's
API payloads, metrics, timestamp, and RSS, or the sampling error. Raw evidence is
ignored by Git and refuses to overwrite an existing file. The Markdown report records
the configuration SHA-256 and missing configured-book observations. Keep the selected
configuration with the raw artifacts when archiving a run.

The observer can also be driven directly:

```bash
uv run python tools/soak.py \
  --duration-seconds 86400 \
  --sample-seconds 60 \
  --pid <backend-pid> \
  --config config.toml \
  --samples-output artifacts/benchmarks/soak/soak_YYYY-MM-DD.jsonl \
  --output artifacts/benchmarks/soak/soak_YYYY-MM-DD.md
```

Pass the pid of the interpreter actually running `arb.main`. A virtualenv or `uv
run` launcher may re-exec the real interpreter as a child process, and sampling
the launcher reports a flat few-megabyte RSS rather than the backend's memory.
The launcher script resolves this automatically.

The observer samples per-exchange ingest volume, adapter reconnects and gaps,
canonical book eligibility and age, readiness, opportunities, background-task
failures, HTTP failures, recovery durations, and backend RSS. It also keeps a
lightweight `/ws/live` consumer connected, validates its initial state and stream
sequence, and reports frames, reconnects, malformed messages, and delivery outages.
Use `--no-websocket` only for diagnostic runs; such a run is not release evidence. Each
sample also runs a bounded TCP connect to public endpoints (`1.1.1.1:443`, then
`8.8.8.8:443`; override with repeatable `--host-probe HOST:PORT`) concurrently with the
backend requests, so the report can attribute a backend failure to lost host connectivity
rather than to the backend, and summarizes contiguous outage windows at sample resolution.
Probe failures are recorded but never interrupt a run; `--no-host-probe` disables the probe
and leaves backend outages unattributed. The
observer rewrites the report after every sample, so an interrupted run still leaves a
readable report marked `interrupted`. Windows sleep prevention cannot override forced
sleep, shutdown, lid policies, or host suspension. Durations below the four-hour bar are
useful as smoke tests but must not be described as the required soak, and any soak result
must state the duration actually achieved.

Start a soak from an interactive shell that outlives whatever launched it. A run started as
a child of a short-lived process is killed with its parent, which has already ended one
attempt at 107 minutes.

Reports also capture labeled persistence, WebSocket, reconciliation, and background
failure counters, observed backend restarts, and missing RSS samples. Counters first
appearing after the initial sample start at zero; a restart or observed counter reset
invalidates operational deltas. Adapter deltas are invalid after a restart as well.
Restart detection compares backend start timestamps at successful samples, so it cannot
count multiple restarts between samples. Recovery times have the sampling interval's
resolution, and transient eligibility changes between samples can be missed.

Run provenance records the observer's platform, Python, checkout commit and dirty state,
target URL, and sampled PID. For a published report, also record the backend's exact
commit, configuration, and environment; the observer may target a different checkout
or machine. Review failures and all configured books before drawing conclusions:
`complete` means the observer finished its requested duration, not that the run passed.
The built-in consumer establishes continuous protocol-delivery evidence with low overhead;
the connected-dashboard benchmark remains the evidence for rendering in a real browser.
