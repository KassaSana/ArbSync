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
