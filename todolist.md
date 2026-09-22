# ArbSync backlog

Open tickets only. Estimates are engineering effort, not calendar duration, and include
implementation, tests, review fixes, and documentation. Completed tickets (ARB-001 through
ARB-045) are archived verbatim with their acceptance criteria in
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
- Live net-executable episodes are not added: ARB-041 measured zero net-positive intervals
  on a 45-minute capture and recorded the decision to retain offline intervals.

## Open tickets

### [ ] ARB-046 — Investigate recurring Gemini price drift and scope its recovery

- Priority: P2
- Estimate: 6–12 hours
- Dependencies: ARB-028, ARB-045

Problem: the 2026-09-22 soak confirmed six Gemini price drifts in four hours (`DOT-USD`
four times, `LTC-USD` twice), each within the first 90 minutes. Gemini has no scoped
resync, so every confirmation reconnected the whole venue and made its other eight books
briefly ineligible (2.0–2.7 s each); the reconnect metric labels these only
`reason="RuntimeError"`.

Acceptance criteria:

- Determine from captured traffic whether the drifts are genuine stream divergence, REST
  snapshot staleness, or a normalization defect, and record the evidence.
- If Gemini's protocol allows it, recover a single pair without reconnecting the venue, or
  document why it cannot; keep recovery adapter-owned and fail-closed.
- Label adapter reconnects by their cause (for example confirmed drift versus transport
  error) instead of the exception class.

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
ARB-028 + ARB-045 -> ARB-046 Gemini drift and scoped recovery
```

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

Avoid expanding into execution modeling until the current detection-only claims, units,
and evidence are internally consistent; ARB-041 decided against live net episodes.
