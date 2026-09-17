# ArbSync repository guidance

ArbSync is a cross-exchange market-data and execution-quality research platform. It
ingests public Gemini, Coinbase, and Binance.US order books, maintains trusted in-memory
L2 books, analyzes cross-venue opportunities (episodes, depth-walked pricing), persists
research data in SQLite, streams state to a React dashboard, and supports capture/replay
of real exchange traffic for offline work.

This file is the shared, tool-neutral instruction source: `CLAUDE.md` imports it, Codex
and Cursor read it directly, and `.gemini/settings.json` points at it. Edit this file,
not the shims, and do not pin model names or product-specific capabilities here.

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
- Preserve the documented semantics of each opportunity and pricing field. Top-of-book
  `spread_pct`/`theoretical_profit`, depth-walked executable pricing, and fee-adjusted net
  values are distinct tiers; never silently present one as another, and keep fee, slippage,
  latency, and inventory assumptions explicit wherever they enter.

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
npm run test:coverage
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
- Add or update tests whenever behavior changes. Documentation- or metadata-only work
  needs the relevant static, packaging, link, or content checks instead of artificial tests.
- Passing tests are evidence, not proof. When behavior depends on an external protocol or a
  documented invariant, verify boundary behavior against that specification rather than
  trusting existing tests.
- Run verification proportionate to the change and review the final diff before declaring
  the ticket complete. Keep unrelated user changes out of the ticket commit.
- When a ticket in [`todolist.md`](todolist.md) is complete, update its checkbox from `[ ]`
  to `[x]` in the same commit. If work is partial or blocked, leave it unchecked and record
  the remaining gap.

## Collaboration

- Explain important design decisions, invariants, unexpected findings, and tradeoffs in
  plain language. When recommending a different approach, say so directly with the evidence.
- Keep routine implementation narration concise. In the final handoff, summarize what
  changed, verification performed, limitations, and important engineering lessons.

## Git

- Create focused local commits with imperative messages when ticket work is complete and
  verified. Never push unless the owner explicitly requests it.
- Use the repository owner's configured Git identity as author and committer. If it is
  unavailable or ambiguous, ask before committing. Never invent an identity or replace a
  human contributor's.
- Never add AI-tool authorship, co-authorship, contribution credit, generated-by/assisted-by
  attribution, or commit-signature identity.
- After committing, verify the complete author, committer, and commit message.
