# ArbSync backlog

Open tickets only. Estimates are engineering effort, not calendar duration, and include
implementation, tests, review fixes, and documentation. Completed tickets (ARB-001 through
ARB-051) are archived verbatim with their acceptance criteria in
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
  Gemini removed from REST reconciliation: complete.
- ARB-049 price-set reconciliation, ARB-050 Coinbase/Binance.US audit, and ARB-051 failed
  snapshot-fetch replay: complete; evidence and its timing limits are in `docs/VALIDATION.md`.

## Open tickets

No open tickets. The next scheduled soak should still show Gemini verification running for
hours with its outcomes counted; it is not a dedicated ticket and was not run in this session.

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
                       -> ARB-049 set-based reconciliation comparison (complete)
                       -> ARB-050 Coinbase and Binance.US snapshot audit (complete)
ARB-037 + ARB-038 -> ARB-051 replay of failed snapshot fetches (complete)
```

ARB-049, ARB-050, and ARB-051 were independent of each other. ARB-049 affects only
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
| Book trust (ARB-049 through ARB-051) | 9–15 h | Complete |

Avoid expanding into execution modeling until the current detection-only claims, units,
and evidence are internally consistent; ARB-041 decided against live net episodes.
