# Depth-ledger and capture-writer investigation — 2026-09-18

## Decision

Two measured changes are justified:

1. Price only the requested buy/sell route when an episode opens or reaches a new peak.
   The previous `ledgers_for_route` priced all six directed routes in a three-venue market
   and discarded five of them.
2. Drain capture output in batches of at most 256 lines and perform file open, write,
   gzip compression, and close calls through `asyncio.to_thread`. The bounded asyncio
   queue and drop policy remain unchanged.

No parser, book structure, process, or general event-loop redesign is justified by this
investigation.

## Environment and method

- Windows 11 `10.0.26200`, Python 3.12.10.
- Baseline commit: `88776da1b5b325b77ab4573b4326560bc196b6ef`.
- Pricing measurements time one `DepthSampler.ledgers_for_route` call for four configured
  notionals and three eligible venues. Books contain 20, 500, or 5,000 levels per side.
  The candidate was checked for exact ledger equality before timing.
- Capture measurements prefill the real bounded `CaptureWriter` and measure both total
  drain time and the largest gap seen by a 1 ms event-loop heartbeat. Payloads are
  deterministic JSON-like exchange frames. Each post-change result was repeated twice.
- The committed 150-second capture contains 15,384 WebSocket frames: raw-size p50 589 B,
  p95 2,780 B, p99 5,532 B, maximum 4,622,480 B. The single-frame stress case below is
  approximately 4.0 MB; the backlog case is 1,000 frames of approximately 17 KB each.
- Windows commonly schedules a 1 ms sleep at about 16 ms. Continuous modeled capture at
  110 and 1,100 frames/s stayed at that timer floor before and after, so it does not show
  a steady-state latency improvement. The backlog and large-frame cases isolate the
  synchronous writer stall instead.

## Results

### Requested route ledger

| Levels per side | Before median | After median range | Reduction |
| ---: | ---: | ---: | ---: |
| 20 | 1,464 µs | 230–234 µs | 84% |
| 500 | 24,172 µs | 4,077–4,245 µs | 82–83% |
| 5,000 | 24,405 µs | 4,184–4,369 µs | 82–83% |

The full-book depth is copied before walking, so 500 and 5,000 levels are similar once the
largest configured notional consumes about 500 levels. The important result is the stable
roughly sixfold amplification from computing every route. The committed capture replayed
15,455 frames with no opportunities and therefore no ledger calls: this is episode-open or
new-peak burst latency, not per-message steady-state cost. Historical live evidence recorded
108 distinct dislocations in four hours, so average CPU impact at that workload is small;
removing a 20–25 ms event-loop stall when a deep-book episode changes is still useful.

### Capture writer

| Workload | Before max heartbeat gap | After max gap range | Total drain behavior |
| --- | ---: | ---: | --- |
| one ~4.0 MB frame, plain | 18.7 ms | 6.5–8.2 ms | 12–13 ms after vs 18.6 ms before |
| one ~4.0 MB frame, gzip | 24.6 ms | 10.6–12.7 ms | 24.7–26.4 ms after vs 24.4 ms before |
| 1,000 × ~17 KB, plain | 37.1 ms | 6.8–9.1 ms | 27.7–39.6 ms after vs 37.0 ms before |
| 1,000 × ~17 KB, gzip | 87.5 ms | 15.9–16.1 ms | 85.0–88.2 ms after vs 87.5 ms before |

Offloading does not make gzip cheaper, and that is not the goal. It preserves comparable
throughput while preventing a queued drain or large frame from monopolizing the ingestion
event loop for the full write/compression duration. Batches cap thread handoff overhead;
the queue remains bounded at the configured frame count, and overload still drops and
counts new capture frames rather than blocking ingestion.

## Correctness and limits

- The route-specific result is value-equivalent to filtering the full route set; a
  regression test also proves an unrelated venue is not read.
- Plain and gzip round trips, queue overflow, close behavior, capture/replay, detector, and
  pricing tests pass. A regression test verifies capture open/write/close calls use the
  worker-thread boundary.
- The capture stress data is local filesystem evidence, not a slow-disk or network-share
  benchmark. `json.dumps` and event summarization still occur synchronously in
  `record_ws`; this investigation did not show they are a bottleneck at the configured live
  rate, so moving mutable frame construction across threads would be speculative.
- During this investigation, `tools/profile_pipeline.py` failed after collecting a run
  because it queried the removed `opportunities` table. A follow-up changed the summary
  count to schema-v4 `opportunity_episodes`, added a direct regression test, and completed
  a valid connected-dashboard run at 110 events/s with 550/550 events processed.
