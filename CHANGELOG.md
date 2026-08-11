# Changelog

All notable changes to Acceptora Agent Skill are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
