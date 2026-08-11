# Acceptora agent setup

This release supports Codex, Claude Code, and Gemini CLI through the Acceptora Agent Skill and Streamable HTTP MCP. Any language can use the contract-equivalent REST API in `references/rest-api-contract.md`.

Installation is available only when the canonical Acceptora origin serves both `/agent-skill/release-manifest.json` and `/agent-skill/verify-generated-work.zip` with status `200`. A `404` or `503` means no verified release is available from that deployment; stop instead of substituting repository files or an unverified mirror. Contact [Acceptora support](https://www.acceptora.com/contact) when availability is unexpected.

## Boundary

Use only a trusted repository and branch. The operating system, the current user account, and trusted administrators are part of the security boundary. The agent token is present in the client environment, so repository commands can inherit it. This version does not protect the token from malicious repository code or project-level client configuration. Do not use it on untrusted forks or pull requests. Use short-lived, least-scope credentials and inspect effective hooks/MCP configuration after branch changes.

The installer never reads the token, grants client trust, or approves tools. It changes files only after an exact installation-plan digest is supplied. No agent operation can make a human verification decision.

## Agent client prerequisites

- A verified release extracted outside the target repository.
- The actual Git worktree root; subdirectory installs are rejected.
- A strict Git worktree with no submodules/gitlinks, unresolved index stages, `assume-unchanged` or `skip-worktree` entries, non-UTF-8 or non-portable paths, or special filesystem objects in the eligible source. The installer rejects unsupported state before mutation.
- Absolute Python 3.11+ and Git executables outside that worktree, with trusted owners and no untrusted write or replacement access along their path. Planning probes the selected Python in isolated mode and requires its reported executable identity to match.
- Codex CLI 0.144.0 or newer, or a current supported Claude Code or Gemini CLI release.
- The project ID and canonical HTTPS origin shown in Acceptora **Settings > Connection**.
- A project credential with the seven normal scopes: `projects:read`, `features:resolve`, `features:read`, `checklists:write`, `feedback:read`, `feedback:address`, and `gates:read`.
- Optional `exceptions:write` only when exact-source policy exceptions are required.

Export the agent credential as `ACCEPTORA_AGENT_TOKEN`. Never write its value to a file, URL, prompt, plan, or log.

## REST-only prerequisites

A direct REST integration does not need the release bundle, an agent client, Git, Python, or the fixed agent token environment-variable name. It needs:

- The project ID and canonical HTTPS origin shown in Acceptora **Settings > Connection**.
- A bearer credential loaded from any secure secret provider, with the seven normal scopes listed above and optional `exceptions:write` only when required.
- An HTTP client that preserves `Authorization`, correlation, idempotency, and retry headers without following credential-bearing cross-origin redirects.
- The public OpenAPI document at `<canonical-origin>/api/v1/integrations/openapi.json` as the request and response schema authority.

## Verify the downloaded bytes

Download these exact paths from the canonical HTTPS origin shown in **Settings > Connection**:

- `<canonical-origin>/agent-skill/release-manifest.json`
- `<canonical-origin>/agent-skill/verify-generated-work.zip`

Require both responses to be `200`. Hash both locally and require each digest to match its `X-Acceptora-Artifact-SHA256` response header. Require the ZIP digest and byte size to match the manifest entry. Accept only `name: verify-generated-work`, the expected version, `source_state: clean`, and an expected immutable source commit.

The current channel has no separate publisher signature. These checks prove the bytes served by the approved Acceptora deployment, not an independent publisher identity.

## Plan

Write the plan outside the target worktree. Commands below use one-line PowerShell syntax: replace every angle-bracket placeholder, preserve the quotes around paths, and keep the leading `&`. On POSIX shells, remove only the leading `&`.

```text
& "<absolute-python>" -I "<release>/scripts/install.py" plan --client "<codex|claude-code|gemini-cli>" --target-root "<absolute-git-worktree-root>" --project-id "<proj_ULID>" --api-base-url "<canonical-https-origin>" --python-executable "<absolute-python>" --git-executable "<absolute-git>" --format json --output "<external-path>/acceptora-install-plan.json"
```

Optional `--runtime-base` and `--client-config-dir` paths must remain outside the worktree and inside the current user's verified home directory. They may not cross a symlink or junction, use an untrusted owner, or permit another local account to write or replace a path in the chain; an existing runtime base must already be private to the current user. Review the target/runtime/config paths, pinned executables, origin, project ID, fixed token name, per-target MCP alias, every file operation and digest, conflicts, and warnings. The plan is non-mutating and contains no token value.

## Apply

Only after the user explicitly accepts the exact `plan_sha256`:

```text
& "<absolute-python>" -I "<release>/scripts/install.py" apply --plan "<external-path>/acceptora-install-plan.json" --accept-plan-sha256 "<exact-plan-sha256>" --format json
```

Apply reconstructs the canonical plan and fails if source, inputs, or target state changed. Save the returned `trusted_installer`, `runtime_root`, `runtime_base`, receipt, client, and target root. Use only that external `trusted_installer` for later lifecycle commands.

## Client review

- Codex: inspect `/skills`, `/mcp`, and `/hooks`; repeat `/mcp` and `/hooks` after branch/config changes.
- Claude Code: confirm skill discovery, inspect `/mcp`, and review the user-scope hook settings.
- Gemini CLI: run `/skills reload` or restart, then inspect the MCP and hook settings. Automatic MCP trust is intentionally absent.

Hooks enforce the completion gate only while they remain enabled, unchanged, and trusted. Bounded loop protection fails open with a visible warning rather than retrying forever.

For Codex, the generated MCP entry uses `default_tools_approval_mode = "writes"`, the current per-server key documented by the [official Codex configuration reference](https://developers.openai.com/codex/config-reference). Codex CLI 0.144.0 is the minimum supported release for this value; earlier builds can reject or ignore the policy. Treat it as client-side approval policy, not server authorization or proof that a particular client build enforced a prompt. Keep Codex current and confirm the expected write-tool prompt during the disposable post-install smoke test.

## Health check

Run the external, pinned health check:

```text
& "<absolute-python>" -I "<runtime-root>/package/scripts/health_check.py" --format json
```

Do not pass repository `.verification/config.json`. Health verifies project identity/lifecycle, canonical endpoints, versions, scopes, OpenAPI, MCP identity, the exact eight tools, approval annotations, and all input/output schema digests. It calls no product write tool and never prints the credential.

## Status and rollback

```text
& "<absolute-python>" -I "<trusted-installer>" status --client "<client>" --target-root "<absolute-git-worktree-root>" --format json

& "<absolute-python>" -I "<trusted-installer>" rollback-plan --client "<client>" --target-root "<absolute-git-worktree-root>" --format json --output "<external-path>/acceptora-rollback-plan.json"

& "<absolute-python>" -I "<trusted-installer>" rollback --plan "<external-path>/acceptora-rollback-plan.json" --accept-rollback-plan-sha256 "<exact-rollback-plan-sha256>" --format json
```

Repeat the original `--runtime-base` on `status` and `rollback-plan` if it was customized. Review every rollback removal and its exact digest. Changed owned content causes rollback to stop.

## REST-only clients

A direct REST client may load the bearer token from any secure secret provider; the `ACCEPTORA_AGENT_TOKEN` name is specific to installed agent clients. Start with the unauthenticated OpenAPI document, then authenticate `GET /api/v1/integrations/project` before writes. The required normal sequence is project preflight, `resolve_feature`, `get_feature_context`, `reconcile_checklist`, `check_completion_gate`, and `get_verification_status`. Feedback work adds `get_verification_feedback` and `address_feedback` before a full reconciliation. Use the exact OpenAPI schemas and read `references/rest-api-contract.md`.

Never follow credential-bearing redirects, silently switch transports after an ambiguous write, or reuse an idempotency key for different bytes.

## Recovery

After bounded retry exhaustion, inspect and replay the secret-free outbox only through the external runtime:

```text
& "<absolute-python>" -I "<runtime-root>/package/scripts/replay_offline_outbox.py" --dry-run
& "<absolute-python>" -I "<runtime-root>/package/scripts/replay_offline_outbox.py"
```

Read `references/offline-recovery.md` before handling a non-transient conflict.

## Post-install and upgrades

The recorded client versions are reviewed configuration baselines, not proof of a live client session. Before production use, run a disposable end-to-end smoke with the installed client: confirm skill discovery, MCP discovery, each hook event, read-only project preflight, one authorized test workflow, and token revocation.

On `SessionStart`, the installer-owned runtime checks a five-minute cache and performs at most one bounded, unauthenticated GET of the pinned canonical release manifest during that interval. It follows no redirects, sends no bearer token, and verifies the response-header SHA-256 and clean release identity. The external runtime cache stores either the verified result or a generic failure status without copying rejected manifest content. A baseline-capture warning does not suppress this separate update check. A newer verified release produces a non-blocking client message with the manifest digest, source commit, and expected ZIP digest and size. It does not download the ZIP, execute release code, change repository/client setup, approve tools, or apply an update. Existing installations gain this behavior only after one explicit verified installation of a release that contains the checker.

Do not upgrade files in place, and do not treat the release-update record digest as install approval. Download and verify the new manifest and bundle separately, inspect the old installation with its trusted installer, create and accept its rollback plan, and roll it back. Only after rollback, create a fresh install plan with the new release, review and accept that exact plan digest, and apply it. A pre-rollback new install plan would contain conflicts and cannot remain canonical after rollback.

Among Anthropic clients, version 1 supports Claude Code only; Claude chat/web/Desktop custom connectors require authless or OAuth remote-connector authentication, which this bearer-only endpoint does not provide.
