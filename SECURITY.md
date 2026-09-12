# Security policy

## Supported versions

ArbSync is an alpha project. Security fixes target the current default branch; older
commits and release versions do not receive separate backports. Update to the latest
default-branch revision before checking whether an issue is already fixed. The package
version alone does not identify the installed revision: include the commit ID in reports.
There is no guaranteed response time or security support service agreement.

## Report a vulnerability privately

Use GitHub's [private vulnerability reporting page](https://github.com/KassaSana/ArbSync/security/advisories/new).
Do not put exploit details, credentials, or sensitive logs in public issues or pull requests.
If the reporting form is unavailable, open an issue asking only for a private security
contact, without describing the vulnerability, and wait for a private channel before
sharing details. The Git commit noreply address is not a reporting inbox.

Include the affected commit/version, deployment configuration, impact, reproduction
steps, and a minimal proof of concept if available. Sanitize logs and omit credentials
and personal data. Test only systems you own or have permission to assess; a local
reproduction is preferred to probing a public deployment or exchange service.

The maintainer will assess the report and coordinate any fix and disclosure with the
reporter through the private thread. Please keep details private while that coordination
is in progress. Ordinary bugs can use public issues; report suspected unauthorized
access, data exposure, or exploitable resource exhaustion privately.

## Deployment boundary

ArbSync consumes public market data without exchange credentials and does not place
trades. Its theoretical opportunities are not execution or accounting guarantees.
Follow the [hosted deployment guidance](README.md#hosted-deployment) for TLS, origin
restrictions, and proxy request/connection limits. Direct public Uvicorn exposure is
not a supported deployment. Operators are responsible for updating their installation
and protecting the host, proxy, configuration, logs, and SQLite files.

## Dependency maintenance and audit triage

[Dependabot](.github/dependabot.yml) proposes weekly updates for the Python `uv.lock`,
dashboard npm lockfile, GitHub Actions, and isolated audit tooling. Review and merge
updates through normal pull requests; automated merging is not configured. Keep action
references pinned to full commit SHAs with readable version comments. Review updates to
the workflow's pinned uv installer version when upgrading local uv.

The [dependency audit workflow](.github/workflows/dependency-audit.yml) runs on pushes,
pull requests, weekly, and manually. Python checks export all extras from the unchanged
lockfile and audit applicable dependencies on Python 3.11 on Linux and Windows. This
includes runtime, development, and profiling dependencies, but does not establish
coverage of every Python-version-specific dependency or downloaded browser binary.
The npm check includes runtime and development packages. Advisory findings at any
severity fail the checks; network/service failures also fail rather than count as clean.
These checks detect known advisories, not every malicious package or undisclosed flaw.

Reproduce Python auditing from the repository root using isolated tooling:

```text
uv export --locked --all-extras --no-emit-project --format requirements.txt --output-file var/audit-requirements.txt
uv tool run --with-requirements tools/requirements-audit.txt pip-audit --strict --disable-pip --no-deps --require-hashes --progress-spinner off -r var/audit-requirements.txt
```

Create `var/` first if it does not exist. From `dashboard/`, run
`npm audit --include=dev --audit-level=low`. Audits contact public package/advisory
services and send package names and versions. No application credentials are needed.

For a finding, record its advisory ID, affected locked version, dependency path,
runtime or build exposure, and available fix. Prioritize exploitable runtime issues
and compromised build tools. Upgrade the smallest affected dependency set, regenerate
the relevant lockfile, and run the audit and behavior checks before merging. Do not use
forced automatic upgrades as a substitute for reviewing breaking changes.

If no fix exists or a finding is not applicable, document evidence, mitigation, an owner,
and a review expiry in a linked issue (privately for exploitable project details).
Any temporary advisory-specific suppression requires maintainer review in a PR and
must be removed or renewed at expiry. There are currently no suppressions. Do not
disable the audit job, lower its threshold, or ignore all failures to get a green build.

## Secret scanning

GitHub secret scanning and push protection were verified enabled for this repository
on 2026-09-12. Before publication or a repository transfer, verify both remain enabled
under Settings > Security > Code security. See GitHub's
[secret scanning setup guidance](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/detect-secret-leaks/enable-secret-scanning).
Availability depends on repository visibility and plan; if unavailable, document that
gap and scan the full Git history with a local secret scanner before publishing.

Never commit credentials, private keys, production configuration, or sensitive logs.
Review staged changes and full history before a release; filename ignore rules alone
do not detect secrets. If a secret is found, revoke or rotate it first, assess its use,
then coordinate cleanup with the maintainer. Deleting the current file does not remove
the secret from Git history or forks. Do not paste the secret into an issue or bypass
push protection for a real credential.
