# Resync Strategy

This project uses adapter-driven resync on sequence gaps.

Chosen strategy:
- Drop the local sequence chain for the affected `(exchange, pair)` stream
- Reject normalized snapshots that do not contain a usable bid and ask or whose best bid
  is greater than or equal to the best ask
- Reinitialize from the exchange-defined snapshot source
- Validate buffered updates against that snapshot before emitting normalized events
- Resume processing only after exchange continuity is established

Why this approach:
- It keeps the recovery logic close to the exchange adapter, which is where exchange-specific sequence semantics already live.
- It gives the `OrderBookManager` a clean snapshot boundary instead of forcing it to recover from partially trusted delta streams.
- It prevents later deltas from making an incomplete or crossed snapshot chain eligible.

`OrderBookManager` owns the shared validity check. When it rejects a snapshot or otherwise
clears a normalized chain, it returns a resynchronization signal to the pipeline. The
pipeline asks the originating adapter for a scoped single-pair resync when the
adapter supports one (Binance.US and Gemini do, via `request_pair_resync`) and falls back
to a full reconnect otherwise. Scoped recovery still starts from a cleared book
for the affected pair, so detection stays fail-closed for that pair while the
venue's other books keep flowing. Adapters without scoped recovery still reset
the whole connection, which invalidates that exchange's other books until the
new connection rebuilds them.

The periodic reconciler uses the same boundary after repeated live-versus-REST divergence.
It brackets each REST request with live reads and requires disagreement with both, so market
movement during the request does not count as evidence. Price mismatches use the normal
confirmation count; size-only mismatches require a longer streak with the same side and
direction. It clears the affected book before requesting reconnection, but does not apply
the REST comparison snapshot as stream state. Confirmation and cooldown prevent a single
non-atomic comparison from causing a recovery storm. Venues whose adapter verifies every
book against the exchange's own snapshots at the same update id (Gemini, since ARB-048) are
left out of REST reconciliation: ARB-047 showed Gemini's REST book is the stale side, so its
confirmations only rebuilt books that were already right.

Continuous verification: the adapter emits a `verify` event carrying the exchange's top
levels when, and only when, the book has applied exactly the update id those levels
describe. `OrderBookManager` compares them with the book's top levels (price and size; a
side shorter than the depth cap is the whole side, so the book must hold exactly those
levels) and,
on any disagreement, clears the book and returns the same resynchronization signal as an
invalid snapshot, so the pipeline requests the adapter's scoped resync. A match changes and
publishes nothing. Outcomes are counted in `arb_book_verifications_total` (`match`,
`mismatch`, `skipped` for a book not at that sequence, and `unaligned` for a snapshot whose
update id the book never stopped on).

Tradeoffs:
- Binance's REST snapshot introduces extra latency during its resync window.
- The affected pair's book stays ineligible throughout its own resync; other
  pairs on the same socket are unaffected unless recovery escalates to a full
  reconnect.
- Exchange update identifiers validate protocol continuity; normalized local sequences remain consecutive for `OrderBookManager`.

Per exchange:
- `Gemini`: connects to `wss://ws.gemini.com?snapshot=-1`, treats the first `depthUpdate` of each `{symbol}@depth` subscription as the full snapshot, and validates later ranges by Gemini's documented chain (`U` equals the previous frame's `u`; `U = u + 1` is also accepted). It also subscribes `{symbol}@depth20`, whose top-20 snapshots carry a `lastUpdateId` in the same id space: each is held until the book has applied exactly that id and then emitted as a `verify` event carrying Gemini's depth cap; a snapshot is counted `unaligned` and dropped if the book passes that id or the pair is uninitialized or resyncing. Once a pair has received a partial snapshot on a connection, 30 s of frame time without one verifying (Gemini sends them every second even when nothing changes) reconnects the venue with cause `verification_stalled`, so a lost `@depth20` stream cannot leave books trusted without a check; captures without `@depth20` are never watched and replay unchanged. A sequence gap or an externally requested pair resync (verification mismatch, invalid book) resubscribes only that pair's `@depth` stream on the open socket: the adapter drops the pair's state (emitting a level-less `reset` on a gap), sends `UNSUBSCRIBE`, ignores the pair's remaining frames, and sends `SUBSCRIBE` only after the unsubscribe is acknowledged, so the next frame for the pair is the new subscription's snapshot. Request ids are self-describing strings (`resync:<symbol>:<step>:<n>`), which Gemini echoes, so replay walks the same state machine from the recorded acknowledgements without recording outbound frames. While a pair awaits its snapshot, only a single-update-id frame (`U == u`, the subscription snapshot) may seed the book; a stray chained delta is dropped rather than trusted. A rejected step (`status` other than 200), no snapshot within 10 s of the first frame after the request, or more than three resyncs of one pair within 60 s falls back to a full-venue reconnect, whose exponential backoff then paces retries. All of these times are frame receipt times, so replay escalates exactly when live did. `GET /v1/book/{symbol}` is not used by the pipeline: Gemini is excluded from REST reconciliation because its REST book retains levels the stream deleted (ARB-047).
- `Coinbase`: waits for a fresh Level 2 stream snapshot and never mixes REST Exchange sequence numbers with Advanced Trade WebSocket sequence numbers.
- `Binance.US`: reads and bounds WebSocket updates while fetching `GET /api/v3/depth?symbol=...&limit=5000`. It discards updates covered by `lastUpdateId`, requires the first retained range to contain the snapshot ID, then checks every later `U/u` range. A sequence gap or an externally requested pair resync (confirmed reconciliation drift, invalid book) discards only that pair's sync state and re-fetches only that pair over the still-open socket, reusing the per-pair snapshot-task machinery from initial sync. A gap additionally emits a level-less `reset` event so the book manager drops the pair immediately rather than trusting it until the replacement snapshot arrives; external requests need none because their callers already invalidated the book. Overflow, snapshot failure, repeated misalignment, or `serverShutdown` aborts to a full-venue reconnect, which remains the bounded fallback. An external pair request never disrupts a snapshot fetch already in flight for that pair: its buffered updates are kept, so the fetch still aligns and rebuilds the book scoped. Scoped resyncs are counted in `arb_adapter_pair_resyncs_total` by trigger (`sequence_gap`, `external`).

## Reconnect causes

Every reconnect is counted in `arb_adapter_reconnects_total` by cause rather than exception class: `confirmed_drift` (reconciler, adapter without scoped resync), `invalid_book` (book manager rejected a snapshot or crossed book), `sequence_gap`, `protocol_error`, `missing_snapshot`, `invalid_update`, `snapshot_failed`, `snapshot_misaligned`, `buffer_overflow`, `server_shutdown`, `pair_resync_failed`, `pair_resync_timeout`, `pair_resync_repeated`, `verification_stalled`, and `transport_error` for a socket or network failure the adapter did not request.

## Gemini drift diagnosis (ARB-046, corrected by ARB-047)

The 2026-09-22 soak confirmed six Gemini price drifts (`DOT-USD` four times, `LTC-USD` twice). ARB-046 concluded that Gemini's incremental `@depth` stream diverges from Gemini's own snapshots. **ARB-047 measured this exactly and found the opposite: the stream-built book is correct, and the drifts are stale levels in the REST book.**

The exact test: Gemini's `{symbol}@depth20` stream sends a top-20 snapshot about once a second whose `lastUpdateId` is in the same id space as `@depth`, so the incremental book can be compared with Gemini's own levels at exactly the same update id, with no timing or REST involved. `tools/gemini_audit_capture.py` records the normal pipeline with that stream and `@trade` added; `tools/gemini_book_audit.py` performs the comparison. On a 45-minute capture (2026-09-22 22:23 local, all nine Gemini pairs, captures under `var/` are not committed):

- **The incremental book matched Gemini's top 20 exactly at all 21,797 aligned update ids** (price and size, both sides, every pair); 2,485 partial snapshots arrived at ids the book never stopped on and were skipped. A separate three-minute live run through the adapter matched 1,437 of 1,437, including the 27 checks taken immediately after a connect or a forced pair resubscription, so subscription snapshots agree with the stream too.
- **Trades side with the stream.** Of 1,214 Gemini trades, 160 printed at a price better than the book's best on the resting side; the depth stream mentioned every one of those prices within 2 s, which is an order placed and filled between one-second depth batches, not liquidity the stream missed.
- **The REST book keeps levels the stream deleted, and they never trade.** In 112 of the 720 REST comparison sides the REST best price was better than the stream book's best: 99 of those prices had been explicitly deleted by the stream (60 more than 5 s earlier) and 13 were never announced. None traded within 60 s, whereas the stream book's own best price traded within 60 s in 21 of 720 samples (2.9 %, about 3 expected).
- **Research data is unaffected in this window.** Of 38 replayed episodes with a Gemini leg, 37 had a Gemini price equal to Gemini's top-20 snapshot at the latest aligned id before the episode opened (the other had no aligned snapshot within 2 s); none was a phantom opportunity.

Why ARB-046 read it the other way: it compared the incremental book just before a rebuild with the new subscription's snapshot, about a second of batching apart, and counted a disagreement as stale when the stream had not mentioned that price for 30 s. On a tick grid a price deleted long ago is often re-added within the last batch, and a newly placed price has never been mentioned at all, so ordinary timing looked like staleness. On the confirmed `LTC-USD` drift of 2026-09-20 the REST book and a WebSocket snapshot taken 2 s later agreed with each other, but that snapshot was 2 s newer than the incremental book it was compared with. `tools/gemini_drift.py` now reports never-mentioned prices as `unannounced` rather than stale and points to the exact-id audit.

Still valid from ARB-046: the `U/u` chain is continuous for every frame in every capture (the only overlapping or already-covered frames are the first deltas after a snapshot), no frame repeats a price, decimal precision differences compare equal, and per-pair resubscription works as described above. Consequence: a confirmed REST mismatch on Gemini is evidence about the REST book, not ours; ARB-048 replaces REST-driven recovery for Gemini with continuous exact-id verification.

Protocol facts verified against the live endpoint (ARB-046): `UNSUBSCRIBE` then `SUBSCRIBE` on an open socket delivered a full snapshot (`U == u`, about 100 bids and 530 asks for `dotusd`) ahead of the subscribe acknowledgement while `ltcusd` continued uninterrupted; a repeated `SUBSCRIBE` without `UNSUBSCRIBE` produced nothing; string ids were echoed verbatim; an invalid stream returned `status: 400`. A 20-minute forced run made 81 scoped resyncs through `GeminiAdapter.connect()` with no reconnect, sequence gap, or refused request; a two-minute smoke rebuilt `DOT-USD` 0.11 s after the request.

Known replay limit: replay does not run the reconciler, so for a drift-triggered resync it learns of the request from the recorded unsubscribe acknowledgement. Frames between the live request and that acknowledgement (typically well under a second) are dropped live but applied in replay.
