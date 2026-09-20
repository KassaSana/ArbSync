# Offline market-structure research

`tools/research.py` runs deterministic, offline research over an ArbSync capture.
It replays raw frames through the production adapters, trusted order books,
episode detector, and depth sampler. It never writes SQLite or runs on the live
ingestion path.

```powershell
uv run python tools/research.py `
  --config config.toml `
  --capture var/capture.jsonl.gz `
  --output-dir var/research/run-2026-09-19
```

The output directory contains JSONL datasets for episode lifetimes,
fee-adjusted/executable-size survival by notional, venue fill rates, lead/lag,
lead/lag sensitivity, and route leg age and skew, plus `report.json` (`version` 3)
with the replay digest, row counts, and measurement metadata.
The datasets are files rather than SQLite tables so research runs cannot affect
product persistence or ingestion.

## Dataset semantics

- `episodes.jsonl` has one canonical row per `(start, pair, buy venue, sell venue)`
  episode. A spread still open at the end of a capture is closed at the last
  recorded timestamp with reason `shutdown`, matching deterministic replay.
- `survival_by_notional.jsonl` reads the exact fee-aware pricing ledgers captured
  at episode open or peak. `fee_survival_rate` is survivors divided by priced
  observations; `executable_size_survival_rate` is survivors divided by all
  observations, including insufficient-depth outcomes.
- `venue_fill_rates.jsonl` contains the depth sampler's per-venue, per-pair,
  per-notional, per-side observations. Ineligible samples remain separate from
  insufficient depth.
- `route_leg_ages.jsonl`, `age_skew_bands.jsonl`, and
  `age_skew_gate_sensitivity.jsonl` are described under
  [Route leg age and receipt skew](#route-leg-age-and-receipt-skew).

## Route leg age and receipt skew

Canonical eligibility bounds each book's age independently, so a route can compare a
book updated a moment ago with one close to the configured limit. The detector reports
each compared route's leg ages on the local monotonic clock, and research keeps two
separate dimensions from them:

- **absolute age**: the older leg's receipt age (`max(buy_age, sell_age)`), which asks
  whether either input was old;
- **relative skew**: `|buy_age - sell_age|`, which asks how asynchronous the two inputs
  were. A large skew shows the books were not observed together; it does not show the
  quieter book was wrong, and equal ages say nothing about freshness.

Ages come from local receipt times only. Exchange timestamps carry venue-specific clock
behaviour and are never a freshness authority. Connection state and sequence continuity
are reported through `close_reason` (`book_ineligible`) and the replay lifecycle trace
rather than folded into either dimension. Resolution is bounded by the recording host's
monotonic clock: on Windows with Python 3.12 `time.monotonic_ns` advances in roughly
15.6 ms steps, so ages and skews from such a capture are multiples of that tick and the
50 ms skew band is the smallest one that can be read there. A future gate proposal must
state the tick of the capture it rests on.

- `route_leg_ages.jsonl` has one row per canonical episode with both legs' ages and skew
  at open, at the last peak, and at close, the widest skew observed while the episode was
  open (`max_age_skew_ms`), lifetime, peak spread and profit, close reason, and whether
  any stored ledger was net positive (`fee_survivor`). A close caused by a lost leg keeps
  the last ages seen with both legs present.
- `age_skew_bands.jsonl` has one row per dimension and band. Bands are configured as
  upper edges in milliseconds (`--age-bands-ms`, default `100,500,1000,5000,15000`;
  `--skew-bands-ms`, default `50,250,1000,5000`); a value equal to an edge belongs to the
  lower band and one open-ended band follows the last edge. Each row counts every route
  comparison the detector made in that band (`evaluations`), the episodes that opened
  there (`episodes_opened`, `open_rate`), those later closed by a lost leg, fee survivors,
  peak-spread and lifetime percentiles (floating-point summaries), and the same
  survival-by-notional arithmetic as `survival_by_notional.jsonl` restricted to the band.
- `age_skew_gate_sensitivity.jsonl` reports, for a cutoff at each band edge, how many
  episodes would have been retained and rejected at open, how many fee survivors and
  `book_ineligible` closures fall on each side, and the retained share of theoretical
  peak profit per quote asset. USD and USDT totals are never added together.

No gate is applied. The sensitivity table exists so a cutoff can be argued from a
faithful capture; the decision is recorded in this section once the evidence is in.

## Lead/lag method and limits

The tool builds midpoint ticks from versioned canonical post-apply replay
observations, bins them at 250 ms by default, converts them to asynchronous
log-return intervals, and estimates lead/lag with the Hayashi–Yoshida contrast
in `tools/lead_lag.py`. The normalized input deltas remain available separately
for protocol auditing; they are not a price source. Lead/lag datasets produced
before the ARB-036 observation correction or before the ARB-039 estimator
validation (research report `version` 1) are not valid evidence and must be
regenerated; version 2 rows are not comparable with version 1 rows.

### Estimator

With left returns `r_i` on intervals `I_i`, right returns `s_j` on intervals
`J_j`, and lag `θ`:

- Contrast `U(θ) = Σ r_i · s_j · 1{I_i ∩ (J_j − θ) ≠ ∅}` (Hayashi & Yoshida,
  *Bernoulli* 11(2), 2005). Intervals that only touch do not overlap.
- Lag estimate `θ* = argmax |U(θ)|` over a symmetric grid `±K·step`,
  `K = ⌊max_lag / step⌋` (Hoffmann, Rosenbaum & Yoshida, *Bernoulli* 19(2),
  2013). Ties resolve to the smallest `|θ|`.
- Reported correlation `U(θ*) / √(Σ r_i² · Σ s_j²)` (Huth & Abergel, *Journal
  of Empirical Finance* 26, 2014).

Positive lead time means the reported leader's return series is estimated to move
before the follower's. Internally, a positive lag shifts the follower's returns
earlier to align them with the leader.

The normalized contrast is consistent for the true correlation but not bounded
by one in finite samples: every return is multiplied by each partner return it
overlaps while the denominator counts it once, so a near-perfectly correlated
pair sampled asynchronously straddles one. The lag estimate does not depend on
the normalization, so an out-of-range value leaves the lag status alone;
`hayashi_yoshida_correlation` is `null`, the raw ratio remains in
`normalized_contrast`, and `correlation_out_of_range` is `true`. A row never
carries `hayashi_yoshida_correlation` outside `[-1, 1]`.

The contrast is flat over a plateau one sampling interval wide around the true
lag: a lag misaligned by less than one interval overlaps two returns on the
other side and is not distinguishable. The lag step therefore cannot resolve
below the typical inter-tick spacing, which the tick bin only bounds from below.

### Statuses

| `status` | `reason` | Meaning |
| --- | --- | --- |
| `ok` | — | A lag was selected, cleared the null screen, and sits inside the grid. |
| `insufficient_data` | `too_few_observations` | Fewer than two return intervals on a side. |
| `insufficient_data` | `constant_returns` | Zero or non-finite realized variance on a side. |
| `insufficient_data` | `no_overlap` | No interval pair overlaps at any grid lag. |
| `insufficient_data` | `too_few_overlaps` | No grid lag reaches `--min-overlap` overlapping pairs. |
| `not_identifiable` | `tied_maximum` | Lags of opposite sign share the maximum; the leader is ambiguous. |
| `not_identifiable` | `maximum_at_grid_edge` | The maximum sits at `±grid_max_lag_ns`; raise `--max-lag-ms`. |
| `not_identifiable` | `null_not_rejected` | The surrogate null was not rejected at 0.05. |

Zero overlap is never reported as a correlation of zero, and lags below
`--min-overlap` never enter selection. Non-`ok` rows keep whichever diagnostics
were computed before the ladder stopped.

### Uncertainty

There is no confidence interval. Overlapping asynchronous returns are dependent,
and the reported value is a maximum selected over a lag grid, so a Fisher-style
interval on the overlap count would be wrong on both counts. Two diagnostics
replace it:

- **Window stability** (`window_*`): the capture span is split into
  `--windows` equal windows and the estimate is repeated in each.
  `window_agreement_fraction` is the share of `ok` windows whose leader sign
  matches the full sample, or `null` with fewer than two `ok` windows.
- **Shifted-surrogate null** (`null_*`): the right series is circularly
  rotated by `±base·(1 + 5k)` for `k = 1..--null-surrogates`, `base` being the
  larger of the max lag and lag step, and the grid maximum `|ρ|` is recomputed.
  Rotation preserves each series' variance and overlap coverage while
  destroying any lead/lag inside the grid. `null_p_value` is the permutation
  form `(exceedances + 1) / (count + 1)`; at least 10 surrogates per sign are
  required so it can reach 0.05. Surrogates share the same data, so this is a
  screening diagnostic for grid-selection noise, not a calibrated test. On 30
  seeded independent random-walk pairs it retained 29 nulls.

`lead_lag_sensitivity.jsonl` repeats the estimate one knob at a time at half and
double the configured tick bin, lag step, window count, and minimum overlap,
with null surrogates disabled; the `baseline` variant matches `lead_lag.jsonl`
apart from the null screen. `--no-sensitivity` skips it.

The report records a measurement floor of at least the larger of the tick-bin and
lag-step sizes. Local receive timestamps include each venue's network path and
queueing, while exchange timestamps have venue-specific clock behavior. The
estimator therefore cannot identify causal leadership below that floor or remove
geographic latency and clock skew. Results are labelled research and must not be
shown as live product metrics or trade recommendations.
