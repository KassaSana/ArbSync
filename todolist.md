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
- Complete a documented uninterrupted multi-hour live soak.

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

### [x] ARB-002 — Stop treating USDT books as USD books

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

### [x] ARB-003 — Make configured pairs available during cold start

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

Resolution: `/api/pairs` returns the configured roster union the books actually seen,
sorted and deduplicated, so it is complete before any market event arrives. The dashboard
re-requests the roster whenever a socket connects, and offers a retry on both a failed and
an empty response rather than presenting emptiness as final. Cold start, partial
initialization and the unconfigured-symbol case are covered in `test_api.py`; reconnect
and empty-roster recovery in `live.test.tsx`.

### [x] ARB-004 — Exclude ineligible and stale books from dashboard spread calculations

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

Resolution: contributing books are gated on canonical eligibility; a state snapshot
replaces held books and statuses instead of merging into them; an ineligible `book_status`
drops the held quote outright rather than relying on every reader to check first; and the
age column is derived from the backend's `age_ms` plus locally elapsed time instead of the
exchange timestamp the backend deliberately does not trust. Covered by
`LiveSpreads.test.tsx` and `live.test.tsx`.

ARB-003 is listed as a dependency and remains open, but it governs which pairs appear at
all during cold start, not whether an ineligible book may contribute to a row.

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

### [x] ARB-006 — Remove or justify the unused `httpx2` dependency

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

Resolution: the use is deliberate. `starlette.testclient` does `import httpx2 as httpx`
and falls back to `httpx` with a deprecation warning, so every test using
`fastapi.testclient.TestClient` depends on it without naming it. It was briefly removed on
the grounds that nothing imports it, which is true and misleading; the dependency is
restored, documented at its declaration, and pinned by
`test_test_client_prefers_httpx2`.

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

### [x] ARB-020 — Harden public deployment defaults and control endpoints

- Priority: P0
- Estimate: 6 hours
- Dependencies: ARB-009, ARB-018

Problem: Local development now defaults to loopback, but hosted mode still permits every
CORS origin and exposes an unauthenticated uptime-reset endpoint. The project has no complete
hosted security profile or abuse controls for expensive/stateless public requests and
WebSockets.

Progress: the local bind defaults to `127.0.0.1`; `PORT` enables conventional hosted binding,
and `ARB_HOST` provides an explicit override. CORS now uses a validated explicit allowlist,
and the unauthenticated uptime-reset route was removed.

Acceptance criteria:

- Default local development to loopback or prominently explain network exposure.
- Make allowed origins configurable and restrictive in the hosted profile.
- Remove, protect, or explicitly scope the reset endpoint.
- Document reverse-proxy TLS, request-size, connection, timeout, and rate-limit expectations.
- Add tests for origin policy and any protected control route.

P0 total: **43 engineer-hours**.

## Reliability and maintainability (P1)

### [x] ARB-009 — Validate runtime configuration and preserve boundedness

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

### [x] ARB-010 — Turn reconciliation into safe, confirmed recovery

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

### [x] ARB-011 — Make persistence-worker failure and shutdown non-blocking

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

### [x] ARB-012 — Surface unexpected WebSocket sender failures

- Priority: P1
- Estimate: 3 hours
- Dependencies: none

Problem: [`_send_messages`](server/arb/broadcast.py) caught every `Exception` and silently
passes. Expected disconnect errors are routine, but serialization and programming errors
are currently indistinguishable from client departure.

Acceptance criteria:

- Classify and quietly handle expected disconnect/closed-socket exceptions.
- Log unexpected exceptions with useful client and message context without leaking data.
- Add a metric for unexpected sender failures.
- Retain guaranteed cleanup and add tests for expected disconnect, serialization failure,
  queue overflow, and cancellation.

### [x] ARB-013 — Validate snapshots before allowing delta-based recovery

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

### [x] ARB-014 — Add runtime validation for API and WebSocket payloads in the dashboard

- Priority: P1
- Estimate: 10 hours
- Dependencies: ARB-003, ARB-004

Problem: [`requestJson`](dashboard/src/api/client.ts) and the WebSocket handler previously cast
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

### [x] ARB-016 — Add contributor and security documentation

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

Resolution: added contributor setup/checks and review guidance, an alpha support and
private vulnerability-reporting policy, a maintainer-led code of conduct, three issue
templates, and a PR checklist. README links these entry points. Local documentation
links and issue-template metadata were checked. GitHub private vulnerability reporting
was confirmed disabled; SECURITY.md includes a contact-request fallback that keeps
vulnerability details private. Enabling the native reporting form remains an owner
setting decision.

### [x] ARB-017 — Add automated dependency and supply-chain maintenance

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

Resolution: configured weekly Dependabot updates for uv, npm, Actions, and isolated
audit tooling. Added push/PR/weekly/manual Python and npm audit jobs with read-only
permissions, immutable action pins, and no retained checkout credentials. Documented
triage and secret handling; GitHub secret scanning and push protection were confirmed
enabled. Initial npm findings were remediated with patched React Router, Vite, Vitest,
and compatible transitive updates, with navigation regression coverage. Local audits
report no known vulnerabilities; hosted Linux/Windows audit execution awaits a push.

### [x] ARB-018 — Make the installed application runnable outside the repository root

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

### [x] ARB-019 — Define SQLite retention and maintenance behavior

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

Resolution: added the explicit `arbsync-prune` command with row and SQL-work limits,
atomic rollup rebuilding, and bounded lock waits. History remains opt-in to pruning.
`docs/STORAGE.md` covers measured synthetic growth, backup, vacuum, and migration.
Boundary, concurrent-write, rollback, restart, statistics, and installed CLI checks
pass with the full backend suite and static checks.

### [x] ARB-021 — Complete and publish an uninterrupted multi-hour live soak

- Priority: P2
- Estimate: 4 engineer-hours plus at least 4 hours elapsed runtime
- Dependencies: ARB-002, ARB-010, ARB-011

Problem: [`VALIDATION.md`](docs/VALIDATION.md#L106) correctly identifies the missing
long-duration evidence. Short smoke runs cannot establish memory stability or recovery
behavior over time.

Progress: the observer captures labeled operational failure counters, observed process
restarts, missing RSS samples, and checkout/environment provenance. Restarted runs mark
counter deltas invalid, and the Windows launcher accepts an explicit configuration.
Raw JSONL sampling evidence, configuration fingerprints, and explicit missing-book
coverage are now available through the observer and Windows launcher.

This ticket originally required 24 uninterrupted hours. Two attempts on a single developer
workstation failed for environmental reasons, not defects: the September 12-13 run
accumulated nine sample gaps including one of 9.5 hours, and the September 13-14 run was
cut short at 107 minutes when its launcher process was killed. The bar is now a window the
available hardware can actually hold, with the published claim stating the duration that
was achieved. Twenty-four hours stays the better evidence if a machine can be dedicated.

Launch the observer from an interactive shell that outlives any tooling session. A soak
started as a child of a short-lived process dies with it, which is what ended the second
attempt.

Carry-forward observation to confirm in a valid run: across the second attempt's 108
samples every configured book stayed eligible except `gemini:DOT-USD`, which went stale
five times, reached 283 seconds against the 60-second limit, and recovered each time.
Thin-market staleness is the eligibility rules working, but a longer run should show it
stays bounded.

Resolution (2026-09-16): a four-hour run completed with 238 of 241 samples successful,
no restart or counter reset, zero sequence gaps, RSS +7.4 MiB start to end, and every
book eligible in all but one or two samples. The exceptions were a seven-minute host
network outage and one whole-venue Binance.US resync, both recovered within one interval.
`gemini:DOT-USD` never went stale. Details, environment, and the two reviewed events are
in [`VALIDATION.md`](docs/VALIDATION.md#live-soak-2026-09-16); the anomalies became
ARB-028 and ARB-029 rather than being folded into a pass.

Acceptance criteria:

- Run the documented observer for at least 4 uninterrupted hours against all configured
  pairs, and state the achieved duration wherever the result is cited.
- Capture RSS drift, adapter gaps/reconnects, eligibility age, recovery time, queue drops,
  background failures, observer failures, and process restarts.
- Investigate and ticket anomalies instead of labeling a degraded run successful.
- Commit the summarized report and update `VALIDATION.md` with exact environment, commit,
  dates, limitations, and links to evidence.

### [x] ARB-022 — Benchmark before changing the event loop, JSON parser, or book structure

- Priority: P2
- Estimate: 4 hours
- Dependencies: ARB-002, ARB-004

Problem: The earlier reviews suggest `orjson`, `sortedcontainers`, threads/processes, and
other scaling changes, but no current result isolates these as bottlenecks at the configured
27 subscriptions. Premature changes would add dependencies and concurrency complexity.

Resolution: repaired the current-market/browser harness, published four valid short
scenarios and three 60-second repeated profiles, and measured exclusive main-thread
function time plus SQLite worker-thread CPU. The worker used 0.094-0.203 seconds of CPU
per minute at 1,100 events/second; all repeats processed 66,000 events without loss.
The measurable optimization gates in
[`ARB-022.md`](artifacts/benchmarks/performance/ARB-022.md) justify no dependency or
architecture change. Main-thread attribution remains elapsed self time, and the Windows
worker CPU clock is quantized; deeper book sweeps are required if a future measurement
crosses the mutation gate and motivates a replacement structure.

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

Progress: added an Unreleased changelog with migration and limitation notes, alpha
versioning rules, and a candidate checklist covering both operating systems, packages,
licenses, scans, and artifact provenance. Publication remains gated on verified runner
evidence and owner review; no release tag has been created.
The content/artifact checker now validates filesystem links, package/lock versions,
archive contents and SHA-256 hashes. The expanded matrix has now run on hosted runners:
[run 34796528741](https://github.com/KassaSana/ArbSync/actions/runs/34796528741) passed all
four jobs, `backend` and `frontend` on both `ubuntu-latest` and `windows-latest`, covering
the documented backend commands (tests with coverage, mypy, ruff, format, repository hooks,
release checks, package build and inspection) and dashboard commands (lint, typecheck, test,
build). The README carries badges for that workflow and for the dependency audit, both of
which passed on the same push. The audit badge is deliberate on the owner's call: it also
runs weekly on a schedule, so it can turn red from a new upstream advisory with nothing in
this repository having changed. For a security-adjacent project that is a signal worth
surfacing, not a broken build.

Remaining: create the release tag after owner review. Everything else is done.

Acceptance criteria:

- Add a changelog or release-note process and document versioning policy.
- Create a clean-checkout release checklist covering Python, dashboard, docs, package build,
  secret scan, license, and artifact provenance.
- Add CI/status badges only for stable public workflows.
- Verify all README commands on Windows and one Unix-like CI runner.
- Tag the release only after required tickets and checks pass.

### [x] ARB-025 — Reject interrupted soak evidence

- Priority: P2
- Estimate: 2 hours
- Dependencies: ARB-021 observer tooling

Problem: the September 12-13 run crossed the requested wall-clock duration after nine
large sampling gaps, including a 9.5-hour suspension, and was labeled complete even though
it did not provide 24 uninterrupted hours of evidence.

Resolution: sample starts now follow a fixed schedule and have an explicit maximum gap.
Exceeding it writes the gap to raw and summarized evidence, marks the report interrupted,
exits nonzero, and preserves the partial run for diagnosis. The Windows launcher defaults
to a two-interval limit and documents the limits of programmatic sleep prevention.

### [x] ARB-026 — Exercise WebSocket delivery during live soaks

- Priority: P2
- Estimate: 3 hours
- Dependencies: ARB-012, ARB-025

Problem: the observer recorded server-side delivery counters without connecting a client,
so zero queue overflows and sender failures did not demonstrate sustained live delivery.

Resolution: official soaks now connect a lightweight `/ws/live` consumer before their
timer starts. It continuously drains and validates envelopes, initial state, message types,
and per-connection stream sequences; reports frames, reconnects, malformed messages and
outages; and interrupts the run if delivery stays unavailable beyond the sample-gap limit.
Browser rendering remains covered by the separate connected-dashboard benchmark.

### [x] ARB-027 — Corroborate non-atomic reconciliation mismatches

- Priority: P2
- Estimate: 4 hours
- Dependencies: ARB-010, ARB-021 diagnostic evidence

Problem: 1,387 of 1,684 mismatches in the interrupted long observation were size-only,
including all 279 Gemini LINK-USD mismatches and 40 completed forced recoveries. Comparing
a live book captured only before a network REST request made normal depth churn look like
persistent corruption.

Resolution: REST comparisons are now bracketed by live reads and must disagree with both.
Price evidence retains its three-cycle confirmation path; size-only evidence must keep the
same side and direction across a separately configurable five-cycle streak. Cause-specific
metrics distinguish price, size, and combined evidence while canonical invalidation,
adapter-owned recovery, cooldown, and fail-closed behavior remain unchanged.

### [ ] ARB-028 — Resynchronize one Binance.US pair without dropping the venue

- Priority: P2
- Estimate: 4 hours
- Dependencies: ARB-010, ARB-021

Problem: the Binance.US adapter carries every configured pair on one combined stream, so
a confirmed reconciliation drift on a single thin pair (`DOT-USDT`, 13:18Z in the
2026-09-16 soak) reconnects the whole socket and reports all nine books `disconnected`
until the rebuild finishes (13.9 s observed). Detection excluded them correctly, but one
pair's drift should not remove a venue from detection.

Acceptance criteria:

- A confirmed drift on one Binance.US pair re-fetches and re-aligns that pair's book
  without disconnecting the others, or the design decision not to is recorded in
  [`RESYNC.md`](docs/RESYNC.md) with the measured cost.
- Sequence validation for the untouched pairs is unaffected; tests cover a mid-stream
  single-pair resync while other pairs keep receiving deltas.
- Book eligibility for the other pairs stays `true` across the resync in a replay test.

### [ ] ARB-029 — Attribute host connectivity loss in the soak observer

- Priority: P3
- Estimate: 2 hours
- Dependencies: ARB-021 observer tooling

Problem: the 2026-09-16 soak contained a seven-minute window in which every exchange
socket, every REST snapshot, and the observer's own loopback requests failed together.
That is a host outage, but the report can only show correlated failures; the attribution
was made afterwards from the Windows event log.

Acceptance criteria:

- Each sample records a lightweight host-connectivity probe (for example a DNS lookup or
  TCP connect to a stable public endpoint) alongside the backend sample.
- The report distinguishes "backend unreachable while host connectivity was lost" from
  "backend unreachable while the host was online", and summarizes outage windows.
- Probe failures never fail or shorten the run on their own.

P2 total: **29 engineer-hours plus the soak runtime**.

## Planning summary

| Milestone | Engineer effort | Release requirement |
| --- | ---: | --- |
| P0 release gate | 43 h | Required before public announcement |
| P1 reliability and maintainability | 54 h | Strongly recommended for the first stable release |
| P2 operations and evidence | 27 h | Can follow initial publication except where dependencies say otherwise |
| Total | **124 h** | About 3.1 engineer-weeks at 40 h/week |

The critical path is ARB-002 -> ARB-004/ARB-010 -> ARB-021. The highest-risk issue is
quote-currency conflation, not performance. Avoid expanding into execution modeling until
the current detection-only claims, units, and evidence are internally consistent.
