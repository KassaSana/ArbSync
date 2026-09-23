# ArbSync backlog

Open tickets only. Estimates are engineering effort, not calendar duration, and include
implementation, tests, review fixes, and documentation. Completed tickets (ARB-001 through
ARB-046) are archived verbatim with their acceptance criteria in
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
- ARB-046 Gemini drift diagnosis, per-pair resubscription, and cause-labelled reconnects:
  complete; a naturally confirmed drift on the scoped path remains a validation gap.
- Live net-executable episodes are not added: ARB-041 measured zero net-positive intervals
  on a 45-minute capture and recorded the decision to retain offline intervals.

- Book trust (ARB-047 through ARB-050): open. ARB-046 showed Gemini's incremental stream
  diverges from Gemini's own snapshots on every pair; on 81 forced rebuilds (2026-09-22) the
  true best bid or ask had been missing from the incremental book for over 30 s on 14 of
  162 sides, while the book stayed eligible.

## Open tickets

### [ ] ARB-047 — Measure Gemini top-of-book error and its effect on stored research

- Priority: P1
- Estimate: 4–8 hours
- Dependencies: ARB-046

Problem: `tools/gemini_drift.py` counts divergent levels but not where they sit or how long
they last. A read-only pass over the 2026-09-22 forced-resync capture found stale missing
levels at rank 0 on 14 of 162 book sides, rank 1 on 14, and rank 2 on 12. Detection,
spreads, depth-walked pricing, and fill rates all read those levels while the book is
eligible, so the effect on stored and replayed research is unknown.

Acceptance criteria:

- Report divergence by rank (best price, top 5, within each configured notional's depth
  walk) and by how long each stale level persisted, per pair.
- Decide from Gemini trade prints, not REST, whether the incremental book or the snapshot
  is correct when they disagree, and record the evidence. Probe the live trade stream first.
- Measure the share of Gemini-leg episodes (stored and replayed from the 2026-09-20
  capture) that rested on a level the next Gemini snapshot contradicts; record it in
  `docs/VALIDATION.md` and caveat Gemini-leg research claims if it is material.

### [ ] ARB-048 — Bound Gemini divergence age continuously

- Priority: P1
- Estimate: 6–10 hours
- Dependencies: ARB-047

Problem: divergence is repaired only after the reconciler confirms it: about one REST
comparison per book per minute, top 10 levels, three to five consecutive confirmations.
Meanwhile a wrong Gemini best price is treated as eligible.

Acceptance criteria:

- Probe whether Gemini's fast API delivers `{symbol}@depth20` (or `@depth10`) partial
  snapshots on the same socket, and record the result.
- If it does, compare each partial snapshot with the incremental book's top N inside the
  adapter and, on a confirmed stale mismatch, invalidate the pair and resubscribe it through
  the ARB-046 path. Otherwise rotate proactive per-pair resubscriptions so every book is
  rebuilt within a documented bound, and record the measured ineligibility cost.
- Recovery stays adapter-owned and fail-closed, reuses the pair-resync limit and
  cause-labelled metrics, and replays deterministically from captures.
- A capture shows the bound holding: no stale best-price divergence older than the
  documented limit.

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
  the same class of defect as Gemini.

Also fold into the next scheduled soak, without a dedicated ticket: show a naturally
confirmed Gemini drift recovering through the scoped path (the ARB-046 validation gap), or,
once ARB-048 lands, verification-triggered resyncs with no confirmed drift.

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
                       -> ARB-047 Gemini top-of-book error and research impact
                            -> ARB-048 continuous Gemini divergence bound
                       -> ARB-049 set-based reconciliation comparison
                       -> ARB-050 Coinbase and Binance.US snapshot audit
```

ARB-047 is measurement only and decides how aggressive ARB-048 must be. ARB-049 and ARB-050
are independent of both and of each other.

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
| Book trust (ARB-047 through ARB-050) | 17–29 h | Open |

Avoid expanding into execution modeling until the current detection-only claims, units,
and evidence are internally consistent; ARB-041 decided against live net episodes.
