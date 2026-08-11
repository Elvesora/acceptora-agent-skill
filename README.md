# Acceptora Agent Skill

[![Tests](https://github.com/Elvesora/acceptora-agent-skill/actions/workflows/tests.yml/badge.svg)](https://github.com/Elvesora/acceptora-agent-skill/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

The Acceptora Agent Skill connects coding agents to Acceptora's durable manual-verification workflow. It helps development teams keep one source-bound acceptance checklist synchronized after software, content, configuration, API, SDK, integration, data, or deployment changes.

This repository distributes a standalone agent skill and its secure installer for Codex, Claude Code, and Gemini CLI. It is not a Codex plugin. Applications in any language can use the contract-equivalent REST API without installing an agent client.

No agent operation can make a human verification decision or grant final acceptance.

## What it provides

- Deterministic Git source manifests that bind the checklist to the reviewed revision.
- Reconciliation that preserves human decisions and reopens only materially affected checks.
- Completion hooks for Codex, Claude Code, and Gemini CLI with bounded, visible fail-open behavior.
- Session-start release checks that verify published manifest integrity and notify without downloading or applying updates.
- Streamable HTTP MCP configuration plus a language-neutral [REST API contract](references/rest-api-contract.md).
- Plan-and-apply installation with exact digest acceptance, conflict detection, status, and digest-bound rollback.
- A pinned external runtime for health checks, hooks, recovery, and lifecycle commands.
- Secret rejection, no-follow redirect behavior, bounded network payloads, and offline recovery.

## Requirements

- Python 3.11 or newer and Git.
- Codex CLI 0.144.0 or newer, or a supported Claude Code or Gemini CLI release.
- The project ID and canonical HTTPS origin shown in Acceptora **Settings > Connection**.
- A short-lived Acceptora project credential with the scopes listed in [SETUP.md](SETUP.md#agent-client-prerequisites).
- A trusted Git worktree that satisfies the installer's strict source and filesystem checks.

Minimum and reviewed client configuration baselines are recorded in [`config/package-manifest.json`](config/package-manifest.json).

## Install

Follow [SETUP.md](SETUP.md) for download verification, credentials, plan review, installation, client checks, health verification, rollback, upgrades, and REST-only integration.

Install only after both release routes on the canonical Acceptora origin return `200`:

- `<canonical-origin>/agent-skill/release-manifest.json`
- `<canonical-origin>/agent-skill/verify-generated-work.zip`

A `404` or `503` means the verified release is unavailable. Do not substitute a repository working tree or an unverified mirror. Contact [Acceptora support](https://www.acceptora.com/contact) if the routes remain unavailable.

The secure installation sequence is:

1. Download the manifest and ZIP from the canonical origin.
2. Verify response-header, manifest, byte-size, version, clean-source, and immutable-commit claims.
3. Generate a non-mutating installation plan outside the target worktree.
4. Review and explicitly accept the exact plan SHA-256 before apply.
5. Confirm client skill, MCP, and hook discovery, then run the pinned external health check.

## Supported integrations

| Client | Remote MCP configuration | Completion lifecycle |
| --- | --- | --- |
| Codex | Streamable HTTP `url` with `bearer_token_env_var` | `SessionStart`, `UserPromptSubmit`, `Stop` |
| Claude Code | `type: "http"`, `url`, and an environment-expanded authorization header | `SessionStart`, `UserPromptSubmit`, `Stop` |
| Gemini CLI | Streamable HTTP `httpUrl`, environment-expanded headers, and `trust: false` | `SessionStart`, `BeforeAgent`, `AfterAgent` |

Configuration examples live in [`config/`](config), and client hook adapters live in [`adapters/`](adapters).

## Security boundary

Use a short-lived, least-scope credential from a secure environment variable or secret provider. Never commit credentials or place them in prompts, URLs, plans, logs, fixtures, or repository configuration. The operating system, current user account, trusted administrators, repository code, and client configuration are part of the trust boundary.

The installer never reads the token, grants client trust, or approves tools. Review the complete model in [SETUP.md](SETUP.md#boundary) and report suspected vulnerabilities through [SECURITY.md](SECURITY.md).

## Package layout

- [`SKILL.md`](SKILL.md) contains the focused agent workflow.
- [`references/`](references) contains contracts, lifecycle rules, reconciliation guidance, and task-specific patterns loaded only when needed.
- [`scripts/`](scripts) contains deterministic installation, validation, health, source-manifest, release, and recovery tooling.
- [`adapters/`](adapters) and [`config/`](config) contain reviewed client integration templates.
- [`tests/`](tests) covers success, failure, security, deterministic release, and cross-client behavior.

Repository documentation and community files remain outside the installed skill payload.

## Validate the source package

Run the complete Python test suite:

```text
python -m unittest discover -s tests -p "test_*.py"
```

On Windows, keep line-ending behavior local to this checkout and verify it before creating a release:

```text
git config --local core.autocrlf false
git config --local --get core.autocrlf
```

The second command must print `false`. This does not change the user's global Git configuration. Release installation must use the digest-verification and explicit-plan acceptance flow in [SETUP.md](SETUP.md#verify-the-downloaded-bytes).

## Community and support

- Read release changes in [CHANGELOG.md](CHANGELOG.md).
- Get usage help through [SUPPORT.md](SUPPORT.md).
- Report reproducible defects with a safe, minimal GitHub issue.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md) in project spaces.
- Use the private process in [SECURITY.md](SECURITY.md) for suspected vulnerabilities.

Acceptora Agent Skill is available under the [MIT License](LICENSE).
