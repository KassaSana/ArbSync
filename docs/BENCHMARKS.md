# Benchmarks

This repo ships two benchmark paths:

## 1. Detector Microbenchmark

Purpose:
- isolate `ArbitrageDetector.detect_for_pair()`
- measure pure comparison latency without socket IO, parsing, or persistence

Command:

```bash
uv run python tools/benchmark.py
```

Current output:

```text
iterations=100000
throughput_per_minute=16454491
p50_latency_us=3.42
p95_latency_us=3.88
```

## 2. End-to-End Synthetic Benchmark

Purpose:
- measure ingest-on-socket to arbitrage-opportunity-emitted latency inside one process
- include synthetic websocket receive, JSON parse, book update, and detector invocation

Command:

```bash
uv run python tools/bench_e2e.py --iterations 10000
```

Current output:

```text
iterations=10000
detections_timed=10000
episode_events=2
throughput_per_minute=1920581
p50_latency_us=16.00
p95_latency_us=17.33
p99_latency_us=27.29
max_latency_us=216.67
```

Notes:
- these numbers are measured under synthetic local load, not against the live internet
- the end-to-end benchmark uses an in-process synthetic websocket server
- the detector microbenchmark is not a valid claim for full pipeline throughput

Raw persisted results live in
[`../artifacts/benchmarks/results.json`](../artifacts/benchmarks/results.json).

## Connected-dashboard burst profiling

For process CPU/RSS, receive-to-detection and sender-to-detection p95/p99,
queue drops, browser frame/long-task measurements, and separate Python/React
profiles, see
[the connected-dashboard investigation](../artifacts/benchmarks/performance/README.md).
This exercises the production handler and an actual production-built React
dashboard; the older synthetic benchmark above does not include those paths.

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

## Fee-adjusted survival

`tools/fee_survival.py` re-reads stored opportunity episodes and charges a per-side taker
fee on both legs at each episode's peak spread, reporting how many remain positive, the
net value paid once per episode at the size recorded at that peak, and the lifetime
distribution of closed episodes. Built-in scenarios are illustrative;
`--fee EXCHANGE=PCT` replaces them, `--start`/`--end` bound the window, and `--json`
emits a document. It opens the database read-only and can run against a live one.

```bash
uv run python tools/fee_survival.py --database var/arb.sqlite3   --start 2026-09-16T10:25:38Z --end 2026-09-16T14:25:38Z
```

The result for the four-hour soak is recorded in
[VALIDATION.md](VALIDATION.md#fee-adjusted-survival-of-the-soaks-opportunities).

## 3. Live Soak Observer

On Windows, run the launcher. It starts the backend, waits for readiness, blocks
system sleep for the duration, samples, and stops the backend on exit:

```powershell
.\tools\run_soak.ps1
```

It defaults to a 24-hour run at a 60-second sample interval, which gives roughly 1,441
samples. Shorter runs take `-DurationSeconds` and `-SampleSeconds`; the required bar is
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
