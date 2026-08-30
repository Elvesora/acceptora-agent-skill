# Security Policy

## Supported production source

| Source | Security updates |
| --- | --- |
| Canonical `main` branch | Supported |

Security fixes are published to the canonical repository's production `main` branch. Install from a fresh clone of that branch. Application-hosted mirrors and automatically generated source archives are not supported acquisition paths. Update checks always compare with canonical `main`; semantic versions describe compatibility but do not select the production source. Keep the selected agent client, Python, Git, and operating system current.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the private [GitHub Security Advisory form](https://github.com/Elvesora/acceptora-agent-skill/security/advisories/new) when it is available.

If the private-reporting form is unavailable, use the [Acceptora contact page](https://www.acceptora.com/contact) only to request a secure reporting channel. Do not put vulnerability details, reproduction steps, credentials, source code, personal data, customer data, or production URLs in the initial contact request.

Include through the private channel:

- the affected package, integration, contract, Python, Git, operating-system, and client versions;
- the security impact and affected installation, hook, MCP, REST, health, distribution, or recovery operation;
- reproducible steps using synthetic data and a disposable repository;
- whether credentials, logs, redirects, TLS, response limits, filesystem boundaries, source capture, plan acceptance, rollback, or MCP approval behavior are involved;
- any proposed mitigation.

Revoke any credential that may have been exposed during investigation.

## Security model

The package applies the following controls:

- the production source is the canonical repository's `main` branch; each supported installation records its exact commit and deterministic file digests;
- installation is non-mutating until the user accepts the exact reconstructed plan digest;
- apply and rollback recheck source, input, destination, ownership, and digest preconditions;
- trusted lifecycle commands run from an installer-owned external runtime rather than repository-controlled copies;
- Git source capture rejects submodules, unresolved index stages, hidden index flags, unsafe paths, special files, and unstable reads;
- bearer tokens are loaded from the project-derived environment-variable name pinned in each installer-owned runtime and are not written to plans, receipts, repository configuration, logs, or output;
- remote clients do not follow redirects and permit plain HTTP only for loopback development;
- network timeouts, retry counts, request sizes, response sizes, and error text are bounded;
- hooks use bounded loop protection and fail open with a visible warning when enforcement cannot run;
- no agent operation can make a human verification decision.

The operating system, current account, trusted administrators, repository code, agent client, effective hooks, and MCP configuration remain part of the security boundary. Receipt checksums detect corruption and bind reviewed plans; they do not authenticate a receipt against the current account, so a party able to rewrite the private runtime and recompute its receipt is already inside that trusted boundary and can change rollback authority. Every project token exported to one client process can be inherited by repository commands in that process; project-derived names route credentials correctly but do not provide operating-system secrecy between those repositories. Use separate client processes with only one exported project token when stronger isolation is required. Do not install or run the integration on an untrusted fork or pull request.
