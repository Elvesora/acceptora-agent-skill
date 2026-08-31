# Changelog

All notable changes to Acceptora Agent Skill are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-01

### Added

- Project-local installation for Codex, Claude Code, and Gemini CLI.
- MCP-first workflow with a versioned REST fallback.
- Project-scoped key validation and fresh account/project verification instructions before work and before manual verification steps.
- Deterministic release ZIP, manifest, provenance, and SHA-256 checksums.

### Security

- Derive project identity from the remotely validated project key and keep every worktree bound to its own credential variable.
- Never read or write an existing project environment file; stop and ask the user to place the validated key there.
- Preserve unrelated project instructions and client configuration, and refuse destructive update or uninstall when installer-owned state has drifted.
- Keep human verification decisions human-only.
