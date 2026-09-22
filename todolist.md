# ArbSync backlog

Open tickets only. Estimates are engineering effort, not calendar duration, and include
implementation, tests, review fixes, and documentation. Completed tickets (ARB-001 through
ARB-041, ARB-043, and ARB-044) are archived verbatim with their acceptance criteria in
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
- ARB-043 filtered history API, bounded JSONL export, and dashboard drill-down: complete.
- ARB-044 venue-comparison route coherence: complete; its evidence criterion moved to
  ARB-045.
- Live net-executable episodes are not added: ARB-041 measured zero net-positive intervals
  on a 45-minute capture and recorded the decision to retain offline intervals.

## Open tickets

### [ ] ARB-042 — Make fill-rate statistics complete, windowed, and reproducible

- Priority: P2
- Estimate: 12–20 hours
- Dependencies: ARB-037, ARB-038

Problem: live fill counts exist only for the current process, and sampling iterates books
that have already been seen. A configured book that never initializes can be absent rather
than counted as unavailable, and the API has no time window or sample provenance.

Acceptance criteria:

- Sample the configured roster, including missing and never-initialized books, and preserve
  ineligible observations separately from insufficient depth.
- Define sample start/end, cadence, restart, missed-sample, depth-cap, and configuration
  semantics. Expose raw counts alongside ratios.
- Produce reproducible windowed aggregates by venue, pair, side, and notional from a bounded
  persisted or file-backed representation chosen explicitly for this use case.
- Verify restart behavior, quiet and missing books, configuration changes, capped versus
  full-depth venues, and replay/live agreement on the same observation sequence.
- Do not make this ticket depend on live net episodes.

### [ ] ARB-045 — Refresh research and validation claims and run a current-code soak

- Priority: P1
- Estimate: 4–8 hours plus soak runtime
- Dependencies: ARB-036 through ARB-040, ARB-044

Problem: the published four-hour soak predates the schema-v4 depth/fee ledgers, Binance.US
per-pair resynchronization (ARB-028), direct host-connectivity probes (ARB-029), and the
research-correctness contracts of ARB-036 through ARB-040. This criterion was split out of
ARB-044 so the completed dashboard fix is not held open by cross-cutting evidence work.

Acceptance criteria:

- After ARB-036 through ARB-040 settle their contracts, refresh the relevant architecture,
  research, changelog, and validation claims and run a qualifying current-code soak. Keep
  that evidence work out of feature implementation commits when it is independently scoped.

### Revised dependency order

```text
ARB-036 canonical observations
  -> ARB-037 capture integrity and provenance
  -> ARB-038 time-faithful replay and lifecycle
       -> ARB-039 estimator validation
       -> ARB-040 age/skew observability
            -> ARB-041 offline net-interval analysis

ARB-037 + ARB-038 -> ARB-042 windowed fill-rate evidence
schema v4         -> ARB-043 historical query/export (complete)
ARB-034           -> ARB-044 dashboard route correctness (complete)
ARB-036..ARB-040 + ARB-044 -> ARB-045 current-code evidence and soak
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
| Research correctness and next roadmap (ARB-036 through ARB-045) | 116–188 h | ARB-036 through ARB-041, ARB-043, and ARB-044 complete; ARB-042 and ARB-045 open |

Avoid expanding into execution modeling until the current detection-only claims, units,
and evidence are internally consistent; ARB-041 decided against live net episodes.
