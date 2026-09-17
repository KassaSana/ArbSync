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
adapter supports one (Binance.US does, via `request_pair_resync`) and falls back
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
- `Gemini`: reconnects to `wss://ws.gemini.com?snapshot=-1`, treats the first `depthUpdate` per pair as the full snapshot, and validates later `U/u` ranges. `GET /v1/book/{symbol}` is used only for reconciliation comparisons, never stream recovery.
- `Coinbase`: waits for a fresh Level 2 stream snapshot and never mixes REST Exchange sequence numbers with Advanced Trade WebSocket sequence numbers.
- `Binance.US`: reads and bounds WebSocket updates while fetching `GET /api/v3/depth?symbol=...&limit=5000`. It discards updates covered by `lastUpdateId`, requires the first retained range to contain the snapshot ID, then checks every later `U/u` range. A sequence gap or an externally requested pair resync (confirmed reconciliation drift, invalid book) discards only that pair's sync state and re-fetches only that pair over the still-open socket, reusing the per-pair snapshot-task machinery from initial sync. Overflow, snapshot failure, repeated misalignment, or `serverShutdown` aborts to a full-venue reconnect, which remains the bounded fallback. An external pair request never disrupts a snapshot fetch already in flight for that pair: its buffered updates are kept, so the fetch still aligns and rebuilds the book scoped. Scoped resyncs are counted in `arb_adapter_pair_resyncs_total` by trigger (`sequence_gap`, `external`).
