# Changelog

All notable changes to Acceptora Agent Skill are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2026-09-02

### Changed

- Store the validated project key in the current Git root's `.acceptora-env` as `ACCEPTORA_PROJECT_TOKEN`.
- Complete installation in the same command after validating and storing a prompted key.

### Security

- Keep application `.env` files untouched and reject a tracked `.acceptora-env`.
- Keep each project bound to the authenticated project returned for its own local key.

## [1.0.1] - 2026-09-01

### Fixed

- Use the authenticated API project ID instead of the credential ID embedded in a project key.
- Show masked feedback while a project key is entered or pasted into the installer.

## [1.0.0] - 2026-09-01

### Added

- Project-local installation for Codex, Claude Code, and Gemini CLI.
- Dependency-free `npx` installer with explicit `doctor`, `update`, and `uninstall` commands.
- MCP-first workflow with a versioned REST fallback.
- Project-scoped key validation and fresh account/project verification instructions before work and before manual verification steps.
- Deterministic release ZIP, manifest, provenance, and SHA-256 checksums.

### Security

- Derive project identity from the remotely validated project key and keep every worktree bound to its own credential variable.
- Never read or write an existing project environment file; stop and ask the user to place the validated key there.
- Preserve unrelated project instructions and client configuration, and refuse destructive update or uninstall when installer-owned state has drifted.
- Keep human verification decisions human-only.
