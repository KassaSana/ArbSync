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

In both cases the backend had already written its measurement output, so the pipeline
completed and the measurements are intact; only interpreter exit hung. A dedicated
four-run reproduction on the changed code at 1,100/s did not reproduce it.

`b6735c5` names the relevant hazard in its own commit message: `aiosqlite` worker threads
are not daemons, so a surviving writer connection blocks interpreter exit. That commit
guards the path where the store is never run. The leading candidate for the remaining
cases is a path where `_close_db` does not complete — it clears `self._db` before awaiting
`close()`, so an interruption at that await drops the only reference to the connection
while its thread is still alive. The 0.027 MiB database left by one such run, against
0.96 MiB for every clean run, is consistent with a connection that never closed and never
checkpointed its WAL.

This is a candidate mechanism, not a confirmed cause. Confirming it needs a reproduction
with more runs than were done here; a blind fix would risk masking a different cause.

Source: [non-reproduction](shutdownrepro-perf-20260913T230715Z.json).

## Limits

Short modeled runs on a developer workstation that was also running Chromium and the load
generator, not captured internet traffic, a dedicated idle host, or a 24-hour soak. The
two pairs of runs were taken in opposite orders — unprofiled before-then-after, profiled
after-then-before — and the changed code measured faster in both, so the result is not an
artifact of measurement order. Three repeats per rate is a small sample against the
observed run-to-run spread, and the 1,100/s after-runs themselves span 9.75% to 14.87%.
