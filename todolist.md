# ArbSync open-source readiness backlog

This backlog consolidates the repository audit and the earlier Claude and Gemini reviews.
Estimates are engineering effort, not calendar duration, and include implementation, tests,
review fixes, and documentation. They assume one engineer already familiar with Python,
asyncio, React, and exchange market-data protocols.

## Audit baseline

- Backend verification baseline: [`docs/VALIDATION.md`](docs/VALIDATION.md).
- Static checks: Ruff check and format, strict mypy, TypeScript typecheck, and ESLint pass.
- Repository documentation has no broken relative links.
- No obvious committed credentials were found.
- The worktree already contained unrelated changes to `AGENTS.md`, `CLAUDE.md`, `.claude/`,
  and `.gemini/`; this audit did not alter them.

## Decisions on the prior reviews

Accepted:

- Fix the dashboard's use of ineligible cached books.
- Remove or explicitly justify the unused `httpx2` development dependency.
- Make the Decimal invariant accurately describe the derived floating-point rollup.
- Preserve expected WebSocket disconnect handling while surfacing unexpected sender errors.
- Complete the documented 24-hour live soak.

Accepted with a different implementation:

- Reconciliation mismatches need an operational response, but a single REST/live mismatch
  must not immediately force a reconnect. REST and WebSocket views are not atomic; use
  repeated confirmation, hysteresis, and observable recovery.
- Measure order-book data-structure and JSON-decoding alternatives before adding a tree,
  `sortedcontainers`, `orjson`, worker threads, or processes.

Not open-source release blockers:

- Fees, slippage, inventory, and multi-level VWAP are product-scope expansions. ArbSync
  already describes itself as a detection-only, top-of-book observability project.
- The single event loop and Python GIL are constraints, not demonstrated defects at the
  configured workload.
- The five curated benchmark summaries are useful supporting evidence; only raw artifacts
  need to remain ignored.
- `!.env.example` is not dead: it permits the tracked `dashboard/.env.example` through the
  preceding `.env.*` rule.

## Release gate (P0)

Complete every P0 ticket before announcing the repository as open source.

### [x] ARB-001 — Choose and publish the project license

- Priority: P0
- Estimate: 2 hours
- Dependencies: owner chooses a license

Problem: The repository has no `LICENSE` or `COPYING` file and `pyproject.toml` has no
license metadata. Public source without a license does not grant normal reuse,
modification, or redistribution rights.

Acceptance criteria:

- Add the owner-approved license text at the repository root.
- Add the matching SPDX license expression to `pyproject.toml`.
- State the license in the README.
- Verify dependency licenses are compatible with the chosen project license.

### [ ] ARB-002 — Stop treating USDT books as USD books

- Priority: P0
- Estimate: 16 hours
- Dependencies: product decision on cross-quote comparison

Problem: [`normalize_binance_symbol`](server/arb/adapters/binance.py#L15) maps `BTCUSDT`
to `BTC-USD`, while Gemini and Coinbase really are USD markets. The detector then compares
USDT and USD prices as if the quote currencies were identical and labels profit as USD.
A USDT/USD deviation can therefore appear as an arbitrage opportunity.

Acceptance criteria:

- Preserve the true quote asset in every normalized pair, such as `BTC-USDT`.
- Compare books only when base and quote assets are economically compatible.
- If USD/USDT conversion is supported, model the conversion explicitly, timestamp it,
  apply it consistently to prices and profit units, and document its assumptions.
- Decide how existing SQLite rows with conflated pair names are migrated or discarded.
- Add detector, adapter, API, persistence, and dashboard tests covering mixed quote assets.
- Update the README and architecture documentation so no claim implies USD/USDT parity.

### [ ] ARB-003 — Make configured pairs available during cold start

- Priority: P0
- Estimate: 4 hours
- Dependencies: none

Problem: [`GET /api/pairs`](server/arb/api.py#L309) returns `book_manager.known_pairs()`,
which contains only books that have received an event. The dashboard fetches this endpoint
once in [`LiveProvider`](dashboard/src/state/live.tsx#L133). A dashboard opened before the
first snapshots can permanently render an empty pair table.

Acceptance criteria:

- Return configured/tracked pairs independently of book initialization state.
- Preserve deterministic sorting and avoid duplicate pairs.
- Add a backend test for `/api/pairs` before any market event is received.
- Add a dashboard recovery path for a failed or empty initial pair request.
- Verify cold start, partial exchange initialization, and reconnect behavior.

### [ ] ARB-004 — Exclude ineligible and stale books from dashboard spread calculations

- Priority: P0
- Estimate: 6 hours
- Dependencies: ARB-002, ARB-003

Problem: [`LiveSpreads`](dashboard/src/components/LiveSpreads.tsx#L63) records venue
eligibility but adds every cached book to `row.entries`. Disconnected, stale, crossed, or
otherwise ineligible books can still determine the displayed best bid, best ask, and
spread even though backend detection correctly excludes them.

Acceptance criteria:

- Include a book in calculations only when its canonical status is eligible.
- Replace, rather than only merge, authoritative state on a new WebSocket state snapshot.
- Remove or ignore cached top-of-book data immediately when an ineligible status arrives.
- Derive displayed contributing-book age from canonical status age where possible.
- Add frontend tests for disconnect, age expiry, crossed/incomplete status, reconnect, and
  a state snapshot that omits a previously eligible book.

### [x] ARB-005 — Repair the pre-commit backend hook paths

- Priority: P0
- Estimate: 1 hour
- Dependencies: none

Problem: [`.pre-commit-config.yaml`](.pre-commit-config.yaml#L13) invokes
`server/scripts/run_backend_tool.py`, but the helper now lives at
`tools/run_backend_tool.py`. All three backend hooks fail before running their tools.

Acceptance criteria:

- Point Ruff check, Ruff format, and mypy hooks to the real helper path.
- Run `pre-commit run --all-files` successfully from a clean checkout.
- Add a lightweight CI check that exercises the local hook configuration or its commands.

### [ ] ARB-006 — Remove or justify the unused `httpx2` dependency

- Priority: P0
- Estimate: 1 hour
- Dependencies: none

Problem: [`pyproject.toml`](pyproject.toml#L19) installs `httpx2` for development, but the
code imports `httpx`. The similarly named unused package also pulls in `httpcore2` and
`truststore`, increasing install time and supply-chain surface.

Acceptance criteria:

- Confirm there is no deliberate use case; remove it if unused.
- Regenerate `uv.lock` with the approved dependency set.
- Run the backend test, lint, format, and typecheck commands with `uv sync --locked`.
- If it is deliberate, document exactly which tool requires it and add a test or command
  that proves that requirement.

### [x] ARB-007 — Make fixtures and validation claims accurately describe their evidence

- Priority: P0
- Estimate: 3 hours
- Dependencies: none

Problem (resolved): the former `server/tests/fixtures/recorded/*_5min.jsonl` files contained
three synthetic/example frames per exchange, not five minutes of recorded traffic. Their
old path and filenames overstated what they validated, and manually copied test counts had
drifted between the README and validation record.

Acceptance criteria:

- Rename the fixtures and replay language to say `synthetic` or add genuine, sanitized,
  reproducibly captured samples that match the current names.
- Document fixture provenance and sanitization.
- Ensure replay claims distinguish parser examples from live-traffic evidence.
- Replace manually maintained test counts with a timeless statement, or update every
  count from one source of truth.
- Re-run link checks after renaming paths.

### [x] ARB-008 — Decide whether to expose the existing commit email history

- Priority: P0
- Estimate: 1 hour
- Dependencies: owner decision before public forks exist

Problem: Most existing commits expose the owner's personal Gmail address rather than a
GitHub noreply address. Once public forks exist, removing that history becomes disruptive.

Acceptance criteria:

- Explicitly decide whether the current email exposure is acceptable.
- If not acceptable, rewrite authors and committers before publication and verify every
  rewritten commit; coordinate the force-push while the repository is still private.
- Configure the owner's intended Git identity for future commits.
- Do not add AI authors, co-authors, generated-by trailers, or invented identities.

### [x] ARB-024 — Make agent configuration portable and attribution-safe

- Priority: P0
- Estimate: 3 hours
- Dependencies: none

Problem: Repository guidance was duplicated or configured differently across coding tools,
pinned unsafe automatic pushing behavior, and relied only on prose to prevent an AI coding
agent from being recorded as a contributor.

Acceptance criteria:

- Keep one concise, model-neutral instruction source with only the minimal tool shims needed
  for Claude, Codex, Gemini CLI, and Cursor.
- Keep personal output-style settings local and untracked.
- Preserve real human authorship while prohibiting AI-agent author, committer, co-author,
  contributor, generated-by, and assisted-by attribution.
- Enforce structured attribution locally and in CI without rejecting ordinary exchange or
  tooling discussion in commit prose.
- Create verified local commits after completed tickets and require an explicit owner request
  before any push.

### [ ] ARB-020 — Harden public deployment defaults and control endpoints

- Priority: P0
- Estimate: 6 hours
- Dependencies: ARB-009, ARB-018

Problem: Local development now defaults to loopback, but hosted mode still permits every
CORS origin and exposes an unauthenticated uptime-reset endpoint. The project has no complete
hosted security profile or abuse controls for expensive/stateless public requests and
WebSockets.

Progress: the local bind defaults to `127.0.0.1`; `PORT` enables conventional hosted binding,
and `ARB_HOST` provides an explicit override. The remaining criteria keep this ticket open.

Acceptance criteria:

- Default local development to loopback or prominently explain network exposure.
- Make allowed origins configurable and restrictive in the hosted profile.
- Remove, protect, or explicitly scope the reset endpoint.
- Document reverse-proxy TLS, request-size, connection, timeout, and rate-limit expectations.
- Add tests for origin policy and any protected control route.

P0 total: **43 engineer-hours**.

## Reliability and maintainability (P1)

### [ ] ARB-009 — Validate runtime configuration and preserve boundedness

- Priority: P1
- Estimate: 5 hours
- Dependencies: none

Problem: [`load_config`](server/arb/config.py#L41) coerces values but does not validate
ranges or relationships. In particular, an asyncio queue `maxsize <= 0` becomes
unbounded, contradicting the bounded-persistence invariant. Invalid batch sizes, flush
intervals, age limits, ports, symbols, and thresholds fail late or behave unexpectedly.

Acceptance criteria:

- Reject non-positive batch size, queue size, flush interval, and max book age.
- Validate port range, non-negative threshold, supported exchange names, non-empty symbols,
  and duplicate normalized pairs.
- Emit actionable startup errors that identify the field and bad value.
- Add table-driven tests for valid boundaries and every rejected category.

### [ ] ARB-010 — Turn reconciliation into safe, confirmed recovery

- Priority: P1
- Estimate: 10 hours
- Dependencies: ARB-009

Problem: [`SnapshotReconciler`](server/arb/reconcile.py#L39) only logs mismatches. A silently
wrong book can remain eligible indefinitely. Conversely, immediately reconnecting on one
mismatch would create false recovery storms because REST and WebSocket snapshots are not
atomic. With 27 targets and one target checked every 60 seconds, each book is currently
revisited only about every 27 minutes.

Acceptance criteria:

- Document whether the interval applies per target or per full reconciliation cycle.
- Track repeated mismatches per exchange/pair with configurable confirmation and cooldown.
- Compare enough state to detect meaningful divergence, including size where appropriate.
- On confirmed mismatch, make the affected book ineligible before adapter-owned recovery.
- Record mismatch, confirmation, recovery start, recovery completion, and failure metrics.
- Test transient mismatch, persistent mismatch, cooldown, recovery, and multi-pair cadence.

### [ ] ARB-011 — Make persistence-worker failure and shutdown non-blocking

- Priority: P1
- Estimate: 5 hours
- Dependencies: none

Problem: A SQLite flush failure ends the persistence task while ingestion continues until
the queue fills. During shutdown, `OpportunityStore.close()` can wait to enqueue its
sentinel into that full queue even though no worker remains to consume it.

Acceptance criteria:

- Propagate persistence-worker failure into an explicit failed/closed store state.
- Stop accepting new rows immediately after worker failure and count/report drops by reason.
- Guarantee shutdown completes within a bounded timeout after worker failure and a full
  queue; report any unflushed row count.
- Add fault-injection tests for initialize, flush, commit, full queue, and shutdown paths.

### [ ] ARB-012 — Surface unexpected WebSocket sender failures

- Priority: P1
- Estimate: 3 hours
- Dependencies: none

Problem: [`_send_messages`](server/arb/api.py#L211) catches every `Exception` and silently
passes. Expected disconnect errors are routine, but serialization and programming errors
are currently indistinguishable from client departure.

Acceptance criteria:

- Classify and quietly handle expected disconnect/closed-socket exceptions.
- Log unexpected exceptions with useful client and message context without leaking data.
- Add a metric for unexpected sender failures.
- Retain guaranteed cleanup and add tests for expected disconnect, serialization failure,
  queue overflow, and cancellation.

### [ ] ARB-013 — Validate snapshots before allowing delta-based recovery

- Priority: P1
- Estimate: 4 hours
- Dependencies: none

Problem: [`OrderBookManager.apply`](server/arb/orderbook.py#L129) accepts snapshots without
rejecting incomplete or crossed state. Eligibility prevents immediate detection, but a
later delta can make that same chain eligible without obtaining another trusted snapshot.

Acceptance criteria:

- Define whether incomplete/crossed snapshots are invalid or temporarily recoverable.
- If invalid, clear the chain and require adapter resynchronization.
- Keep adapter recovery ownership intact.
- Add tests for incomplete snapshot, crossed snapshot, a later uncrossing delta, and a
  subsequent valid snapshot.

### [ ] ARB-014 — Add runtime validation for API and WebSocket payloads in the dashboard

- Priority: P1
- Estimate: 10 hours
- Dependencies: ARB-003, ARB-004

Problem: [`requestJson`](dashboard/src/api/client.ts#L112) and the WebSocket handler cast
unvalidated JSON directly to TypeScript types. TypeScript provides no runtime guarantee;
structurally bad data can poison state. React error boundaries also do not catch errors in
event handlers, so the current comment about malformed frames is stronger than the actual
protection.

Acceptance criteria:

- Validate every REST response and live envelope at the network boundary.
- Ignore or quarantine malformed live frames and expose a diagnostic counter/status.
- Treat invalid REST responses as typed request failures with endpoint context.
- Add a frontend test runner and tests for malformed JSON, missing fields, invalid Decimal
  strings, invalid nanosecond timestamps, unknown message types, and valid payloads.

### [x] ARB-015 — Clarify canonical Decimal guarantees and derived-statistics precision

- Priority: P1
- Estimate: 1 hour
- Dependencies: owner approval of the intended invariant

Problem (resolved): `AGENTS.md` previously said Decimal values never passed through binary
floats, but [`opportunity_minutes`](server/arb/persistence.py#L36) deliberately stores
derived aggregates as SQLite `REAL`, and `_rollup_rows` converts Decimal values to `float`.
Canonical opportunity rows remain exact text; derived statistics are approximate.

Acceptance criteria:

- State that canonical prices, sizes, spreads, profits, and serialized wire values remain
  decimal strings.
- Explicitly document the precision policy for derived rollups and statistics.
- Remove claims that the floating-point rollup is numerically exact, or replace it with an
  exact representation if exact aggregate output is required.
- Add a precision regression test using values that are awkward in binary floating point.

### [ ] ARB-016 — Add contributor and security documentation

- Priority: P1
- Estimate: 5 hours
- Dependencies: ARB-001

Problem: The repository has no `CONTRIBUTING.md`, `SECURITY.md`, code of conduct, issue
templates, or pull-request template. New contributors do not have a public path for setup,
scope decisions, responsible vulnerability reporting, or review expectations.

Acceptance criteria:

- Add concise contribution setup and verification instructions without duplicating
  `AGENTS.md` internals.
- Publish supported-version and private vulnerability-reporting guidance.
- Add a code of conduct appropriate to the chosen community model.
- Add bug, protocol-correctness, and feature-request issue templates plus a PR checklist.
- Include the detection-only/non-execution scope in contribution guidance.

### [ ] ARB-017 — Add automated dependency and supply-chain maintenance

- Priority: P1
- Estimate: 5 hours
- Dependencies: ARB-006

Problem: CI verifies behavior but does not automate dependency updates, vulnerability
auditing, or workflow hardening. GitHub Actions are tag-pinned rather than commit-SHA-pinned.

Acceptance criteria:

- Configure Dependabot or Renovate for Python, npm, and GitHub Actions.
- Add Python and npm vulnerability audits with a documented triage policy.
- Pin third-party actions to immutable SHAs while retaining readable version comments.
- Grant the CI workflow only the permissions it needs.
- Add secret scanning guidance and enable repository-native scanning where available.

### [ ] ARB-018 — Make the installed application runnable outside the repository root

- Priority: P1
- Estimate: 6 hours
- Dependencies: ARB-009

Problem: `arb.main` loads `config.toml` relative to the current working directory and the
package exposes no console entry point. An installed package fails unless launched from a
checkout with the expected file layout.

Acceptance criteria:

- Add an `arbsync` console command or clearly declare that the project is checkout-only.
- Support an explicit config path through a CLI flag or environment variable.
- Ship or generate a documented example config without silently selecting unsafe values.
- Add package metadata: repository URL, issue URL, Python classifiers, authorship, and
  supported Python versions.
- Test an installed wheel from a temporary directory, including missing-config errors.

P1 total: **54 engineer-hours**.

## Operational hardening and evidence (P2)

### [ ] ARB-019 — Define SQLite retention and maintenance behavior

- Priority: P2
- Estimate: 5 hours
- Dependencies: ARB-009

Problem: Opportunity history and minute rollups grow forever. Long-running public users
have no documented disk-growth, pruning, vacuum, backup, or migration policy.

Acceptance criteria:

- Document expected growth from measured rates.
- Add configurable retention or explicitly document manual retention procedures.
- Prune canonical rows and rollups transactionally without blocking ingestion for an
  unbounded period.
- Test pruning boundaries, active writes, restart, and statistics after retention.

### [ ] ARB-021 — Complete and publish the 24-hour live soak

- Priority: P2
- Estimate: 6 engineer-hours plus 24 hours elapsed runtime
- Dependencies: ARB-002, ARB-010, ARB-011

Problem: [`VALIDATION.md`](docs/VALIDATION.md#L106) correctly identifies the missing
long-duration evidence. Short smoke runs cannot establish memory stability or recovery
behavior over a full day.

Acceptance criteria:

- Run the documented observer for at least 24 uninterrupted hours against all configured
  pairs after P0 correctness fixes.
- Capture RSS drift, adapter gaps/reconnects, eligibility age, recovery time, queue drops,
  background failures, observer failures, and process restarts.
- Investigate and ticket anomalies instead of labeling a degraded run successful.
- Commit the summarized report and update `VALIDATION.md` with exact environment, commit,
  dates, limitations, and links to evidence.

### [ ] ARB-022 — Benchmark before changing the event loop, JSON parser, or book structure

- Priority: P2
- Estimate: 4 hours
- Dependencies: ARB-002, ARB-004

Problem: The earlier reviews suggest `orjson`, `sortedcontainers`, threads/processes, and
other scaling changes, but no current result isolates these as bottlenecks at the configured
27 subscriptions. Premature changes would add dependencies and concurrency complexity.

Acceptance criteria:

- Profile representative live-like depth and churn, not only detector permutations.
- Attribute CPU time and event-loop lag among JSON decode, Decimal conversion, sorted-level
  mutation, detection, persistence, and WebSocket delivery.
- Define a measurable threshold that justifies each proposed dependency or architecture
  change.
- Open separate implementation tickets only for changes with demonstrated benefit.

### [ ] ARB-023 — Prepare the first public release and project presentation

- Priority: P2
- Estimate: 3 hours
- Dependencies: all P0 tickets, ARB-016, ARB-017, ARB-018

Problem: The README is technically strong, but the repository has no release tag,
changelog/release notes, support policy, or concise verified-release checklist.

Acceptance criteria:

- Add a changelog or release-note process and document versioning policy.
- Create a clean-checkout release checklist covering Python, dashboard, docs, package build,
  secret scan, license, and artifact provenance.
- Add CI/status badges only for stable public workflows.
- Verify all README commands on Windows and one Unix-like CI runner.
- Tag the release only after required tickets and checks pass.

P2 total: **18 engineer-hours plus the 24-hour soak runtime**.

## Planning summary

| Milestone | Engineer effort | Release requirement |
| --- | ---: | --- |
| P0 release gate | 43 h | Required before public announcement |
| P1 reliability and maintainability | 54 h | Strongly recommended for the first stable release |
| P2 operations and evidence | 18 h | Can follow initial publication except where dependencies say otherwise |
| Total | **115 h** | About 2.9 engineer-weeks at 40 h/week |

The critical path is ARB-002 -> ARB-004/ARB-010 -> ARB-021. The highest-risk issue is
quote-currency conflation, not performance. Avoid expanding into execution modeling until
the current detection-only claims, units, and evidence are internally consistent.
