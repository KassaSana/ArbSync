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
fee-adjusted/executable-size survival by notional, venue fill rates, and lead/lag,
plus `report.json` with the replay digest, row counts, and measurement metadata.
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

## Lead/lag method and limits

The tool builds midpoint ticks from versioned canonical post-apply replay
observations, bins them at 250 ms by default, converts them to asynchronous
log-return intervals, and calculates Hayashi–Yoshida overlap covariance across
a configurable lag grid.
The normalized input deltas remain available separately for protocol auditing;
they are not a price source. Lead/lag datasets produced by ARB-035 before this
observation correction are not valid evidence and must be regenerated.
Positive lead time means the reported leader's return series is estimated to move
before the follower's series. Internally, a positive lag shifts the follower's
returns earlier to align them with the leader. The selected correlation includes an approximate
95% Fisher-transform interval based on overlap count; overlapping returns are not
independent, so this interval is directional research evidence rather than a
formal product statistic.

The report records a measurement floor of at least the larger of the tick-bin and
lag-step sizes. Local receive timestamps include each venue's network path and
queueing, while exchange timestamps have venue-specific clock behavior. The
estimator therefore cannot identify causal leadership below that floor or remove
geographic latency and clock skew. Results are labelled research and must not be
shown as live product metrics or trade recommendations.
