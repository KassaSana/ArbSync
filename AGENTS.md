# ArbSync repository guidance

ArbSync ingests public Gemini, Coinbase, and Binance.US order books, maintains
trusted in-memory L2 books, detects theoretical cross-exchange opportunities, stores
them in SQLite, and streams state to a React dashboard.

This file is the model- and tool-neutral source of instructions for coding agents
working in this repository. `CLAUDE.md` imports it; Codex and Cursor read it directly;
Gemini CLI is pointed at it by `.gemini/settings.json`. Edit this file, not the shims.
Describe required outcomes and repository constraints here without pinning model names,
versions, or product-specific capabilities.

## Layout

```text
server/arb/          Backend package
server/arb/adapters/ Exchange-specific protocol and recovery logic
server/tests/        Backend tests
dashboard/src/       React/TypeScript dashboard
tools/                Benchmark, replay, profiling, and soak tools
artifacts/benchmarks/ Benchmark results and live-run artifacts
docs/RESYNC.md       Recovery design decision
docs/VALIDATION.md   Current verification status and remaining evidence
docs/BENCHMARKS.md   Benchmark and soak methodology
config.toml          Runtime configuration
var/                 Ignored runtime data
```

## Architectural invariants

- Adapters own exchange-specific sequence validation and recovery.
- `OrderBookManager` owns the canonical eligibility decision shared by detection,
  readiness, metrics, and dashboard state.
- A disconnected, uninitialized, discontinuous, stale, incomplete, or crossed book
  must not contribute to detection.
- Persistence and per-client WebSocket delivery remain bounded and must not block
  market-data ingestion.
- SQLite stores opportunities, not order books.
- Canonical prices, sizes, spreads, profits, persisted opportunity rows, and wire values
  remain decimal-exact strings. Derived minute rollups and dashboard statistics use SQLite
  `REAL`/binary64 and are approximate observability summaries, not accounting values.
- Opportunities are theoretical and exclude fees, slippage, latency, inventory, and
  execution risk.

See [`docs/RESYNC.md`](docs/RESYNC.md) before changing adapter recovery or normalized
sequence behavior.

## Run and verify

From the repository root:

```powershell
uv run pytest -q server/tests
uv run mypy --strict server/arb
uv run ruff check server tools
uv run ruff format --check server tools
uv run python -m arb.main
```

From `dashboard/`:

```bash
npm run typecheck
npm run lint
npm run test
npm run build
npm run dev
```

Use small batch sizes and short flush intervals in tests. Tests that open SQLite more
than once should use a file under pytest's `tmp_path`, not `:memory:`.

Update [`docs/VALIDATION.md`](docs/VALIDATION.md) only when verification evidence or
the remaining validation gap materially changes.

## Ticket workflow

- For each ticket, review its dependencies and acceptance criteria, inspect the existing
  implementation and worktree, and decide on a scoped approach before editing.
- Implement the ticket and add or update tests whenever behavior changes. Documentation-
  only or metadata-only work does not require artificial tests; run the relevant static,
  packaging, link, or content checks instead.
- Run verification proportionate to the change and review the final diff before declaring
  the ticket complete. Keep unrelated user changes out of the ticket commit.
- When a ticket in [`todolist.md`](todolist.md) is complete, update its checkbox from `[ ]`
  to `[x]` in the same commit without waiting for a separate request. If work is partial
  or blocked, leave it unchecked and record the remaining gap where appropriate.
- After verification, create a focused local commit with an accurate imperative message
  and verify its complete author, committer, and message.
- Never push automatically. Push only when the owner explicitly requests it after reviewing
  the local result.

## Explanatory collaboration

- Use a teaching-oriented style so the owner can learn while work progresses. Before
  implementation, explain the problem, intended approach, and meaningful tradeoffs in
  plain language.
- During implementation, call out important repository patterns, architectural constraints,
  and findings that materially affect the solution. Define unfamiliar technical terms when
  they first matter, without turning routine steps into noise.
- When recommending a different approach, say so directly and explain the evidence, risks,
  and practical consequences. Do not assume the owner's initial approach must be accepted.
- In the final handoff, explain what changed, why the solution works, what verification
  passed, any limitations or follow-up work, and the main engineering lessons from the
  ticket. Keep explanations concrete and proportionate to the change.

## Commit authorship

- When a coding agent creates a commit on the owner's behalf, use the repository owner's
  configured Git identity as both author and committer. If that identity is unavailable or
  ambiguous, ask before committing; never invent an identity.
- Preserve truthful author and co-author credit for real human contributors. Do not rewrite
  a human contributor's authorship to the repository owner.
- Never identify Claude, Codex, Gemini, Cursor, Copilot, or any other AI/coding agent as an
  author, committer, co-author, contributor, or commit-signature identity. Never add
  generated-by, assisted-by, or similar AI-tool attribution to commits or contribution
  credits.
- Verify the author, committer, and complete commit message after every commit.
- Apply these rules to every future commit in this repository unless the owner explicitly
  changes them.
