# Test fixture provenance

The files under `synthetic/` are small, hand-authored protocol examples based on the
public Gemini, Coinbase, and Binance.US market-data schemas. Each file contains three
messages chosen to exercise snapshot/delta parsing and final order-book construction.

They are not live captures, do not represent five minutes of traffic, and are not evidence
of sustained exchange behavior. Because the payloads were created for tests rather than
recorded from users or authenticated sessions, they contain no credentials, account data,
or personal information requiring sanitization.
