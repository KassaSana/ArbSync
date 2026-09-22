# ArbSync backlog

Open tickets only. Estimates are engineering effort, not calendar duration, and include
implementation, tests, review fixes, and documentation. Completed tickets (ARB-001 through
ARB-041) are archived verbatim with their acceptance criteria in
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

### [ ] ARB-043 — Add filtered historical opportunity queries and export

- Priority: P2
- Estimate: 12–20 hours
- Dependencies: existing schema version 4; net-specific filters wait for an approved ARB-041 follow-up

Problem: the API exposes only the latest bounded list. Existing canonical episode history
cannot be paged or filtered for ordinary investigation without reading SQLite directly.

Acceptance criteria:

- Add stable cursor pagination with explicit ordering and filters for time range, pair,
  buy venue, sell venue, close reason, and open/closed state.
- Add a bounded JSONL or CSV export path that preserves decimal strings and quote units and
  cannot monopolize ingestion or hold an unbounded result in memory.
- Validate query limits, cursor stability across concurrent inserts, malformed filters, and
  index-supported plans on a representative database.
- Add dashboard drill-down only after the API contract is tested. Do not imply net-executable
  filtering until that lifecycle exists or an explicit stored-ledger filter is defined.

### [ ] ARB-044 — Keep venue-comparison route economics coherent with eligibility

- Priority: P1
- Estimate: 8–12 hours
- Dependencies: ARB-034

Problem: individual venue cells consult canonical book status, but the selected gross/net
route is chosen from the last polled pricing response without applying those statuses. The
panel can therefore show a route's economics after one leg has become ineligible. Its
independent cheapest-buy and best-sell labels can also describe different venues from the
route whose gross and net spread is displayed.

Acceptance criteria:

- Suppress a cached route immediately when either route leg becomes ineligible or the live
  feed is disconnected; distinguish unavailable from insufficient depth.
- Label the buy and sell venues for the exact displayed route. If independent per-side best
  quotes remain useful, present them separately without implying they produced that route.
- Carry an as-of marker or generation through pricing refreshes so an older response cannot
  revive a route after a newer invalidation or refresh.
- Test disconnect, age expiry, crossed/incomplete status, out-of-order pricing responses,
  same-venue independent best quotes, route recovery, and a route that differs from the
  independent cheapest-buy/best-sell pair.
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
schema v4         -> ARB-043 historical query/export
ARB-034           -> ARB-044 dashboard route correctness
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
| Research correctness and next roadmap (ARB-036 through ARB-044) | 112–180 h | ARB-036 through ARB-041 complete; ARB-042 through ARB-044 open |

Avoid expanding into execution modeling until the current detection-only claims, units,
and evidence are internally consistent; ARB-041 decided against live net episodes.
