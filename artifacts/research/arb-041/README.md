# ARB-041 net-executable interval evidence and decision

Offline analysis of the same lossless 45-minute, three-venue capture used for ARB-040
(recorded 2026-09-20, 20:54:59–21:40:11 UTC, 27 configured pairs, replay digest in
`report.json`; the capture stays under `var/`), plus the committed 150-second fixture.
`tools/research.py` ran with `--null-surrogates 0` and the default net settings
(threshold `0`, hysteresis `0`, delay grid `50 250 1000` ms, configured taker fees
0.60 / 0.40 / 0.60 % for Coinbase / Gemini / Binance.US, notionals 100 / 1,000 /
10,000 / 50,000 quote). Semantics are in
[`docs/RESEARCH.md`](../../../docs/RESEARCH.md#net-executable-intervals).

Host: Windows 11, Python 3.12, one workstation core; `time.monotonic_ns` advances in
~15.6 ms steps, so every duration below is quantized to that tick.

## What the capture showed

- 306,893 canonical book changes produced 1,666,586 change-only signal rows (four
  notionals across 54 directed routes).
- **Zero net-positive intervals** at every notional, in the baseline and in every
  sensitivity variant: detector threshold (0.1 %), hysteresis 0.01 / 0.05, delay 50 /
  250 / 1,000 ms, and halved fees. `net_intervals.jsonl` and
  `net_interval_sensitivity.jsonl` are therefore empty by construction, not by error;
  the lifecycle is exercised by the unit tests and by the hypothetical fee tables below.
- The best net executable spread seen anywhere in 45 minutes was **−0.68 %** (DOT-USD,
  buy Coinbase → sell Gemini, 100 quote, gross +0.32 %). Per notional the best net was
  −0.68 / −0.77 / −0.78 / −0.86 % at 100 / 1,000 / 10,000 / 50,000; no priced row was
  ever above −0.5 %. Round-trip taker fees of 1.0 % dominate a market whose best gross
  executable spread was 0.32 %.
- Gross-positive rows are common (29,138 of 240,143 priced rows at 100 quote; 1,380 of
  559,887 at 50,000), so the net result is a fee result, not a depth result.
  Insufficient depth appeared only at 10,000 (21 rows) and 50,000 (729 rows); 278 rows
  per notional recorded an ineligible leg.
- The 150-second fixture behaves the same way: 71,444 signal rows, zero intervals, best
  gross +0.038 %, best net −0.96 %.

### Hypothetical fee tables (not a product tier)

To show what the lifecycle measures on this data, the recorded gross and fills were
re-netted under fee schedules that no configured venue offers. These numbers describe
the market's gross dislocations, not executable profit.

| Fee schedule | Intervals | Close reasons | Duration p50 / p90 (ms) | Peak p50 / max (%) | Survive 50 / 250 / 1,000 ms delay |
| --- | ---: | --- | ---: | ---: | ---: |
| Zero fees (gross > 0), 100 quote | 6,103 | 6,087 spread_closed, 16 invalidated | 547 / 6,000 | 0.008 / 0.317 | see total |
| Zero fees, 1,000 | 5,313 | 5,305 spread_closed, 8 invalidated | 485 / 4,407 | 0.005 / 0.232 | |
| Zero fees, 10,000 | 1,428 | 1,427 spread_closed, 1 insufficient_depth | 312 / 1,765 | 0.004 / 0.225 | |
| Zero fees, 50,000 | 218 | 218 spread_closed | 297 / 782 | 0.007 / 0.140 | |
| Zero fees, total | 13,062 | | | | 89.9 % / 64.5 % / 28.4 % |
| Quarter fees (0.15 / 0.10 / 0.15 %) | 7 (all 100 quote, one DOT-USD route) | 7 spread_closed | 657 / 3,078 | 0.022 / 0.067 | 85.7 % / 71.4 % / 28.6 % |
| Halved fees (sensitivity variant) | 0 | | | | |

Overlap with the 132 theoretical episodes (0.1 % top-of-book threshold): only 128 of
the 13,062 zero-fee intervals opened while a theoretical episode was open on the same
route, mean coverage 1.2 %, and only 113 intervals ever reached a 0.1 % gross peak (43
of them inside a theoretical episode). Conversely 120 of the 132 episodes had at least
one gross-positive interval on their route during their life. The two datasets describe
different things: theoretical episodes are rare, top-of-book, threshold-gated events;
gross-positive executable stretches are frequent, sub-basis-point, and short.

## Cost

`tools/perf_net_intervals.py` replays the capture twice in fresh processes, without
and with the observer, times each observer call, and times the periodic
`DepthSampler.sample_all` walk on the replay's own 5-second cadence ticks. The
synthetic case prices four routes across two 300-level books and one 5-level book on
every event, with 50,000 quote exhausting the 64-level window and escalating to the
full book.

| Input | Book changes | Observer mean / p90 / p99 per change | Observer CPU per capture-second | Sampled baseline (5 s cadence) per capture-second | Resident growth (rows) | Wall without → with |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 45-minute capture | 306,893 | 6.6 / 12.3 / 19.8 ms (max 142 ms) | 0.75 s | 0.025 s (126 ms per tick) | +931 MB (1,666,586 rows, ~560 B/row) | 107 s → 2,265 s |
| 150-second fixture | 15,226 | 3.8 / 8.2 / 13.6 ms | 0.38 s | 0.013 s | +47 MB (71,444 rows, ~650 B/row) | 4.6 s → 63 s |
| Synthetic 300-level books | 200 ticks | 8.4 / 8.7 ms (mean / p90) | — | — | — | — |

Reading the table: exact per-event re-pricing of the affected routes costs 0.4–0.75
CPU-seconds per second of three-venue, 27-pair market data on this host, about 30× the
periodic sampler and within a factor of 1.3 of saturating one core on the 45-minute
capture. An in-process live implementation would have to run on the ingestion path or
a dedicated worker, with per-event p99 around 20 ms and tail stalls above 100 ms on
large books. Memory grows linearly at roughly 560–650 bytes per change-only row (about
0.9 GB per 45 minutes at these rates), so any live variant would need bounded state
rather than a retained signal. Peak working set is dominated by the decoded capture
itself (~2.3 GB in both runs) and the without-observer run's resident size fell during
replay as decode buffers were released, so the per-process growth figures, not their
difference, are the memory evidence. The cyclic garbage collector had to be paused for
the offline replay: with it enabled, every full collection rescanned the accumulated rows and the run went
superlinear (a first attempt exceeded 70 CPU-minutes without finishing).

## Decision

**Retain offline intervals; do not add live net episodes.**

- There is nothing to track live. Across 45 minutes and the 150-second fixture, no route
  at any configured notional was net positive under configured fees, halved fees, or a
  0.1 % threshold; the best net observation was −0.68 %. A live net-episode lifecycle
  would have opened zero episodes.
- The gap between theoretical episodes and executability is a fee gap, not a
  ledger-refresh gap. Gross-positive stretches are frequent but sub-basis-point and short
  (median ≈0.5 s, 28 % survive a 1-second delay), and they barely overlap the theoretical
  episodes (1.2 % coverage). Refreshing an episode's ledger more often would not have
  surfaced an executable opportunity.
- The measured cost is material: 0.4–0.75 CPU-seconds per market second and ~0.9 GB of
  retained signal per 45 minutes for exact per-event pricing, against 0.013–0.025
  CPU-seconds per second for the periodic sampler. That cost is not justified by an
  empty result.

Revisit when a capture shows net-positive rows under configured fees (for example after
a fee-tier change or on venues with lower taker fees), or when a stated product need
requires gross-executable rather than net-executable tracking. Either case should start
from this dataset's `net_signal.jsonl` export rather than a new live lifecycle, and any
live variant must bound its state. No SQLite migration and no change to live episode
semantics follow from this ticket.

## Files

- `report.json` — replay digest, dataset counts, and `measurement.net_intervals`
  (settings, fee schedule, notionals, timing fidelity, 1,666,586 signal rows).
- `net_intervals.jsonl`, `net_interval_sensitivity.jsonl` — empty on this capture (see
  above).
- `episodes.jsonl`, `survival_by_notional.jsonl`, `route_leg_ages.jsonl`,
  `age_skew_bands.jsonl`, `age_skew_gate_sensitivity.jsonl` — the theoretical-episode
  datasets from the same run, used for the overlap join.
- `perf_capture_45m.json`, `perf_fixture_150s.json` — cost measurements.
- The raw `net_signal.jsonl` export (834 MB), the lead/lag datasets, and
  `venue_fill_rates.jsonl` from the same run are not committed.
