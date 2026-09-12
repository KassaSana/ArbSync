---
name: Protocol correctness
about: Report an exchange feed, sequencing, eligibility, or recovery defect
title: ""
labels: ""
assignees: ""
---

<!-- If the defect is exploitable, report privately using SECURITY.md. -->

## Affected feed

- Commit:
- Exchange, channel, and native symbol:
- Normalized base/quote pair:
- UTC time and relevant connection/reconnect history:

## Expected and observed behavior

Explain the sequence or snapshot rule involved and link the exchange specification
if available. Describe how eligibility, recovery, or theoretical detection was affected.

## Minimal evidence

Provide sanitized snapshot/delta frames, sequence IDs, and relevant status/log output.
Identify synthetic examples versus captured traffic and explain any sanitization.
Include replay steps or a regression test if possible. Do not attach a full database.

## Checks

- [ ] I searched existing issues and read docs/RESYNC.md.
- [ ] I identified the quote asset and did not assume USD/USDT parity.
- [ ] I removed credentials and sensitive data from the evidence.
