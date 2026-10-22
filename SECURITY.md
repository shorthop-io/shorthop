# Security policy

## Reporting a vulnerability

Email **security@shorthop.io**. Please do not open a public issue, pull request or discussion
for a security problem.

Include what you found, how to reproduce it, the version you tested (the image tag, or the
commit), and what an attacker could do with it. A proof of concept helps but is not required.

What happens next:

- You get a reply within **5 working days** saying whether the report is confirmed.
- A confirmed issue is fixed in a new release, and the release notes say what was fixed.
- You are credited in those notes, unless you ask not to be.
- Please give us **90 days** from the report before publishing details, or less once a fixed
  release is out. If we need longer, we will say why.

Shorthop is maintained by one person today. The times above are a promise, not a service
level agreement.

## Supported versions

Security fixes go into the **latest release** only. Upgrade to it to get a fix — the database
is migrated on start (see [Running it](docs/operations.md#upgrading)).

## In scope

- This repository: the Shorthop server, its web UI, the Docker image, the Helm chart and the
  Docker Compose setup.
- The hosted relay that delivers pushes to the phone app (`relay.shorthop.io`) and the account
  pages (`account.shorthop.io`).

Not in scope: denial of service by volume, findings that need an already-compromised admin
account or host, missing hardening headers with no demonstrated impact, and reports from
automated scanners without a working attack.

How Shorthop protects itself is described in [docs/security.md](docs/security.md).
