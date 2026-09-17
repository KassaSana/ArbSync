# Captured exchange traffic

`three_venue_150s_20260917.jsonl.gz` was recorded on 2026-09-17 with
`arbsync capture --duration 150s` against the shipped nine-pair roster on
Gemini, Coinbase, and Binance.US: 15,455 frames (WebSocket texts plus the
REST snapshot payloads the adapters and reconciler fetched mid-stream).

The file is public market data: order-book texts and snapshot payloads.
It contains no credentials, account data, or personal information.

The recording covers a quiet window: replaying it through the production
pipeline yields 15,226 book transitions across all 27 books but no spread
reaches the configured 0.1% detection threshold (the largest raw
cross-venue spread is about 0.04%). It still exercises multi-venue
sequencing, Binance.US snapshot alignment, reconciler snapshot
interleaving, and replay determinism. An opportunity-rich window can be
added later for episode-boundary tests without changing the format.

Longer captures stay out of Git; each committed file must stay under
roughly 5 MB (see `test_captured_fixtures_stay_small`).
