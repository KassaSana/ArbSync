# ARB-040 route leg age and skew evidence

Offline analysis of one 45-minute, three-venue capture recorded on 2026-09-20
(`arbsync capture --duration 45m`, 20:54:59–21:40:11 UTC, 27 configured pairs,
capture format version 2, integrity `lossless`, provenance `complete`). The capture
itself (79 MB compressed) stays outside Git under `var/`; the replay digest in
`report.json` identifies it. Analysis ran with the default bands and
`--no-sensitivity --null-surrogates 0`; the lead/lag datasets from the same run are
not part of this evidence.

Host: Windows 11, Python 3.12, so `time.monotonic_ns` advances in roughly 15.6 ms
steps; every age below is quantized to that tick.

## What the capture showed

- 306,893 canonical transitions, 9 REST snapshots consumed, 132 episodes at the
  configured 0.1% threshold; 131 closed `spread_closed`, 1 `book_ineligible`.
- Fee survival was zero at every notional (`survival_by_notional.jsonl`): no episode
  had a net-positive stored ledger at 100, 1,000, 10,000, or 50,000 quote units.
- At open, the older leg's age equalled the skew in every episode (median 617 ms,
  p90 1.6 s, max 4.8 s), because detection runs on the updating leg's own event, so
  that leg is 0 ms old by construction. The two dimensions separate only at peak and
  close and under age-expiry scans, not at open.
- Route comparisons (`evaluations`) concentrated in the 250–1,000 ms skew band
  (1.04 M of 1.84 M); the open rate per evaluation was 5–9 per 100,000 in every
  populated band and showed no monotone trend with age or skew.
- No episode opened with a leg older than 5 s; the 5–15 s and >15 s bands hold
  58,590 evaluations and zero episodes.

## Decision

No default route gate. The rule stated before the capture required a cutoff that
rejects a materially larger share of `book_ineligible`/zero-survivor episodes than of
fee-surviving episodes, stable across at least two of four windows. With zero
survivors there is no class for a gate to protect, and the single `book_ineligible`
close sits inside the 100–500 ms band with 44 ordinary closes. Every cutoff below
1 s would have discarded 16–93% of theoretical peak profit while separating nothing.

Absolute age, relative skew, connection state, and sequence continuity remain
separate observed dimensions. Revisit only with a capture that contains fee
survivors, or with a stated reason to gate that does not depend on survival.
