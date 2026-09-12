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
opportunities_emitted=10000
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

The current verification summary and remaining live-soak gap are tracked in
[VALIDATION.md](VALIDATION.md).

## 3. Live Soak Observer

On Windows, run the launcher. It starts the backend, waits for readiness, blocks
system sleep for the duration, samples, and stops the backend on exit:

```powershell
.\tools\run_soak.ps1
```

It defaults to the full 24-hour run at a 60-second sample interval, which gives
roughly 1,441 samples. Shorter runs take `-DurationSeconds` and `-SampleSeconds`.
Use `-Config <path>` to select a configuration (paths containing spaces are supported).
It defaults to the repository's `config.toml`. Default report and backend log names
include the start time to distinguish runs on the same day.

The observer can also be driven directly:

```bash
uv run python tools/soak.py \
  --duration-seconds 86400 \
  --sample-seconds 60 \
  --pid <backend-pid> \
  --output artifacts/benchmarks/soak/soak_YYYY-MM-DD.md
```

Pass the pid of the interpreter actually running `arb.main`. A virtualenv or `uv
run` launcher may re-exec the real interpreter as a child process, and sampling
the launcher reports a flat few-megabyte RSS rather than the backend's memory.
The launcher script resolves this automatically.

The observer samples per-exchange ingest volume, adapter reconnects and gaps,
canonical book eligibility and age, readiness, opportunities, background-task
failures, HTTP failures, recovery durations, and backend RSS. It rewrites the
report after every sample, so an interrupted run still leaves a readable report
marked `in progress`. Shorter durations are useful as smoke tests but must not be
described as the required 24-hour soak.

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
The observer creates no WebSocket clients, so delivery-load evidence requires separately
connected clients and a description of that workload.
