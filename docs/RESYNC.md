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
non-atomic comparison from causing a recovery storm.

Tradeoffs:
- Binance's REST snapshot introduces extra latency during its resync window.
- The affected pair's book stays ineligible throughout its own resync; other
  pairs on the same socket are unaffected unless recovery escalates to a full
  reconnect.
- Exchange update identifiers validate protocol continuity; normalized local sequences remain consecutive for `OrderBookManager`.

Per exchange:
- `Gemini`: connects to `wss://ws.gemini.com?snapshot=-1`, treats the first `depthUpdate` of each `{symbol}@depth` subscription as the full snapshot, and validates later ranges by Gemini's documented chain (`U` equals the previous frame's `u`; `U = u + 1` is also accepted). A sequence gap or an externally requested pair resync (confirmed reconciliation drift, invalid book) resubscribes only that pair on the open socket: the adapter drops the pair's state (emitting a level-less `reset` on a gap), sends `UNSUBSCRIBE`, ignores the pair's remaining frames, and sends `SUBSCRIBE` only after the unsubscribe is acknowledged, so the next frame for the pair is the new subscription's snapshot. Request ids are self-describing strings (`resync:<symbol>:<step>:<n>`), which Gemini echoes, so replay walks the same state machine from the recorded acknowledgements without recording outbound frames. While a pair awaits its snapshot, only a single-update-id frame (`U == u`, the subscription snapshot) may seed the book; a stray chained delta is dropped rather than trusted. A rejected step (`status` other than 200), no snapshot within 10 s of the first frame after the request, or more than three resyncs of one pair within 60 s falls back to a full-venue reconnect, whose exponential backoff then paces retries. All of these times are frame receipt times, so replay escalates exactly when live did. `GET /v1/book/{symbol}` is used only for reconciliation comparisons, never stream recovery.
- `Coinbase`: waits for a fresh Level 2 stream snapshot and never mixes REST Exchange sequence numbers with Advanced Trade WebSocket sequence numbers.
- `Binance.US`: reads and bounds WebSocket updates while fetching `GET /api/v3/depth?symbol=...&limit=5000`. It discards updates covered by `lastUpdateId`, requires the first retained range to contain the snapshot ID, then checks every later `U/u` range. A sequence gap or an externally requested pair resync (confirmed reconciliation drift, invalid book) discards only that pair's sync state and re-fetches only that pair over the still-open socket, reusing the per-pair snapshot-task machinery from initial sync. A gap additionally emits a level-less `reset` event so the book manager drops the pair immediately rather than trusting it until the replacement snapshot arrives; external requests need none because their callers already invalidated the book. Overflow, snapshot failure, repeated misalignment, or `serverShutdown` aborts to a full-venue reconnect, which remains the bounded fallback. An external pair request never disrupts a snapshot fetch already in flight for that pair: its buffered updates are kept, so the fetch still aligns and rebuilds the book scoped. Scoped resyncs are counted in `arb_adapter_pair_resyncs_total` by trigger (`sequence_gap`, `external`).

## Reconnect causes

Every reconnect is counted in `arb_adapter_reconnects_total` by cause rather than exception class: `confirmed_drift` (reconciler, adapter without scoped resync), `invalid_book` (book manager rejected a snapshot or crossed book), `sequence_gap`, `protocol_error`, `missing_snapshot`, `invalid_update`, `snapshot_failed`, `snapshot_misaligned`, `buffer_overflow`, `server_shutdown`, `pair_resync_failed`, `pair_resync_timeout`, `pair_resync_repeated`, and `transport_error` for a socket or network failure the adapter did not request.

## Gemini drift diagnosis (ARB-046)

The 2026-09-22 soak confirmed six Gemini price drifts (`DOT-USD` four times, `LTC-USD` twice). `tools/gemini_drift.py` classifies drift from captured traffic without trusting REST: at every rebuild it compares the incremental book just before the rebuild with the snapshot Gemini sends for the new subscription, within the price range of the old book's top 50 levels per side, and marks a disagreement `stale` when the stream had not mentioned that price for more than 30 s (or never). Stale disagreements cannot be explained by the short gap between the two observations; non-stale ones mostly reflect the up-to-one-second batching of `@depth` frames.

Evidence (captures are local runtime data under `var/`, not committed):

| Capture | Gemini depth frames | Rebuilds compared | Levels agreeing | Stale missing from incremental | Stale ghosts in incremental |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-09-20, 45 min, 3 reconnects | 20,475 | 27 (reconnect, seconds of downtime) | 2,409 | 136 | 16 |
| 2026-09-22, 20 min, forced resync of every pair every 2 min | 10,247 | 81 (pair resync, about 0.1 s gap) | 7,650 | 115 | 8 |

- **Not a normalization defect.** In both captures, and across all 44,963 Gemini frames of a 90-minute 2026-09-22 capture, every frame continues its symbol's `U/u` chain (the few overlapping or already-covered frames are the first deltas after a snapshot); no frame repeats a price on a side; decimal strings of differing precision compare equal; the book applies removals and inserts exactly.
- **Not REST staleness.** The comparison uses Gemini's own WebSocket snapshot, not REST. On the confirmed `LTC-USD` drift of 2026-09-20, the REST comparison and the fresh WebSocket snapshot 2 s later agree with each other (`58.24`, `58.27`, `58.31` present) and disagree with the incremental book.
- **Genuine stream divergence, on every pair.** With a 0.1 s gap, every one of the nine pairs still had levels in Gemini's snapshot that its incremental stream had removed or never announced more than 30 s earlier (`BTC-USD` 34, `LINK-USD` 29, `AAVE-USD` 13, `SOL-USD` 12, `UNI-USD` 9, `LTC-USD` 7, `ETH-USD` 5, `AVAX-USD` 3, `DOT-USD` 3). `DOT-USD` and `LTC-USD` are the pairs that confirm because their sparse books turn a single missing level into a price difference above the reconciler's 0.5 % per-index threshold; dense books absorb it as a size-only mismatch. Gemini documents `@depth` as the price levels changed in the last second and prescribes resubscribing to resync, which is what recovery now does.

The protocol was verified against the live endpoint before implementation: `UNSUBSCRIBE` then `SUBSCRIBE` on an open socket delivered a full snapshot (`U == u`, about 100 bids and 530 asks for `dotusd`) ahead of the subscribe acknowledgement while `ltcusd` continued uninterrupted; a repeated `SUBSCRIBE` without `UNSUBSCRIBE` produced nothing; string ids were echoed verbatim; an invalid stream returned `status: 400`. The 20-minute forced run above made 81 scoped resyncs through `GeminiAdapter.connect()` with no reconnect, sequence gap, or refused request; a two-minute smoke rebuilt `DOT-USD` 0.11 s after the request.

Known replay limit: replay does not run the reconciler, so for a drift-triggered resync it learns of the request from the recorded unsubscribe acknowledgement. Frames between the live request and that acknowledgement (typically well under a second) are dropped live but applied in replay.
