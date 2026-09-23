# ArbSync backlog

Open tickets only. Estimates are engineering effort, not calendar duration, and include
implementation, tests, review fixes, and documentation. Completed tickets (ARB-001 through
ARB-048) are archived verbatim with their acceptance criteria in
[`docs/COMPLETED_TICKETS.md`](docs/COMPLETED_TICKETS.md); user-facing outcomes are in
[`CHANGELOG.md`](CHANGELOG.md).

## Status

- P0 release gate, P1 reliability, and P2 operational evidence: complete; `v0.1.0` is
  published.
- Executability roadmap ARB-030 through ARB-035 (capture/replay, episodes, depth-walked and
  fee-aware pricing, venue comparison, offline research): complete on the default branch
  under `Unreleased`; package metadata is still `0.1.0`.
- Research correctness ARB-036 through ARB-041 (canonical observations, capture provenance,
  time-faithful replay, estimator validation, age/skew measurement, offline net-executable
  intervals): complete.
- ARB-042 complete, windowed fill-rate statistics (schema version 5 minute buckets): complete.
- ARB-043 filtered history API, bounded JSONL export, and dashboard drill-down: complete.
- ARB-044 venue-comparison route coherence: complete; its evidence criterion moved to
  ARB-045.
- ARB-045 current-code four-hour soak (2026-09-22) and claim refresh: complete.
- ARB-046 Gemini per-pair resubscription and cause-labelled reconnects: complete; its drift
  diagnosis was corrected by ARB-047.
- Live net-executable episodes are not added: ARB-041 measured zero net-positive intervals
  on a 45-minute capture and recorded the decision to retain offline intervals.

- ARB-047 Gemini book audit: complete. Gemini's stream-built books matched Gemini's top-20
  snapshots at all 21,797 aligned update ids; the ARB-046 "drift" is stale levels in
  Gemini's REST book, which never traded.
- ARB-048 continuous Gemini verification against `@depth20` at the same update id, with
  Gemini removed from REST reconciliation: complete. ARB-049 through ARB-051 are open.

## Open tickets

### [ ] ARB-049 — Compare reconciliation levels by price set, not by index

- Priority: P2
- Estimate: 3–5 hours
- Dependencies: ARB-046

Problem: `SnapshotReconciler._side_difference` compares the top 10 levels position by
position. On a sparse book one missing level shifts every later index and reads as a large
price mismatch (the DOT-USD and LTC-USD confirmations); on a dense book the same defect,
even a wrong best price, can appear only as a size-only mismatch that needs a longer streak.

Acceptance criteria:

- Compare the best price explicitly and the set of price levels within the range both
  books cover, keeping the live-read bracketing, confirmation counts, and cooldown.
- Replay the 2026-09-20 capture's reconciliation snapshots before and after, and record how
  mismatch and confirmation counts change per venue and pair.

### [ ] ARB-050 — Audit Coinbase and Binance.US books against their own snapshots

- Priority: P2
- Estimate: 4–6 hours
- Dependencies: ARB-046

Problem: the ARB-045 soak's Coinbase LTC-USD size mismatches (71) and Binance.US drifts
are explained only as non-atomic comparison noise. The ARB-046 method has not been applied
to the other venues.

Acceptance criteria:

- Generalize the drift tool: Coinbase compares against a fresh Level 2 snapshot after
  resubscribing one product; Binance.US compares the book at exactly the REST snapshot's
  `lastUpdateId`.
- Record per-venue divergence from existing captures and state whether either venue shows
  the same class of defect as Gemini, and whether its REST snapshots retain levels the
  stream deleted (the ARB-047 Gemini finding), using trades where the venue provides them.

### [ ] ARB-051 — Replay a connection that ended on a failed REST snapshot fetch

- Priority: P3
- Estimate: 2–4 hours
- Dependencies: ARB-037, ARB-038

Problem: a REST snapshot fetch that raises leaves no capture frame. On the 2026-09-22
ARB-047 capture, Binance.US's first connection ended when an initial-sync fetch failed and
reconnected; replay then resolved that pair's fetch to the next connection's recorded
snapshot and stopped with a provenance mismatch, so the capture replays only with that
venue's first 40 s removed.

Acceptance criteria:

- Record failed snapshot fetches with their request provenance, and have replay reproduce
  the failure and the recorded reconnect instead of stopping.
- The ARB-047 capture replays in full.

Also fold into the next scheduled soak, without a dedicated ticket: once ARB-048 lands, show
Gemini verification running for hours with its outcomes counted.

### Revised dependency order

```text
ARB-036 canonical observations
  -> ARB-037 capture integrity and provenance
  -> ARB-038 time-faithful replay and lifecycle
       -> ARB-039 estimator validation
       -> ARB-040 age/skew observability
            -> ARB-041 offline net-interval analysis

ARB-037 + ARB-038 -> ARB-042 windowed fill-rate evidence (complete)
schema v4         -> ARB-043 historical query/export (complete)
ARB-034           -> ARB-044 dashboard route correctness (complete)
ARB-036..ARB-040 + ARB-044 -> ARB-045 current-code evidence and soak (complete)
ARB-028 + ARB-045 -> ARB-046 Gemini drift and scoped recovery (complete)
                       -> ARB-047 Gemini book audit (complete)
                            -> ARB-048 continuous Gemini verification (complete)
                       -> ARB-049 set-based reconciliation comparison
                       -> ARB-050 Coinbase and Binance.US snapshot audit
ARB-037 + ARB-038 -> ARB-051 replay of failed snapshot fetches
```

ARB-049, ARB-050, and ARB-051 are independent of each other. ARB-049 now affects only
Coinbase and Binance.US, since Gemini is no longer REST-reconciled.

ARB-039 does not block ARB-040 or ARB-041: lead/lag estimator validity is separate from
book-age diagnostics and executable-route arithmetic. ARB-041 decided against a live
net-episode ticket on current evidence; it was never a prerequisite for existing-history
queries or fill-rate correctness. Streaming multi-hour captures with bounded memory remains a focused
follow-up when the chosen research dataset demonstrates that materialization is the actual
limit; it should not be bundled with unrelated API performance work.

## Planning summary

| Milestone | Engineer effort | Status |
| --- | ---: | --- |
| P0, P1, P2 and executability roadmap (ARB-001 through ARB-035) | 171 h plus soak runtime | Complete; historical estimate, not elapsed time |
| Research correctness and next roadmap (ARB-036 through ARB-045) | 116–188 h | Complete |
| Gemini drift and scoped recovery (ARB-046) | 6–12 h | Complete |
| Gemini book audit (ARB-047) | 4–8 h | Complete |
| Gemini verification (ARB-048) | 4–8 h | Complete |
| Book trust (ARB-049 through ARB-051) | 9–15 h | Open |

Avoid expanding into execution modeling until the current detection-only claims, units,
and evidence are internally consistent; ARB-041 decided against live net episodes.
