# Pipeline optimization measurement — 2026-09-13

Four changes landed on `main` after ARB-022: removing dead indirection (`eb455e4`),
reusing adapter HTTP and SQLite writer connections (`b6735c5`), comparing book status
before building its payload (`7d51b10`), and evaluating book eligibility once per event
(`81c9c22`). This record measures them. The headline result is a reduction in backend CPU
at the 10x headroom rate with no change at the representative rate. It also records one
unexplained shutdown observation that these changes may have introduced.

## What was compared

Both sides use `tools/profile_pipeline.py` with identical arguments, the same host, the
same session, and the workload ARB-022 documents: 27 books, 500 levels per side, the
5-second burst cycle, warmup and bounded drain, 60-second runs, three repeats.
Environment: Windows 11 build 26200, Python 3.12.10, Chrome 152.0.7977.83, 16 logical
CPUs, 15.65 GiB RAM.

"Before" is `7f7fd0c`, the commit immediately preceding the four changes, checked out and
measured on the same day rather than compared against ARB-022's stored figures. Every
report carries matching before/after source fingerprints: `f5a30b0d7ecc` before,
`eec3b6968097` after.

ARB-022's stored profile numbers are **not** a valid baseline for this comparison, which
the section on contention below explains. Measuring both sides on the same host in the
same session is what makes the delta attributable.

## Measured effect, unprofiled

These are the capacity numbers. Ranges are the three repeats at each rate.

| Rate | Metric | Before | After |
| ---: | --- | ---: | ---: |
| 110/s | Mean process CPU, one core | 3.61–4.49% | 3.23–3.75% |
| 110/s | Receive→detect p99 | 0.69–0.97 ms | 0.58–0.61 ms |
| 110/s | Send→detect p99 | 31.0–63.9 ms | 33.2–36.6 ms |
| 110/s | Mean RSS | 74.9–75.7 MiB | 72.4–73.0 MiB |
| 1,100/s | Mean process CPU, one core | 27.13–29.35% | 9.75–14.87% |
| 1,100/s | Receive→detect p99 | 0.56–0.67 ms | 0.17–0.33 ms |
| 1,100/s | Send→detect p99 | 298.5–644.7 ms | 46.5–176.6 ms |
| 1,100/s | Mean browser CPU, one core | 67.0–72.4% | 29.2–45.1% |
| 1,100/s | Mean RSS | 84.3–84.9 MiB | 82.7–83.7 MiB |

All twelve runs processed every sent event with zero gaps, reconnects, persistence drops,
dashboard queue overflows, or browser page errors.

At the representative 110/s rate the change is not material: CPU and latency differences
are within the run-to-run spread of either side. The benefit appears only at the 10x
headroom rate, which is the rate where ARB-022 observed sender backlog.

Sources: [before](preopt-perf-20260913T224905Z.json), [after](postopt-perf-20260913T225644Z.json).

## Contention is the dominant variable, and it confounds profiles

Across every profiled artifact in this directory, backend self time outside the named
stages tracks Chromium's CPU, not the backend code:

| Run set | Browser CPU, one core | `other` self time |
| --- | ---: | ---: |
| After, three repeats | 37.5%, 38.1%, 37.8% | 10.53 s, 10.48 s, 10.66 s |
| Before, three repeats | 40.9%, 75.3%, 97.1% | 12.56 s, 21.48 s, 28.44 s |
| ARB-022 worker profile | 109.5%, 82.1%, 116.8% | 31.38 s, 23.04 s, 33.72 s |

The before-profile degraded across its own repeats — browser CPU 40.9% to 97.1%, send→detect
p99 109.7 ms to 1,435 ms — while the after-profile stayed flat. The unprofiled before-runs at
the same rate did not degrade (27.13%, 29.35%, 27.13%), so this escalation is an artifact of
profiling overhead on a contended host, not the capacity behavior. ARB-022's profile sits in
the same contended regime; its higher figures were previously assumed to reflect its
thread-CPU instrumentation, but backend instrumentation cannot raise a separate browser
process's CPU, so contention is the better explanation.

Consequence: only the first repeat of each profiled session is comparable, and profiled
self times cannot be treated as a clean attribution of where backend work was removed.

## Stage attribution, first profiled repeat only

Exclusive elapsed self time, 60 s at 1,100/s. Cumulative times overlap and are not summed.
This partitions elapsed time, not exact CPU.

| Stage | Before | After | Change |
| --- | ---: | ---: | ---: |
| JSON decoding/encoding | 0.6331 s | 0.6102 s | −3.6% |
| Adapter Decimal conversion | 0.1303 s | 0.1264 s | −3.1% |
| Sorted-level insertion/update/removal | 0.2747 s | 0.2618 s | −4.7% |
| Detector | 0.1695 s | 0.1667 s | −1.6% |
| Persistence on the main thread | 0.0464 s | 0.0520 s | +12.1% |
| Broadcaster and WebSocket delivery | 0.6724 s | 0.4879 s | −27.4% |
| Event-loop wait | 46.13 s | 48.33 s | +4.8% |
| Other functions, scheduling and instrumentation | 12.56 s | 10.66 s | −15.2% |

None of the four changes touch JSON parsing, Decimal conversion, sorted-level mutation, or
the detector. Those four stages moved by at most 4.7%, which sets the noise floor for this
pair of runs. Broadcaster and delivery fell well outside it, which is consistent with
`7d51b10` keeping payload construction off the path that discards the message, and the
remainder fell by 15% while event-loop wait rose — the backend is idle for more of the run.

The residual is still the largest non-wait term and is still not attributable to any single
named stage. ARB-022's gates for a JSON, book-structure, or event-loop change remain
uncrossed, and this work does not change that conclusion.

Sources: [before](preopt-profile-profile-20260913T231731Z.json),
[after](postopt-profile-profile-20260913T231221Z.json).

## Open: intermittent failure to exit

Two runs on the changed code did not exit within the harness's 60-second graceful window
and were killed; no run on the baseline code did. The counts are 2 of 13 after versus 0 of
9 before. Forced-shutdown recording (`7f7fd0c`) is present on both sides, so this is a
real difference in the observations and not a difference in instrumentation.

In both cases the backend had already written its measurement output, so the measurements
are intact. That output is written by the benchmark stop route before shutdown begins, so
it establishes only that measurement finished, not that the shutdown sequence completed.
The hang could be anywhere from the first shutdown phase to the interpreter's join of
non-daemon threads.

`b6735c5` names the relevant hazard in its own commit message: `aiosqlite` worker threads
are not daemons, so a surviving writer connection blocks interpreter exit. That commit
guards the path where the store is never run. The 0.027 MiB database left by one hung run,
against 0.96 MiB for every clean run, is consistent with a connection that never closed and
never checkpointed its WAL.

The leading candidate was an interrupted `_close_db`, which clears `self._db` before
awaiting `close()` and so would drop the only reference to a live connection.
`test_cancelled_worker_releases_its_writer_thread` rules that out: cancelling the worker
mid-run still executes the `finally`, closes the connection, and leaves no worker thread
behind. A companion test pins the graceful path. Neither the benchmark backend nor
`run_pipeline` cancels the persistence worker in the first place — `BackgroundTaskSupervisor.stop`
only sets a flag — so no known path currently strands the writer.

Reproduction attempts now total 144 shutdowns on the changed code with no occurrence: four
harness runs at 60 seconds, twenty at 15 seconds, 100 short cycles of the lifecycle probe
described below, and 20 probe cycles at matched volume. No run left a lingering non-daemon
thread, no run failed to exit, and the slowest shutdown across all of them was 1.17 seconds.
Against the 2-of-13 rate originally observed, this many consecutive clean shutdowns would be
a very unlikely outcome if the per-shutdown probability were still that high.

Volume was the caveat that count could not answer, because both original hangs were
60-second harness runs and every earlier attempt carried less. The 20 matched cycles each
sent and processed 66,000 events, the same as the runs that hung, and each exited in at most
0.63 seconds. The remaining deviation is that those cycles ran four backends at a time
rather than one, so host conditions differed even though the per-backend workload did not.

Rather than keep sampling a rare event, the harness now captures what a single future
occurrence would need. `tools/perf_backend.py` times each shutdown phase, so the phase with
no recorded duration is the one that hung; a `faulthandler` watchdog dumps every thread's
stack from inside the hang; and non-daemon threads surviving the event loop are recorded
before the interpreter's join can block on them. `tools/profile_pipeline.py` carries all of
it into each result as `shutdown_diagnostics`.

The surviving-thread check needed calibration, which the probe found immediately: a worker
whose connection has just been closed can still be finishing, and `threading.enumerate`
reports it. That first appeared as a stranded writer on a cycle that had exited cleanly in
0.345 seconds. Survivors are now joined briefly and only reported if they outlast that, so
the field means a thread that is actually stuck rather than one on its way out.

Sampling shutdowns is also no longer expensive. `tools/shutdown_probe.py` runs the same
backend start and stop path without a browser, a warmup or a dashboard build, at about 1.1
seconds per shutdown against roughly 25 in the measurement harness. It reports startup and
shutdown behavior only and is explicitly not a capacity benchmark.

Sources: [four 60-second runs](shutdownrepro-perf-20260913T230715Z.json),
[twenty 15-second runs](shutdown20-perf-20260913T235048Z.json),
[100 lifecycle cycles](shutdown100-lifecycle-20260914T010205Z.json),
[20 volume-matched cycles](shutdownvolume-lifecycle-20260914T012811Z.json).

## Limits

Short modeled runs on a developer workstation that was also running Chromium and the load
generator, not captured internet traffic, a dedicated idle host, or a 24-hour soak. The
two pairs of runs were taken in opposite orders — unprofiled before-then-after, profiled
after-then-before — and the changed code measured faster in both, so the result is not an
artifact of measurement order. Three repeats per rate is a small sample against the
observed run-to-run spread, and the 1,100/s after-runs themselves span 9.75% to 14.87%.
