# Changelog

All notable changes to Acceptora Agent Skill are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.3] - 2026-08-29

### Fixed

- Preserve a protected Windows Codex runtime boundary while granting only the resolved machine-local `CodexSandboxUsers` group inheritable read/execute access, including on the live hook's Python 3.13+ `state` directory, so the sandbox can execute the trusted instruction reader and read its fresh snapshot without receiving write, modify, delete, or token access.
- Regression-test the exact Codex runtime ACL and the real atomic snapshot writer's inherited state/reader/snapshot access, owner-only client configuration, and bytecode-free instruction preflight.

### Changed

- Mark Antigravity CLI `1.1.22` lifecycle integration unsupported after real-client smoke showed that discovered `hooks.json` entries did not dispatch `PreInvocation` or `Stop`; new preview, plan, and apply operations fail closed while `status`, `rollback-plan`, and rollback remain available for existing receipts.
- Exclude Antigravity from published `supported_clients` and distinguish direct wrapper/adapter tests from real client-hook dispatch evidence.

## [1.2.2] - 2026-08-29

### Fixed

- Generate Windows Antigravity hook commands through a trusted absolute PowerShell `-EncodedCommand` wrapper that preserves paths with spaces, hook input/output, and Python exit codes without exposing quoted executable tokens to a `cmd /c` boundary.
- Regression-test the generated wrapper by invoking it directly through `cmd.exe`, including a runtime path with spaces, non-zero exit propagation, deterministic command encoding, and bytecode-free execution. This test does not prove Antigravity client dispatch.

## [1.2.1] - 2026-08-28

### Fixed

- Canonicalize drive-absolute and UNC Windows local Git origin locators to forward slashes in the source manifest and completion-gate mapping while preserving credential-free URLs and SCP-style remotes.
- Reuse the manifest-produced repository locator across feature resolution, reconciliation, feedback, verification exceptions, and completion gates instead of reconstructing source identity from the current working directory.

## [1.2.0] - 2026-08-28

### Added

- An explicit Antigravity CLI `1.1.22` installer/configuration profile, workspace skill and `AGENTS.md` integration, generated `PreInvocation`/`Stop` hook entries, and client discovery checks. Lifecycle support was subsequently withdrawn in `1.2.3` after real-client dispatch failed.
- A credential-safe Antigravity stdio-to-Streamable-HTTP MCP bridge that loads the project-derived token from the client process without writing it to `mcp_config.json`.
- Account/project verification instructions for analysis priorities, executable manual steps, and safe test data, fetched before work into an authenticated, checksummed installer-owned snapshot and reread before reconciliation.
- Model-visible task-start preflight for Codex, Claude Code, and Gemini CLI, plus generated Antigravity injected-reader output and a pinned isolated snapshot reader that never embeds owner-authored bodies in hook output.
- Optional provider-neutral evidence lineage for project/provider-run identity, timing, artifacts, assertions, authentication, cost, usage, stop reason, and an original-payload reference, with unchanged v1 and legacy evidence compatibility.
- Optional `automated_evidence` include and response projections in feature-context reads so persisted lineage can round-trip through MCP and REST clients.
- Installed generated-hook command regression coverage across client adapters, including bytecode-free runtime and clean installer-status assertions; real-client dispatch remains a separate acceptance boundary.

### Fixed

- Require `reconcile_checklist` to send the same field-for-field request object that passed local validation, preserve required empty arrays, and regression-test removal of every contract-required root field.
- Prevent installed live hooks from writing Python bytecode into the managed external runtime so status and rollback remain clean after client execution.
- Invoke the pinned Python hook command with PowerShell's call operator on Codex for Windows so lifecycle hooks execute in the client's actual shell.
- Keep CI syntax compilation bytecode outside the checkout and reject any `__pycache__`, `.pyc`, or `.pyo` artifact left in the package tree.
- Run every documented isolated Python command and owner-runtime probe with `-B -I` so following the public setup and recovery procedures cannot create package bytecode caches.
- Return checklist sections and complete immutable active/retired item definitions, including target and test data, from the `checklist_definitions` projection so clients can retain or update without losing fields.
- Derive each installed agent credential variable from its Acceptora project ID so multiple projects in one client process cannot select the same bearer-token slot.
- Track shared client configuration by the exact values each installation added so status and rollback preserve pre-existing and unrelated settings, reject ambiguous same-target hooks, and support non-LIFO project removal.
- Keep project credentials out of installer, hook-update, source-manifest, ACL, and release-tool subprocess environments.
- Align configuration-review baselines with Antigravity CLI `1.1.22`, Codex CLI `0.150.1`, and stable Gemini CLI `0.57.0`; Antigravity lifecycle dispatch was not established by that baseline.
- Validate the complete lineage object, strict millisecond timestamps, duration consistency, non-executable credential-free URI references, digests, bounded scalar assertion details, authentication consistency, unique usage pairs, cost bounds, project/source identity, and sensitive lineage fields before sending a checklist.

### Changed

- Require explicit `--client antigravity-cli` selection because Antigravity and Codex share the `.agents` workspace layout; public setup commands now use explicit client selection for every provider.
- Document Google's Antigravity migration path for individual Google-account OAuth after Gemini CLI's consumer service transition. Acceptora lifecycle support for that path was subsequently withdrawn in `1.2.3`.
- Print the exact project-derived credential variable in installation previews and plans while keeping its value outside installer files, receipts, and output.
- Fail closed on Codex/Claude prompt submission and Gemini `BeforeAgent` when fresh verification guidance cannot be validated, and generate an Antigravity stop-before-work directive for direct `PreInvocation` adapter execution. Antigravity client dispatch was not established.
- Bind every new reconciliation request to the immediately reread account/project revisions and effective digest without sending instruction bodies.
- Generate the bounded update check for Antigravity `PreInvocation`; Codex, Claude Code, and Gemini CLI run their `SessionStart` update check. Antigravity client dispatch was not established.

### Security

- Refuse to place the Acceptora bearer token in Antigravity's remote-header configuration; the installer-owned stdio bridge reads only the pinned project-derived environment variable and enforces the existing redirect, transport, session, and payload bounds.
- Isolate client/worktree runtimes and MCP aliases so several projects can coexist in shared user configuration and be rolled back independently.

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
