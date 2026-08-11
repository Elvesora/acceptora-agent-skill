# Lifecycle and completion gate

## Eligibility decision

Require synchronization when all conditions hold:

1. The agent changed an artifact or configuration for the user.
2. The result has observable behavior, appearance, content, integration, data, documentation, configuration, deployment, or regression risk.
3. The work belongs to an identifiable feature, fix, improvement, or deliverable.
4. The agent is at the completion boundary.

Typical eligible changes include features, bug fixes, observable-risk refactors, API/SDK/schema changes, UI changes, content/docs, permissions, schedulers, deployments, and verification-feedback fixes.

## Required sequence

1. Capture a baseline before work and current source state after final edits/tests.
2. Resolve the project and stable feature.
3. Fetch current context and feedback.
4. Inspect final changed surfaces and actual automated evidence.
5. Select patterns and draft one observable claim per item.
6. Address only feedback actually fixed.
7. Reconcile with optimistic concurrency and a logical-write idempotency key.
8. Run the completion gate against the final source digest.
9. Return the real feature URL or a visible recovery artifact.

Repeat steps 1-8 if source changes after the digest is calculated.

## Strict source capture

The installed v1 runtime uses Git-only full-worktree capture. It hashes every eligible tracked worktree file and untracked file and binds index mode/object identity without trusting Git's stat cache. Submodules/gitlinks, unresolved index stages, `assume-unchanged` or `skip-worktree` entries, unsafe or non-UTF-8 paths, special filesystem objects, unstable reads, and Git command failures are unsupported and fail closed. There is no automatic filesystem fallback. Direct REST clients may implement another deterministic source adapter only when they submit the exact OpenAPI source-descriptor and manifest contract.

## Completion-gate outcomes

- `pass`: the exact eligible source digest has a synchronized checklist or valid exception.
- `continue_sync`: keep working; synchronize before normal completion.
- `not_required`: no effective changed source or wholly ignored changes.
- `ambiguous`: do not guess identity; request an explicit feature ID or approved new feature.
- `unavailable`: retry with bounded backoff, then produce an offline outbox and visible warning.

When service health returns, use [offline-recovery.md](offline-recovery.md). A replay is complete only after the exact MCP write succeeds and, when the record carries a completion-gate payload, that gate returns `pass` or `not_required`.

The gate evaluates synchronization only. It never means the human accepted the feature.

## Allowed changed-source exceptions

Use only:

- `exact_revert_to_accepted_revision`
- `mechanical_non_observable`
- `user_explicitly_disabled`

Persist the category, specific explanation, exact source digest, deterministic changed-surface manifest, and automated evidence. A later digest invalidates the exception. Never use an exception to bypass active checklist state or open feedback.

## Client guarantees

- Codex and Claude Code can enforce task-start baseline plus `Stop` completion checks when their adapters are installed and trusted.
- Gemini CLI can capture a baseline with `SessionStart`/`BeforeAgent` and use `AfterAgent` to deny an incomplete response and request another attempt. This is the closest Gemini equivalent to a completion hook; bounded loop protection prevents endless retries.
- Among Anthropic clients, version 1 supports Claude Code only. Claude chat/web/Desktop custom connectors require authless or OAuth remote-connector authentication, which the project-scoped bearer-only Acceptora MCP endpoint does not provide.
