# Dependency license audit

This is a compatibility review of the dependencies resolved by `uv.lock` and
`dashboard/package-lock.json` as of September 9, 2026. It is not legal advice and does
not replace the license terms shipped by each dependency.

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
