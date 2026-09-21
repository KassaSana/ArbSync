# Contributing to ArbSync

ArbSync welcomes focused fixes, protocol evidence, tests, and documentation. It uses
public market data to detect theoretical opportunities; it does not execute trades.
ArbSync models visible-book depth impact and configured taker fees for research pricing.
Latency, inventory management, transfers, order placement, and execution remain outside
the current scope.
Discuss substantial features in an issue before investing in an implementation.

## Set up and verify

Follow the [README setup instructions](README.md#run-locally) for prerequisites,
locked dependency installation, and running the backend and dashboard. Use `uv sync
--locked --extra dev` at the repository root and `npm ci` in `dashboard/`. No exchange
API keys are needed. Keep credentials, local databases, and raw logs out of commits.

Before submitting a change, run the backend commands from the repository root:

```text
uv run pytest -q server/tests
uv run mypy --strict
uv run ruff check server tools
uv run ruff format --check server tools
```

Run the dashboard checks from `dashboard/`:

```text
npm run typecheck
npm run lint
npm run test
npm run build
```

These commands work in PowerShell and Unix shells. Optional local hooks are installed
with `uv run pre-commit install` from the repository root. For documentation-only
changes, check links, commands, and formatting; artificial behavior tests are unnecessary.
State which checks you ran and explain any that you could not run.

## Propose and review a change

1. Search existing issues and the [backlog](todolist.md), then describe the problem and
   intended scope. Use the protocol-correctness template for feed or recovery defects.
2. Create a branch in your fork and make a focused change. Include a regression test
   when behavior changes and update documentation when user-visible behavior changes.
3. Open a pull request describing the trigger, resulting behavior, and verification.
   Include sanitized reproduction steps or fixture provenance where relevant.
4. Address review feedback. Maintainers review correctness, scope, and verification
   before merging; this volunteer project does not promise a review deadline.

Read the [architecture](docs/ARCHITECTURE.md) before changing data flow and the
[recovery decision](docs/RESYNC.md) before changing exchange recovery. Repository
implementation constraints and ticket conventions live in [AGENTS.md](AGENTS.md).
Live exchange access is not required for routine tests. Clearly distinguish synthetic
fixtures from captured traffic and short smoke tests from long-duration evidence.

Submit contributions under the project's [Apache License 2.0](LICENSE), with truthful
human authorship and credit. Follow the [code of conduct](CODE_OF_CONDUCT.md).
Report vulnerabilities through the [security policy](SECURITY.md), not a public issue.
