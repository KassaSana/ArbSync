# Completed ArbSync tickets

Verbatim archive of the tickets closed from [`../todolist.md`](../todolist.md), kept for
their acceptance criteria and evidence notes. The open backlog, dependency order, and
planning summary live in `todolist.md`; user-facing outcomes are in
[`../CHANGELOG.md`](../CHANGELOG.md).

## Audit baseline and review decisions

The original audit baseline and the decisions on the prior Claude and Gemini reviews are
recorded below, unchanged, because several tickets cite them.

- Backend verification baseline: [`docs/VALIDATION.md`](VALIDATION.md).
- Static checks: Ruff check and format, strict mypy, TypeScript typecheck, and ESLint pass.
- Repository documentation has no broken relative links.
- No obvious committed credentials were found.
- The worktree already contained unrelated changes to `AGENTS.md`, `CLAUDE.md`, `.claude/`,
  and `.gemini/`; this audit did not alter them.

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

- Fees, inventory, and multi-level VWAP were not blockers for 0.1.0. Depth walking and
  fee-aware ledgers have since landed as ARB-032/033; execution, latency, inventory, and
  transfer modeling remain out of scope.
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

Problem: [`normalize_binance_symbol`](../server/arb/adapters/binance.py#L15) maps `BTCUSDT`
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

Problem: [`GET /api/pairs`](../server/arb/api.py#L309) returns `book_manager.known_pairs()`,
which contains only books that have received an event. The dashboard fetches this endpoint
once in [`LiveProvider`](../dashboard/src/state/live.tsx#L133). A dashboard opened before the
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

Problem: [`LiveSpreads`](../dashboard/src/components/LiveSpreads.tsx#L63) records venue
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

Problem: [`.pre-commit-config.yaml`](../.pre-commit-config.yaml#L13) invokes
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

Problem: [`pyproject.toml`](../pyproject.toml#L19) installs `httpx2` for development, but the
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

Problem: [`load_config`](../server/arb/config.py#L41) coerces values but does not validate
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

Problem: [`SnapshotReconciler`](../server/arb/reconcile.py#L39) only logs mismatches. A silently
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

Problem: [`_send_messages`](../server/arb/broadcast.py) caught every `Exception` and silently
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

Problem: [`OrderBookManager.apply`](../server/arb/orderbook.py#L129) accepts snapshots without
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

Problem: [`requestJson`](../dashboard/src/api/client.ts) and the WebSocket handler previously cast
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
floats, but [`opportunity_minutes`](../server/arb/persistence.py#L36) deliberately stores
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

Problem: [`VALIDATION.md`](VALIDATION.md#L106) correctly identifies the missing
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
in [`VALIDATION.md`](VALIDATION.md#live-soak-2026-09-16); the anomalies became
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
[`ARB-022.md`](../artifacts/benchmarks/performance/ARB-022.md) justify no dependency or
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

### [x] ARB-023 — Prepare the first public release and project presentation

- Priority: P2
- Estimate: 3 hours
- Dependencies: all P0 tickets, ARB-016, ARB-017, ARB-018

Original problem: the README was technically strong, but the repository had no release
tag, changelog/release notes, support policy, or concise verified-release checklist.

Initial progress: added an Unreleased changelog with migration and limitation notes, alpha
versioning rules, and a candidate checklist covering both operating systems, packages,
licenses, scans, and artifact provenance. Publication was gated on verified runner
evidence and owner review.
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

Resolution (2026-09-16): the changelog now records `0.1.0` with the soak evidence,
the Node 22 toolchain note, and the reviewed limitations. The release tag `v0.1.0` is
created from the reviewed commit after the clean-checkout checks and hosted CI pass on
it, and pushed only on the owner's explicit request.

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

### [x] ARB-028 — Resynchronize one Binance.US pair without dropping the venue

- Priority: P2
- Estimate: 4 hours
- Dependencies: ARB-010, ARB-021

Problem: the Binance.US adapter carries every configured pair on one combined stream, so
a confirmed reconciliation drift on a single thin pair (`DOT-USDT`, 13:18Z in the
2026-09-16 soak) reconnects the whole socket and reports all nine books `disconnected`
until the rebuild finishes (13.9 s observed). Detection excluded them correctly, but one
pair's drift should not remove a venue from detection.

Resolution: a sequence gap or an externally requested recovery now discards only
the affected pair's sync state and re-fetches that pair over the still-open
socket, reusing the per-pair snapshot-task machinery from initial sync.
`SnapshotReconciler` and `consume_adapter` prefer the new `request_pair_resync`
hook and fall back to a full reconnect for adapters without one; buffer
overflow, snapshot failure, and repeated misalignment keep the full-venue
reconnect as the bounded fallback. Scoped resyncs are counted in
`arb_adapter_pair_resyncs_total` by trigger. Covered by mid-stream,
replay-eligibility, reconciler, and consumer tests in `test_binance.py`,
`test_reconcile.py`, and `test_pipeline.py`; behavior documented in
[`RESYNC.md`](RESYNC.md).

Acceptance criteria:

- A confirmed drift on one Binance.US pair re-fetches and re-aligns that pair's book
  without disconnecting the others, or the design decision not to is recorded in
  [`RESYNC.md`](RESYNC.md) with the measured cost.
- Sequence validation for the untouched pairs is unaffected; tests cover a mid-stream
  single-pair resync while other pairs keep receiving deltas.
- Book eligibility for the other pairs stays `true` across the resync in a replay test.

### [x] ARB-029 — Attribute host connectivity loss in the soak observer

- Priority: P3
- Estimate: 2 hours
- Dependencies: ARB-021 observer tooling

Problem: the 2026-09-16 soak contained a seven-minute window in which every exchange
socket, every REST snapshot, and the observer's own loopback requests failed together.
That is a host outage, but the report can only show correlated failures; the attribution
was made afterwards from the Windows event log.

Resolution: each sample now performs bounded TCP probes independently of backend API
sampling, records probe results without shortening the run, and summarizes intervals where
the backend and host were unreachable together. The committed four-hour soak predates this
instrumentation, so it remains historical evidence rather than a retroactively attributed
probe result.

Acceptance criteria:

- Each sample records a lightweight host-connectivity probe (for example a DNS lookup or
  TCP connect to a stable public endpoint) alongside the backend sample.
- The report distinguishes "backend unreachable while host connectivity was lost" from
  "backend unreachable while the host was online", and summarizes outage windows.
- Probe failures never fail or shorten the run on their own.

P2 total: **29 engineer-hours plus the soak runtime**.

## Executability roadmap (post-0.1.0)

The 0.1.0 fee-survival analysis showed that top-of-book cross-venue spreads exist but none
survives retail fees at the recorded sizes. The project's thesis from here is: **top-of-book
cross-venue spreads exist, but depth, fees, and decay determine whether they are
executable.** Each ticket below adds one term of that model. Capture/replay comes first
because every later feature needs deterministic tests against real exchange traffic, and
the consumer interface between the books and their readers is allowed to stay a plain
callback list until replay shows what shape it needs; a plugin framework is out of scope.

Release status: ARB-030 through ARB-035 are complete on the default branch after the
published `v0.1.0` tag and remain under `Unreleased`; package metadata is still `0.1.0`.
The next reviewed minor candidate can include those changes.

### [x] ARB-030 — Capture and replay real exchange traffic

- Priority: P2
- Estimate: 10 hours
- Dependencies: ARB-021 tooling, 0.1.0 tag

Problem: regression tests run on hand-written synthetic fixtures and live verification
needs the internet. Real protocol quirks (thin-book transitions, resync ordering,
Binance.US depth limits) are never exercised deterministically, and the hosted demo
depends on a free-tier backend that sleeps.

Acceptance criteria:

- `arbsync capture --duration 10m --output PATH` records, per frame: exchange, local
  receive timestamp (wall clock and monotonic), exchange timestamp when the message
  carries one, sequence identifiers when present, and the raw WebSocket text. Output is
  JSONL, optionally zstd-compressed.
- The capture writer is a bounded queue drained off the receive path with a drop counter
  and metric, following `OpportunityStore`; it never blocks ingestion.
- `arbsync replay CAPTURE [--speed N]` feeds frames through the real adapters, books, and
  detector without network access, at recorded or accelerated pacing.
- Replaying the same capture twice produces identical normalized book transitions and
  detector output; a test asserts this by hashing the event stream.
- A committed three-venue capture of a few minutes lives under
  `server/tests/fixtures/captured/`, compressed, with a test enforcing a per-file size
  cap (5 MB); longer captures stay outside Git. SQLite still stores opportunities only.
- The dashboard can be driven from a replayed capture, so a demo needs no live backend.

### [x] ARB-031 — Track opportunities as episodes (schema v3)

- Priority: P2
- Estimate: 8 hours
- Dependencies: ARB-030

Problem: the detector inserts one row per book update while a spread persists. The soak's
246 rows were 108 distinct resting-quote pairs, so counts and profit sums overstate what
existed, and nothing records how long a dislocation lasted.

Acceptance criteria:

- One episode spans appearance to disappearance for a (pair, buy venue, sell venue) and
  records `start_ns`, `end_ns`, peak spread, spread at close, and size at peak.
- Identity and correlation use wall-clock `time.time_ns()`; duration uses monotonic
  deltas, so a system-clock step can never yield a negative or inflated lifetime.
- Schema version 3 is migrated on startup like the quote-currency migration; the pruner
  and statistics rollups are updated and tested against the new shape.
- Lifetime distribution (p50, p90, max) is available from the API and shown in the
  dashboard; `tools/fee_survival.py` reads episodes instead of deduplicating rows.
- Episode boundaries are verified deterministically with an ARB-030 replay test.

### [x] ARB-032 — Depth-aware executable pricing with explicit insufficient depth

- Priority: P2
- Estimate: 8 hours
- Dependencies: ARB-030, ARB-031

Problem: detection compares top-of-book only. "Where is it cheapest to buy $10k" cannot
be answered, and the top-of-book number overstates what a real order would pay.

Acceptance criteria:

- For configured notionals (default `100`, `1000`, `10000`, `50000` in quote units) the
  book is walked to a decimal-exact VWAP per venue and side; results are decimal strings
  on the wire.
- When the subscribed depth cannot fill the notional the result is an explicit
  `insufficient_depth` value, never a fabricated price, and every result carries the
  venue's subscribed depth ceiling (`subscribed_depth_levels`) so fill-rate statistics
  are not compared across venues with different caps (Binance.US snapshots are limited;
  Coinbase and Gemini stream full books).
- Property tests: VWAP is monotonically non-improving in notional, equals top-of-book for
  a notional within the first level, and reports insufficient depth exactly when the
  summed depth is short.
- Fill-rate per venue and notional ("could fill $50k in N% of observations") is exposed as
  a statistic; replay tests cover thin alt books and a mid-resync window.

### [x] ARB-033 — Fee-aware net pricing in the product

- Priority: P2
- Estimate: 5 hours
- Dependencies: ARB-032

Problem: fees live only in the offline `tools/fee_survival.py` scenarios. Users cannot see
net executable spread, and the offline tool's assumptions are not the product's.

Acceptance criteria:

- `config.toml` gains a per-venue fee schedule (taker first; maker optional and unused
  until a use exists), validated at startup like other settings.
- Every episode and executable-price result carries the ledger
  `top-of-book spread -> depth impact -> fees -> net executable spread`, without a separate
  "expected slippage" term, because depth impact is measured by ARB-032.
- Net values are decimal strings on the wire and in SQLite; dashboard statistics may
  remain `REAL`.
- The soak-style survival report is reproducible from stored net values, and
  `tools/fee_survival.py` either reads them or is retired with its documentation updated.

### [x] ARB-034 — Venue-comparison dashboard view

- Priority: P2
- Estimate: 8 hours
- Dependencies: ARB-032, ARB-033

Problem: the dashboard answers "was an arbitrage detected", which the fee analysis shows
is rarely the useful question.

Acceptance criteria:

- Per pair: each venue's executable buy and sell price at the selected notional, the
  cheapest venue to buy and best to sell, available depth or `insufficient_depth`, gross
  spread, net spread after fees, and the current or last episode lifetime.
- No "price leader" or lead/lag cell in the live view: single-vantage measurements cannot
  separate market leadership from network path and exchange clock differences.
- Existing eligibility, freshness, and connectivity panels remain; boundary validation
  covers every new REST and live message shape; coverage gates hold.

Resolution: reused the existing depth-pricing endpoint and fee-aware route ledgers,
added strict dashboard payload validation and five-second refreshes, and added a
selected-notional venue comparison panel. The panel shows per-venue executable VWAPs,
filled base depth, cheapest-buy and best-sell venues, gross and net route spreads, and
current or most recent episode lifetime. Missing quotes are shown as unavailable while
short books remain explicitly insufficient depth. Dashboard tests, typecheck, lint, and
production build pass; the focused backend API, pricing, and detector tests pass.

### [x] ARB-035 — Offline lead/lag and market-structure research

- Priority: P3
- Estimate: 6 hours to first module
- Dependencies: ARB-030, ARB-031

Problem: captures make cross-venue questions answerable, but naive "who moved first"
comparisons from one machine are contaminated by differential network latency and
exchange clock offsets.

Acceptance criteria:

- Research runs over captures offline, never on the ingestion path, and writes datasets
  as files (JSONL or Parquet), not SQLite tables.
- First modules: episode lifetime distribution, fee-survival and executable-size survival
  rates, and per-venue fill rate by notional (liquidity fragmentation).
- Lead/lag uses an estimator designed for asynchronous ticks (Hayashi–Yoshida or
  Hoffmann–Rosenbaum–Yoshida), reports confidence intervals, and documents the
  measurement floor from network geography and clock skew; results are labelled as
  research, not product metrics.

Resolution: added the offline `tools/research.py` capture replay workflow with JSONL
datasets for canonical episode lifetimes, fee and executable-size survival by notional,
venue fill rates, and Hayashi–Yoshida lead/lag estimates. Replay uses production
adapters, trusted books, the episode detector, depth sampler, and exact fee-aware
ledgers; it does not write SQLite or run on ingestion. Added measurement-floor and
clock-skew caveats in `docs/RESEARCH.md`, deterministic replay timestamps, and focused
research tests. Backend tests, Ruff, formatting, and strict mypy pass.

Original executability estimate: **39 engineer-hours** through ARB-034, now complete.
ARB-035 added a separate 6-hour first research module. The audit after ARB-035 found that
the research inputs and replay lifecycle need further correctness work before their output
can support stronger market-structure or executability conclusions.

## Research correctness

### [x] ARB-036 — Build research series from canonical post-apply observations

- Priority: P1
- Estimate: 8–12 hours
- Dependencies: ARB-035

Problem: `ReplayTransition.bids` and `asks` contain the levels changed by the normalized
input event, not the canonical best bid and ask after applying it. ARB-035 treats the first
changed bid and ask as a midpoint and discards one-sided updates. A deep-level update can
therefore move the research price while the real top of book is unchanged, and a genuine
best-price change can be omitted.

Acceptance criteria:

- Add a versioned replay/research observation representing the canonical state after an
  event is applied: exchange, pair, canonical best bid and ask, local wall and monotonic
  observation times, sequence, and whether the book is eligible.
- Produce no valid price tick from an incomplete, crossed, disconnected, discontinuous, or
  expired book. Preserve the normalized input delta separately where protocol auditing and
  the replay digest still need it.
- Make the ARB-035 price-series builder consume canonical observations only; it must never
  infer a midpoint from the first changed levels in an event.
- Cover a one-sided best-price update, a deep-only update that leaves the midpoint unchanged,
  deletion of the current best level, a snapshot, and an invalidation boundary.
- Keep the work offline/replay-facing. Do not add a database migration or change the live
  opportunity model in this ticket.
- Document that existing ARB-035 lead/lag output produced before this correction is not
  valid evidence, while leaving estimator validation to ARB-039.

Resolution: replay now emits version-1 canonical post-apply observations alongside the
normalized input transitions. Observations carry the exchange, pair, exact canonical best
prices, recorded wall and monotonic times, local sequence, and eligibility. The research
price-series builder consumes only eligible observations, so deep-only updates, one-sided
best-price changes, best-level deletion, snapshots, and reset boundaries cannot fabricate or
omit a midpoint. Existing ARB-035 lead/lag output must be regenerated; estimator validation
remains deferred to ARB-039.

### [x] ARB-037 — Make capture integrity and lifecycle provenance self-describing

- Priority: P1
- Estimate: 12–20 hours
- Dependencies: ARB-036 observation contract

Problem: a footer marked `clean` proves an orderly writer close but does not prove the
capture is complete. Queue-full and closed-writer drops exist only in process metrics.
Capture frames also do not identify connection boundaries or whether a REST response came
from initial synchronization, scoped recovery, full reconnect recovery, or reconciliation.

Acceptance criteria:

- Version the capture format and record attempted, accepted, flushed, and dropped frame
  counts by reason and kind in the artifact. A reader can distinguish clean shutdown from
  lossless capture without consulting external metrics.
- Record adapter connection/disconnection boundaries and enough recovery context to replay
  when a canonical book became invalid and when a new connection generation began.
- Correlate each recorded REST snapshot response with its request purpose, pair, connection
  generation, request time, and response time. Do not match unrelated responses solely by
  exchange and URL.
- Treat writer failure, a missing footer, count mismatch, and declared frame loss as distinct
  outcomes. Research rejects lossy input by default and requires an explicit override that
  is written to its report metadata.
- Define and test backward compatibility for version-1 captures. If some provenance cannot
  be reconstructed, label that limitation instead of inventing it.
- Preserve bounded, non-blocking ingestion and the existing observable drop policy.

Resolution: capture format version 2 records lifecycle connection boundaries, per-kind
attempted/accepted/flushed counts, dropped frames by reason and kind, writer-failure status,
and request-correlated REST snapshot provenance. Adapter request context identifies initial,
sequence-gap, scoped-recovery, full-reconnect, and reconciliation snapshots without relying
on exchange and URL alone. The reader distinguishes missing footers, writer failures, count
mismatches, and declared frame loss; research rejects lossy captures by default and records
an explicit `--allow-lossy` override. Version-1 captures remain readable with legacy
provenance limitations labelled.

### [x] ARB-038 — Replay recovery, disconnects, expiry, and snapshots on recorded time

- Priority: P1
- Estimate: 20–32 hours
- Dependencies: ARB-036, ARB-037

Problem: replay preloads REST responses and returns them immediately, so a snapshot recorded
later can affect earlier WebSocket frames. It calls adapter parsers sequentially rather than
reproducing Binance.US's concurrent buffering while a snapshot request is in flight. Replay
also omits live connection-state callbacks and the eligibility monitor, so gaps,
disconnects, and age expiry can leave episodes open until capture shutdown.

Acceptance criteria:

- Drive WebSocket frames, REST request completion, connection boundaries, and eligibility
  deadlines with one deterministic scheduler on the recorded monotonic timeline.
- A snapshot response cannot affect canonical state before its recorded completion time.
  WebSocket frames received while it was in flight remain buffered and are aligned by the
  production adapter's recovery rules.
- Replay a disconnect or full reconnect through the same canonical invalidation effects as
  live operation. A scoped Binance.US recovery invalidates only its affected pair.
- Advance age-based eligibility even across quiet periods and close an affected episode at
  the deterministic expiry boundary rather than only when another market frame arrives.
- Add regression tests for future-snapshot lookahead, interleaved Binance.US updates,
  reconnect generations, sequence-gap closure, quiet-book expiry, scoped recovery, and
  missing or mismatched snapshot provenance.
- Keep recorded replay deterministic and make its digest cover the canonical transitions
  and lifecycle boundaries whose equality is being claimed.

Resolution: replay now drives connection boundaries, WebSocket input, correlated REST
snapshot completion, book-expiry deadlines, and depth samples through one recorded-time
priority queue. Binance.US exposes the same sans-I/O buffering and snapshot-alignment state
machine to live streaming and replay, including pair-scoped recovery and bounded retries.
Disconnects invalidate canonical books, quiet books expire at their exact age boundary, and
version-2 snapshot provenance is matched by pair, purpose, and connection generation;
legacy captures retain explicitly labelled URL-order timing. The versioned lifecycle trace
and canonical transitions are covered by the deterministic digest and focused regression
tests.

### [x] ARB-039 — Validate the lead/lag estimator and uncertainty reporting

- Priority: P1
- Estimate: 12–20 hours
- Dependencies: ARB-036, ARB-038

Problem: correcting the input price series does not establish that the estimator is valid.
The current overlap normalization can produce values outside `[-1, 1]`, zero-overlap inputs
can be reported as `status="ok"`, and the approximate Fisher interval does not account for
dependent overlaps or selection of the maximum across a lag grid.

Acceptance criteria:

- Define the exact estimator and normalization from a cited primary reference or a trusted
  implementation, including how asynchronous intervals enter its variance terms.
- Never report a correlation outside `[-1, 1]`; zero overlap, constant returns, too few
  observations, and an unidentifiable maximum return explicit insufficient-data statuses.
- Test planted positive, negative, and zero lags; irregular sampling; missing ticks; unequal
  activity; constant series; no overlap; and a null simulation with no leader.
- Use an uncertainty method appropriate to dependent asynchronous returns and lag-grid
  selection, or remove confidence bounds and state precisely what evidence remains.
- Report sensitivity to tick bin, lag step, window, and minimum overlap. Keep the result
  labelled research rather than a live product metric.

Resolution: the estimator moved to `tools/lead_lag.py` with the contrast, `argmax |U(θ)|`
lag selection, and realized-variance normalization cited to Hayashi–Yoshida 2005,
Hoffmann–Rosenbaum–Yoshida 2013, and Huth–Abergel 2014. The grid is symmetric, zero
overlap and sub-`min_overlap` lags never enter selection, and rows report explicit
`insufficient_data` and `not_identifiable` reasons. Validation showed that the normalized
contrast straddles one for near-perfectly correlated asynchronous series even at the
correct lag, so an out-of-range value nulls the bounded correlation and exposes the raw
ratio instead of vetoing the lag. The Fisher interval was removed; window stability and
a circularly shifted surrogate null with a permutation p-value replace it, calibrated on
30 seeded independent walks (29 retained). Planted ±/zero lags, dropout, unequal
activity, noise, constant, disjoint, too-few, tied, grid-edge, and null cases are tested,
and a Hypothesis property pins the overlap sweep to brute force. A one-at-a-time
sensitivity dataset is written per run; the committed 150-second capture analyses in
about 27 seconds.

### [x] ARB-040 — Measure absolute age and cross-venue receipt skew before gating routes

- Priority: P1
- Estimate: 12–20 hours
- Dependencies: ARB-036, ARB-038

Problem: canonical eligibility limits each book's age independently, but a route can compare
a just-updated book with another book near the configured age limit. Relative receipt skew
is useful evidence of asynchronous inputs, but it does not prove the quieter book is wrong,
and equal-age books can both be too old. A guessed hard cutoff would encode policy before
the data establishes one.

Resolution (2026-09-20): `TopOfBook` carries the local receipt time, and the detector
reports each compared route's leg ages and absolute skew through an observer at
evaluation (research only), open, peak, and close, without gating. Research writes
`route_leg_ages.jsonl`, `age_skew_bands.jsonl`, and `age_skew_gate_sensitivity.jsonl`
on two separate dimensions with configurable band edges; live, two histograms record
leg ages and skew at episode open and `/api/pricing/depth` routes carry
`buy_age_ms`/`sell_age_ms`/`age_skew_ms`. A lossless 45-minute capture yielded 132
episodes, zero fee survivors at every notional, no leg older than 5 s at open, and no
trend in open rate with age or skew, so the pre-stated rule for a default gate was not
met and none is adopted; evidence is in `artifacts/research/arb-040/`. Resolution is
bounded by the ~15.6 ms Windows monotonic tick, and at open the older leg's age equals
the skew by construction.

Acceptance criteria:

- Carry each route leg's monotonic receipt age and the absolute age difference into offline
  observations and diagnostics without using exchange clocks as a freshness authority.
- Analyze opportunity counts, spreads, lifetimes, and fee/depth survival across configurable
  absolute-age and relative-skew bands on faithful captures.
- Keep absolute age, relative skew, connection state, and sequence continuity as distinct
  dimensions in output and documentation.
- Add metrics or API fields needed to observe the same values live without changing route
  eligibility by default.
- Propose a default route gate only if the captured sensitivity results support one. If a
  gate is adopted, make it configurable, close affected episodes deterministically, expose
  the rejection reason, and add boundary tests.

### [x] ARB-041 — Analyze net-executable intervals offline before adding live episodes

- Priority: P2
- Estimate: 16–24 hours
- Dependencies: ARB-038, ARB-040

Problem: current episodes intentionally track theoretical top-of-book dislocations. Their
pricing ledger is refreshed only at open or at a wider theoretical peak, so it cannot answer
how long a configured notional stayed executable and net positive or whether depth improved
without a new theoretical peak. That limitation does not by itself justify a second live
episode lifecycle and schema.

Resolution (2026-09-21): replay exposes an offline `book_observer` hook, and
`tools/net_intervals.py` re-prices every directed route through the updated venue from
matched depth and explicit taker fees on each canonical book change, keeping gross and
fills so fee variants re-net without a second replay, then folds the change-only signal
into intervals with explicit open, spread-closed, insufficient-depth, invalidated,
end-of-capture, hysteresis, and delay semantics. Research writes `net_intervals.jsonl`
and `net_interval_sensitivity.jsonl` (report `version` 4) with theoretical-episode
overlap, and `tools/perf_net_intervals.py` measures the cost. The 45-minute capture
produced zero net-positive intervals at every notional under configured fees, halved
fees, a 0.1 % threshold, and every delay (best net −0.68 % against best gross +0.32 %);
at zero fees the same signal holds 13,062 sub-basis-point gross-positive stretches with
a 0.5 s median, barely overlapping theoretical episodes. Exact per-event pricing costs
0.4–0.75 CPU-seconds per market second and ~600 bytes per retained row, about 30× the
periodic sampler. Decision: retain offline intervals, do not add live net episodes, no
SQLite migration; evidence is in `artifacts/research/arb-041/`.

Acceptance criteria:

- On faithful replay, compute per-notional route intervals from matched depth and configured
  fees whenever a relevant canonical book change can alter the result.
- Define opening, closing, insufficient-depth, invalidation, hysteresis, and end-of-capture
  semantics explicitly. Keep theoretical and net-executable intervals as different datasets.
- Report duration, peak and terminal net spread, executable base and quote amounts, depth
  insufficiency, leg ages/skew, and sensitivity to threshold and assumed delay.
- Measure CPU and memory cost at representative book depths and event rates before proposing
  equivalent live computation.
- End with a recorded product decision: add live net episodes, retain offline intervals, or
  collect more evidence. Do not migrate SQLite or change live episode semantics unless that
  decision approves a separately scoped follow-up.

### [x] ARB-044 — Keep venue-comparison route economics coherent with eligibility

- Priority: P1
- Estimate: 8–12 hours
- Dependencies: ARB-034

Problem: individual venue cells consult canonical book status, but the selected gross/net
route is chosen from the last polled pricing response without applying those statuses. The
panel can therefore show a route's economics after one leg has become ineligible. Its
independent cheapest-buy and best-sell labels can also describe different venues from the
route whose gross and net spread is displayed.

Acceptance criteria:

- Suppress a cached route immediately when either route leg becomes ineligible or the live
  feed is disconnected; distinguish unavailable from insufficient depth.
- Label the buy and sell venues for the exact displayed route. If independent per-side best
  quotes remain useful, present them separately without implying they produced that route.
- Carry an as-of marker or generation through pricing refreshes so an older response cannot
  revive a route after a newer invalidation or refresh.
- Test disconnect, age expiry, crossed/incomplete status, out-of-order pricing responses,
  same-venue independent best quotes, route recovery, and a route that differs from the
  independent cheapest-buy/best-sell pair.

The original evidence criterion (refresh architecture, research, changelog, and validation
claims and run a qualifying current-code soak) was split into ARB-045 because it spans
ARB-036 through ARB-044 rather than this dashboard fix.

### [x] ARB-043 — Add filtered historical opportunity queries and export

- Priority: P2
- Estimate: 12–20 hours
- Dependencies: existing schema version 4; net-specific filters wait for an approved ARB-041 follow-up

Problem: the API exposes only the latest bounded list. Existing canonical episode history
cannot be paged or filtered for ordinary investigation without reading SQLite directly.

Acceptance criteria:

- Add stable cursor pagination with explicit ordering and filters for time range, pair,
  buy venue, sell venue, close reason, and open/closed state.
- Add a bounded JSONL or CSV export path that preserves decimal strings and quote units and
  cannot monopolize ingestion or hold an unbounded result in memory.
- Validate query limits, cursor stability across concurrent inserts, malformed filters, and
  index-supported plans on a representative database.
- Add dashboard drill-down only after the API contract is tested. Do not imply net-executable
  filtering until that lifecycle exists or an explicit stored-ledger filter is defined.

### [x] ARB-042 — Make fill-rate statistics complete, windowed, and reproducible

- Priority: P2
- Estimate: 12–20 hours
- Dependencies: ARB-037, ARB-038

Problem: live fill counts exist only for the current process, and sampling iterates books
that have already been seen. A configured book that never initializes can be absent rather
than counted as unavailable, and the API has no time window or sample provenance.

Acceptance criteria:

- Sample the configured roster, including missing and never-initialized books, and preserve
  ineligible observations separately from insufficient depth.
- Define sample start/end, cadence, restart, missed-sample, depth-cap, and configuration
  semantics. Expose raw counts alongside ratios.
- Produce reproducible windowed aggregates by venue, pair, side, and notional from a bounded
  persisted or file-backed representation chosen explicitly for this use case.
- Verify restart behavior, quiet and missing books, configuration changes, capped versus
  full-depth venues, and replay/live agreement on the same observation sequence.
- Do not make this ticket depend on live net episodes.

Resolution (2026-09-22): `arb.fillrates` defines the per-session sample grid, missed-tick
accounting, per-reason ineligible counts, depth-cap shortfalls, and configuration
fingerprints. Counts persist as one-minute buckets in SQLite schema version 5 through the
bounded store queue, and `/api/pricing/fill-rates` sums them over whole-minute windows
grouped by fingerprint with session and coverage provenance. Research writes the same
buckets to `venue_fill_rate_minutes.jsonl`. `server/tests/test_fillrates.py` covers
restarts, quiet and missing books, configuration changes, capped versus full-depth venues,
pruning, the v4 migration, and replay/live agreement on one observation sequence; a live
smoke confirmed buckets and a window across a restart.

### [x] ARB-045 — Refresh research and validation claims and run a current-code soak

- Priority: P1
- Estimate: 4–8 hours plus soak runtime
- Dependencies: ARB-036 through ARB-040, ARB-044

Problem: the published four-hour soak predates the schema-v4 depth/fee ledgers, Binance.US
per-pair resynchronization (ARB-028), direct host-connectivity probes (ARB-029), and the
research-correctness contracts of ARB-036 through ARB-040. This criterion was split out of
ARB-044 so the completed dashboard fix is not held open by cross-cutting evidence work.

Acceptance criteria:

- After ARB-036 through ARB-040 settle their contracts, refresh the relevant architecture,
  research, changelog, and validation claims and run a qualifying current-code soak. Keep
  that evidence work out of feature implementation commits when it is independently scoped.

Resolution (2026-09-22): a four-hour uninterrupted soak of commit `012537c` (clean
checkout, launched under a Windows Scheduled Task) completed `14400.6 s` with 241/241
ready samples, zero sequence gaps, restarts, background or HTTP failures, every configured
book eligible in every sample, the host probe online throughout, two Binance.US scoped pair
resyncs with no Binance.US reconnect, and 2,868 fill-rate samples with none missed.
Stored schema-v4 ledgers show no net-positive route at any notional across 497 episodes.
Report: `artifacts/benchmarks/soak/soak_4h_2026-09-22_153534.md`; evidence and remaining
gaps are in `docs/VALIDATION.md`. ARCHITECTURE, CHANGELOG, README, BENCHMARKS, and STORAGE
claims were refreshed; RESEARCH.md was refreshed with ARB-042. The six Gemini venue
reconnects that each followed a confirmed `DOT-USD` or `LTC-USD` price drift are the
designed fallback, not a failure; recurring Gemini drift and whole-venue recovery are
ticketed as ARB-046.

### [x] ARB-046 — Investigate recurring Gemini price drift and scope its recovery

- Priority: P2
- Estimate: 6–12 hours
- Dependencies: ARB-028, ARB-045

Problem: the 2026-09-22 soak confirmed six Gemini price drifts in four hours (`DOT-USD`
four times, `LTC-USD` twice), each within the first 90 minutes. Gemini has no scoped
resync, so every confirmation reconnected the whole venue and made its other eight books
briefly ineligible (2.0–2.7 s each); the reconnect metric labels these only
`reason="RuntimeError"`.

Acceptance criteria:

- Determine from captured traffic whether the drifts are genuine stream divergence, REST
  snapshot staleness, or a normalization defect, and record the evidence.
- If Gemini's protocol allows it, recover a single pair without reconnecting the venue, or
  document why it cannot; keep recovery adapter-owned and fail-closed.
- Label adapter reconnects by their cause (for example confirmed drift versus transport
  error) instead of the exception class.

Resolution (2026-09-22): the drifts are genuine divergence of Gemini's incremental `@depth`
stream from Gemini's own subscription snapshots, not REST staleness or a normalization
defect. `tools/gemini_drift.py` compares each incremental book with the fresh WebSocket
snapshot that replaces it: over 81 forced single-pair rebuilds with a 0.1 s gap, all nine
pairs had levels the stream had removed or never announced more than 30 s earlier (115 in
total), while every frame continued its `U/u` chain and none repeated a price. DOT-USD and
LTC-USD confirm because one missing level in a sparse book exceeds the reconciler's 0.5 %
per-index threshold. A live probe showed `UNSUBSCRIBE` then `SUBSCRIBE` on an open socket
yields a full snapshot while other pairs keep streaming, so the Gemini adapter now
recovers a sequence gap or confirmed drift by resubscribing only that pair, driven by
self-describing request ids that replay reads from the recorded acknowledgements; a
rejected step, no snapshot within 10 s, or more than three resyncs of a pair per minute
falls back to a full reconnect. `arb_adapter_reconnects_total` now labels reconnects by
cause (`confirmed_drift`, `sequence_gap`, `invalid_book`, `transport_error`, ...). A 90-minute
live capture confirmed no drift, so the reconciler-triggered path is proven by tests and
81 forced live resyncs rather than a natural drift; that gap is recorded in
`docs/VALIDATION.md`. Evidence and protocol details are in `docs/RESYNC.md`.

Correction (ARB-047, 2026-09-23): the diagnosis above is wrong. An exact comparison at the
same update id showed the stream-built books match Gemini's own top-20 snapshots, and the
disagreeing REST levels never traded; the drifts are stale levels in Gemini's REST book.
The per-pair resubscription and cause-labelled reconnects stand.

### [x] ARB-047 — Measure Gemini top-of-book error and its effect on stored research

- Priority: P1
- Estimate: 4–8 hours
- Dependencies: ARB-046

Problem: `tools/gemini_drift.py` counts divergent levels but not where they sit or how long
they last. A read-only pass over the 2026-09-22 forced-resync capture found stale missing
levels at rank 0 on 14 of 162 book sides, rank 1 on 14, and rank 2 on 12. Detection,
spreads, depth-walked pricing, and fill rates all read those levels while the book is
eligible, so the effect on stored and replayed research is unknown.

Acceptance criteria:

- Report divergence by rank (best price, top 5, within each configured notional's depth
  walk) and by how long each stale level persisted, per pair.
- Decide from Gemini trade prints, not REST, whether the incremental book or the snapshot
  is correct when they disagree, and record the evidence. Probe the live trade stream first.
- Measure the share of Gemini-leg episodes (stored and replayed from the 2026-09-20
  capture) that rested on a level the next Gemini snapshot contradicts; record it in
  `docs/VALIDATION.md` and caveat Gemini-leg research claims if it is material.

Resolution (2026-09-23): measured exactly, the premise was wrong. Gemini's `@depth20`
stream carries top-20 snapshots whose `lastUpdateId` shares the `@depth` id space, so
`tools/gemini_audit_capture.py` recorded a 45-minute pipeline run with that stream and
`@trade`, and `tools/gemini_book_audit.py` compared each snapshot with the incremental book
at exactly the same update id. All 21,797 aligned comparisons matched in price and size
across all nine pairs, so there was no divergence to break down by rank or duration; levels
deeper than the top 20 have no exact reference. Trades adjudicated the REST disagreement:
all 160 trades better than the book's best printed at prices the stream showed within 2 s,
while the 112 better prices that only REST showed, 99 of them already deleted by the
stream, never traded within 60 s (the stream book's best traded in 2.9 % of samples). Of 38
replayed Gemini-leg episodes, 37 were confirmed by the aligned snapshot and none was
phantom. The episodes were replayed from the new capture rather than stored rows or the
2026-09-20 capture, which lack `@depth20`; Binance.US's first 40 s were excluded because
replay cannot reproduce a failed REST fetch (ticketed as ARB-051). ARB-046's
stream-divergence conclusion came from comparing books a one-second batch apart;
`tools/gemini_drift.py` now reports never-announced prices separately. Evidence is in
`docs/VALIDATION.md` and `docs/RESYNC.md`; ARB-048 was rescoped to exact verification
replacing REST-driven Gemini recovery.
