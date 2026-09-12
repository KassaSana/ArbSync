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
