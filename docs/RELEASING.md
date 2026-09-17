# Release process

ArbSync is an alpha observability project. Version `0.1.0` was published from the reviewed
`v0.1.0` tag on 2026-09-17. The default branch has additional unreleased capture/replay,
episode, depth-pricing, and fee-ledger work while package metadata remains `0.1.0`; package
metadata alone never identifies which commit or changes were published. Only a reviewed
tag and its release notes identify a release.

## Versioning and notes

- Keep the package version in `pyproject.toml` authoritative for the backend. Update
  `uv.lock` with it. Dashboard package metadata identifies its private build, not an
  independently supported public package.
- Use `0.MINOR.PATCH` during alpha: increment PATCH for compatible fixes and MINOR
  for new behavior or incompatible API/configuration/storage changes. Describe every
  incompatibility and migration explicitly; alpha does not imply silent breakage.
- Keep user-facing changes in `CHANGELOG.md` under `Unreleased`. Before publication,
  move reviewed entries under the actual version and UTC release date, then start
  a new `Unreleased` section. Never predate a release or reuse a published tag.
- Tags use `v` followed by the package version. Build from the exact reviewed commit;
  include that full commit ID, artifact SHA-256 hashes, and verification links in the
  release notes. Publish only after the owner reviews the local result and requests it.
- Support follows [SECURITY.md](../SECURITY.md): the current default branch receives
  fixes, with no separate older-release backport promise or response-time guarantee.

Release notes should tell a new user what changed, the configuration or migration
steps they must take, what was verified, and the remaining limitations. Retain the
detection-only scope and the explicit USD/USDT distinction.

## Candidate checklist

Use a fresh checkout of the candidate commit. Record the OS, Python, Node, uv and npm
versions, full commit ID, commands, outcomes, and links to hosted runs. Local checks
cannot substitute for a Unix-like runner when only Windows has been exercised.

- [ ] Every P0 dependency is complete, and ARB-016, ARB-017 and ARB-018 remain satisfied.
- [ ] Review `docs/VALIDATION.md` and clearly state the soak's actual status and duration.
  Review any soak anomalies before calling the run successful.
- [ ] Run `uv sync --locked --extra dev` and all four backend checks from the README.
  The backend suite includes an installed-wheel check from outside the checkout.
- [ ] Run `npm ci`, typecheck, lint, tests, and build from `dashboard/`.
- [ ] Run the installed `arbsync --help`, example generation, and missing-config checks;
  run the capture/replay commands and both documented benchmark commands. Smoke-test backend
  startup and the dashboard dev proxy with a disposable database. Stop their processes
  when the checks finish; live-network availability is separate evidence.
- [ ] Run the same automated checks on Windows and a Unix-like CI runner for this commit.
  Check README startup commands on both; a configured workflow is not a passing run.
- [ ] Check Markdown links, package version/lock consistency, required license files,
  and source artifact contents. Check that configuration examples retain safe defaults.
- [ ] Run the dependency audits described in `SECURITY.md`; review dependency-license
  changes against `docs/DEPENDENCY_LICENSES.md`. Do not treat a static license inventory
  or a point-in-time vulnerability scan as a permanent guarantee.
- [ ] Review repository secret-scanning results and scan candidate history/artifacts
  with the approved secret scanner. Record the scanner/version, commit range, and result;
  never copy suspected secrets into release logs. Resolve findings before publication.
- [ ] Build wheel and source distribution with `uv run python -m build`. Inspect both:
  include package code, example configuration, license and metadata; exclude runtime
  databases, secrets, caches, browser artifacts, and local configuration.
- [ ] Record SHA-256 for the exact built artifacts. Retain the build/check logs and
  matching source commit. Do not rebuild different artifacts under the same release tag.
- [ ] Review the changelog, upgrade notes, support policy, and known limitations.
- [ ] Confirm a clean worktree, review the final candidate diff, and obtain the owner's
  explicit publication request before pushing a tag or publishing release artifacts.

CI/status badges should link to workflows with reviewed, stable public executions.
Do not add a passing badge merely because a workflow file exists.

## Automated content and artifact checks

From the candidate checkout, run:

```powershell
uv run python tools/check_release.py --require-clean
uv run python -m build --no-isolation --outdir var/release
uv run python tools/check_release.py --require-clean --artifacts var/release --output var/release/checks.json
```

Use a fresh artifact directory; the checker requires exactly one wheel and one source
distribution. It checks inline Markdown filesystem links, required release documents,
the package/lock version, packaged code/configuration/license/metadata, and accidental
runtime content. It reads archives without extracting them. JSON output records the
candidate commit, dirty state, findings, and exact artifact hashes.
The recorded commit identifies the checkout being checked; it cannot independently
prove which source commit produced supplied artifacts. Preserve the build log as well.
Heading anchors, external links, dependency licenses, secret values, hosted CI outcomes and live behavior
remain separate checklist items. A successful content check is not release approval.

CI runs backend and dashboard checks on Windows and Ubuntu, builds the package pair,
and prints the same artifact inspection record. Retain the corresponding hosted job
logs for a release candidate; configured jobs still require actual successful execution.
