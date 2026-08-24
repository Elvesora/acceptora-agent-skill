# Changelog

All notable changes to Acceptora Agent Skill are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-08-23

### Added

- A ten-minute [GETTING-STARTED.md](GETTING-STARTED.md) tutorial for the human install path, leaving [SETUP.md](SETUP.md) as the security specification.
- Skill command routing for `$acceptora init`, `$acceptora doctor`, and the default completion workflow so completion hooks still reconcile without a menu. The installed skill name is `acceptora`.
- The downloadable ZIP is `acceptora-<version>.zip` and extracts as `acceptora/` beside `acceptora-agent-skill-provenance.json`.
- Installer client auto-detection and `--format text` human plans. `--output` still stores the JSON plan; `apply` still requires the exact digest.
- A central machine-readable provider registry for Codex, Claude Code, and Gemini CLI, consumed by installation, trusted runtime, and release metadata.
- A dated client capability matrix separating provider-documented support from Acceptora-generated defaults and reviewed build baselines.
- Optional `evidence_sufficiency` and `blocker_reason` fields that keep proof quality and a `not_run` cause separate from execution outcome and human acceptance.
- Explicit final setup confirmation through `health_check.py --confirm-connection`, requiring all seven normal workflow scopes and performed only after every REST, project, MCP, tool, annotation, and schema check passes.
- A deterministic installable ZIP convenience bundle with embedded canonical-repository, `main` branch, source-commit, and complete source-tree provenance.

### Fixed

- Retry one transient Windows ACL-inspection timeout while continuing to reject repeated timeouts and actual ACL command failures.
- Keep required pull-request CI jobs runnable by limiting the publishable production ZIP build to `main` while preserving its clean-commit provenance checks.
- Align the Gemini CLI capability baseline with stable `0.56.0`, where the documented `/mcp reload` discovery command is supported.
- Validate all published checklist request bounds, nested types, enums, RFC 3339 timestamps, automated-evidence conditionals, source manifests, and ignored entries before sending a checklist.
- Run the remote `main` update query from an isolated repository-free directory so a target repository's local `url.*.insteadOf` configuration cannot redirect it.
- Remove Laravel-, Composer-, and Node-oriented default source ignores that could omit legitimate untracked files in repositories using another stack.
- Reject non-ignored special filesystem objects that Git omits from untracked listings while pruning repository-ignored directories during strict source capture.

### Changed

- Derive client choices, paths, templates, adapters, lifecycle update events, and published client metadata from the provider registry instead of duplicated code and package-manifest fields.
- Use the canonical GitHub repository's `main` branch as the production source and update authority while retaining an application-hosted ZIP generated from an exact clean `main` commit.
- Record the installed Git commit and perform non-blocking, credential-free `SessionStart` update checks against `refs/heads/main`.
- Keep ordinary health diagnostics read-only; only the explicit final setup command marks the project connection established, without adding a ninth MCP tool.
- Define Git and Python as installer/runtime dependencies only, and make MCP and REST usage explicitly independent of the target project's programming language, framework, or SDK choices.

## [1.0.0] - 2026-08-11

### Added

- Standalone `verify-generated-work` skill with progressive references for lifecycle, checklist, reconciliation, feedback, recovery, and task-specific verification patterns.
- Secure plan-and-apply installer for Codex, Claude Code, and Gemini CLI with exact plan-digest acceptance and digest-bound rollback.
- Installer-owned external runtime with pinned Python and Git executables, strict Git source capture, client hooks, configuration, receipts, and health checks.
- Streamable HTTP MCP configuration for all supported clients and a contract-equivalent versioned REST integration.
- Deterministic ZIP and tar.gz release archives, release manifest, source provenance, file digests, artifact digests, and SHA-256 checksum file.
- Offline outbox validation and replay with preserved idempotency identity, redirect refusal, token redaction, and bounded retries.
- Cross-client unit tests, contract fixtures, source-package checks, deterministic archive checks, and supported-Python GitHub Actions gates.
- Codex CLI 0.144.0 minimum compatibility for the generated per-server `writes` approval policy, with Codex CLI 0.147.0 as the reviewed release baseline.
- Public setup, support, contribution, conduct, license, and vulnerability-reporting guidance.
- Non-blocking `SessionStart` release notifications for Codex, Claude Code, and Gemini CLI, backed by installer-owned pinned endpoints and a five-minute external-runtime cache.

### Security

- Reject unsupported worktree state, unsafe paths, symlinks or junctions at managed boundaries, untrusted executables, insecure external runtime paths, secrets in checklist payloads, and ambiguous release provenance.
- Keep bearer credentials in environment variables, disable credential-bearing redirects, bypass ambient proxies for authenticated HTTP loopback requests, bound request and response sizes, and avoid logging tokens or sensitive payloads.
- Verify the public manifest response digest, clean provenance, canonical file inventory, source-tree identity, supported client, and ZIP metadata without sending a credential, following redirects, downloading a bundle, or changing customer setup.
- Bound aggregate MCP tool-discovery count and decoded size, and require replay acknowledgements to match the pinned JSON-RPC protocol, server, session, operation, feature, source digest, checklist revision, and one-to-one feedback resolution and thread identities.
- Scan object keys and values for credential patterns, including sensitive credential labels, encrypted private-key blocks, embedded Acceptora token substrings, and disguised placeholder values; normalize or redact remote error codes, correlation IDs, HTTP failure details, and completion-gate output before persistence or customer-visible output.
- Detect Windows reparse points during offline replay even on Python 3.11, where `Path.is_junction()` is unavailable.
