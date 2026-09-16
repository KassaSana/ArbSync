# Dependency license audit

This is a compatibility review of the dependencies resolved by `uv.lock` and
`dashboard/package-lock.json` as of September 9, 2026. It is not legal advice and does
not replace the license terms shipped by each dependency. Re-run the review whenever a
lockfile changes. Dependabot updates and vulnerability audits are configured by ARB-017;
license review remains a separate manual check.

ArbSync is licensed under Apache-2.0. The resolved Python and dashboard dependencies
use permissive MIT, BSD, ISC, Apache-2.0, PSF, Blue Oak, or similarly permissive terms,
with these exceptions:

- `certifi`, `hypothesis`, and `pathspec` use MPL-2.0. They remain separately licensed
  dependencies; their file-level copyleft terms do not change ArbSync's license.
- `caniuse-lite` contains browser-compatibility data under CC-BY-4.0. It is consumed by
  the frontend build toolchain and is not copied into this repository.

No resolved dependency declares GPL, AGPL, LGPL, SSPL, or another license that requires
ArbSync as a whole to use different terms. Packages whose lock entries omit a license
field were checked against their installed distribution metadata; the affected React
Router packages declare MIT.

Dependencies are not vendored in this repository. Distributors remain responsible for
preserving applicable third-party license and attribution notices when redistributing
dependencies or bundled artifacts.

## Dashboard security refresh, September 12, 2026

Rechecked every license declaration in the refreshed dashboard lockfile after upgrading
React Router to 7.18.3, Vite to 6.4.3, Vitest to 4.1.11, and affected transitive packages.
All entries declare a license: MIT, MIT-0, ISC, Apache-2.0, BSD-2-Clause, BSD-3-Clause,
BlueOak-1.0.0, MIT AND ISC, or the existing CC-BY-4.0 browser data. No new license
category requires changing the project's license. The Python application lockfile is
unchanged; the earlier Python review still applies.

## Lockfile refresh, September 16, 2026

Both lockfiles changed after the September 12 review: the dashboard moved to Vite 8.3.0
with `@vitejs/plugin-react` 6, jsdom 30, ESLint 10.10 and `@typescript-eslint` 8.70,
and the Python lockfile took Ruff 0.16.7 and Hypothesis 6.168.0. The inventory was
re-derived from every resolved entry rather than from the changed packages alone.

Python (59 locked distributions, including the two marker-gated entries `tomli` for
Python 3.11 and `httpx2-jsfetch` for Emscripten): every entry declares MIT, BSD, ISC,
Apache-2.0, PSF, or a permissive combination of those, except the same three MPL-2.0
packages already listed above (`certifi`, `hypothesis`, `pathspec`). No new category.

Dashboard (383 locked packages): 296 MIT, 28 ISC, 22 Apache-2.0, 8 BSD-3-Clause,
8 BSD-2-Clause, 4 BlueOak-1.0.0, 2 MIT-0, one `MIT AND ISC`, the existing CC-BY-4.0
`caniuse-lite` data, and two categories new since the last review:

- `lightningcss` 1.33.0 and its eleven platform-binary packages use MPL-2.0. They arrived
  with Vite 8, which uses Lightning CSS to transform stylesheets at build time. They are
  development dependencies that run in the build toolchain; none of their code is
  emitted into the built dashboard bundle, so their file-level copyleft does not attach
  to ArbSync's own files or change the project's license.
- `mdn-data` 2.27.1 is CC0-1.0 public-domain data consumed by the CSS toolchain, also
  build-only.

Every package declares a license field; no entry required falling back to installed
metadata this time. The conclusion is unchanged: no resolved dependency requires ArbSync
as a whole to adopt different terms.
