# Acceptora agent setup

Human tutorial: [GETTING-STARTED.md](GETTING-STARTED.md). Coding agents follow this file.

This skill supports Codex, Claude Code, and Gemini CLI through Streamable HTTP MCP or the contract-equivalent REST API in `references/rest-api-contract.md`. It can verify repositories in any programming language, framework, or mixed stack. Python and Git run the installed skill and source adapter; they are not target-application dependencies. Direct REST integrations can use any standards-compliant HTTP client and do not require an Acceptora SDK.

Use the dated [client capability matrix](references/client-capabilities.md) for reviewed provider support, generated configuration paths, lifecycle events, and post-install discovery checks. Its machine-readable source is `config/client-profiles.json`.

The canonical source and update authority is the production `main` branch of `https://github.com/Elvesora/acceptora-agent-skill`. Install from either a fresh checkout of that branch or the intact ZIP served by `https://www.acceptora.com/agent-skill/acceptora-agent-skill.zip`. The ZIP is generated from one clean `main` commit and is not an independently authored release source. Do not substitute another remote or mirror.

## Coding-agent install or update

Read this file completely before changing the target. For the normal onboarding-prompt path, treat the requested fresh checkout as the authority for the mechanical setup.

- When an Acceptora onboarding prompt supplies `client`, `target`, `project_id`, and `acceptora_origin`, use those inputs exactly; do not ask the user to repeat them.
- When a `SessionStart` update notice routes here, its printed cache path is `<runtime-root>/state/skill-update.json`. Use `<runtime-root>/package/scripts/install.py` as the existing trusted installer and `<runtime-root>/install-receipt.json` as the installed identity record. Read `client`, `target_root`, `project_id`, `api_base_url` (the `acceptora_origin`), and `runtime_base` from the receipt's `inputs`, then validate that receipt with the trusted installer's **Status and rollback** command before using those values. Stop if the paths or validation do not match.
- Resolve an onboarding `target: current Git worktree` to its absolute Git worktree root; for an update notice, use the validated receipt's `target_root`. Validate the fresh checkout against **Obtain the production source**, then satisfy **Agent client prerequisites**.
- Confirm `ACCEPTORA_AGENT_TOKEN` exists in the coding-agent process environment without printing, copying, or storing its value. If it is absent, stop and explain how the user can set it outside the conversation.
- For a new installation, follow **Plan**, show the full source commit with the plan review, and pause for explicit approval of the exact `plan_sha256`. Then follow **Apply**, **Client review**, **Status and rollback** (status only), and finally **Health check and connection confirmation**.
- For an update, first use the installed trusted installer to inspect status and create a rollback plan. Pause for explicit approval of the exact `rollback_plan_sha256`, apply that rollback, and only then create a new installation plan from this fresh checkout. Never reuse a plan created before rollback. Continue with the new-install sequence above.
- Perform the mechanical commands yourself, but never approve a plan digest, client trust, hooks, or MCP tool permissions on the user's behalf. Stop at every approval or missing prerequisite required by this guide.
- Report the source commit, receipt and runtime locations, installer status, client discovery result, health-check result, and explicit connection confirmation. Do not make unrelated project changes.

## Boundary

Use only a trusted repository and branch. The operating system, the current user account, and trusted administrators are part of the security boundary. The agent token is present in the client environment, so repository commands can inherit it. This version does not protect the token from malicious repository code or project-level client configuration. Do not use it on untrusted forks or pull requests. Use short-lived, least-scope credentials and inspect effective hooks/MCP configuration after branch changes.

The installer never reads the token, grants client trust, or approves tools. It changes files only after an exact installation-plan digest is supplied. No agent operation can make a human verification decision.

## Agent client prerequisites

These are installer and coding-agent runtime prerequisites only. Do not install Python, Git, PHP, Laravel, Composer, or an Acceptora SDK into the target application merely to use the verification workflow.

- A fresh clone of the canonical production `main` branch or an intact extracted canonical ZIP outside the target repository.
- The actual Git worktree root; subdirectory installs are rejected.
- A strict Git worktree with no submodules/gitlinks, unresolved index stages, `assume-unchanged` or `skip-worktree` entries, non-UTF-8 or non-portable paths, or special filesystem objects in the eligible source. The installer rejects unsupported state before mutation.
- Absolute Python 3.11+ and Git executables outside that worktree, with trusted owners and no untrusted write or replacement access along their path. Planning probes the selected Python in isolated mode and requires its reported executable identity to match.
- Codex CLI 0.144.0 or newer, or a current supported Claude Code or Gemini CLI release.
- The project ID and canonical HTTPS origin shown in Acceptora **Settings > Connection**.
- A project credential with the seven normal scopes: `projects:read`, `features:resolve`, `features:read`, `checklists:write`, `feedback:read`, `feedback:address`, and `gates:read`.
- Optional `exceptions:write` only when exact-source policy exceptions are required.

The source adapter always includes tracked files even when their paths match an ignore rule. It includes untracked files unless the repository's Git ignore rules or an explicitly reviewed `.verification/config.json` `ignored_paths` entry excludes them. The installed default is an empty project-specific ignore list; add exclusions only for generated state that cannot affect the observable deliverable.

Export the agent credential as `ACCEPTORA_AGENT_TOKEN`. Never write its value to a file, URL, prompt, plan, or log.

## REST-only prerequisites

A direct REST integration does not need the skill repository, an agent client, Git, Python, or the fixed agent token environment-variable name. It needs:

- The project ID and canonical HTTPS origin shown in Acceptora **Settings > Connection**.
- A bearer credential loaded from any secure secret provider, with the seven normal scopes listed above and optional `exceptions:write` only when required.
- An HTTP client that preserves `Authorization`, correlation, idempotency, and retry headers without following credential-bearing cross-origin redirects.
- The public OpenAPI document at `<canonical-origin>/api/v1/integrations/openapi.json` as the request and response schema authority.

## Obtain the production source

The preferred coding-agent path creates a fresh checkout outside the target repository. The installer records the full commit in its plan, pinned runtime, receipt, and status output.

```text
git clone --depth 1 --branch main --single-branch https://github.com/Elvesora/acceptora-agent-skill "<external-temporary-directory>/acceptora-agent-skill"
git -C "<external-temporary-directory>/acceptora-agent-skill" rev-parse HEAD
```

The checkout's `origin` must resolve to the canonical repository. `main` is the production channel; semantic package versions describe compatibility but do not select updates.

For the downloadable alternative, get both public artifacts from the canonical Acceptora origin:

- `https://www.acceptora.com/agent-skill/acceptora-agent-skill.zip`
- `https://www.acceptora.com/agent-skill/release-manifest.json`

The response header `X-Acceptora-Artifact-SHA256` and external manifest bind the served bytes. Extract the entire ZIP outside the target repository. Keep the top-level `acceptora-agent-skill-provenance.json` beside the `acceptora` directory; do not move only the package directory. The installer refuses a missing, malformed, wrong-repository, wrong-branch, wrong-commit, or wrong-tree provenance record. Use the extracted `acceptora` directory as `<source-directory>` below. The external manifest binds the ZIP digest and size; the embedded record lets the extracted installer revalidate the canonical source commit and complete package identity before plan or apply.

## Plan

Write the plan outside the target worktree. Commands below use one-line PowerShell syntax: replace every angle-bracket placeholder, preserve the quotes around paths, and keep the leading `&`. On POSIX shells, remove only the leading `&`.

```text
& "<absolute-python>" -I "<source-directory>/scripts/install.py" plan --client "<codex|claude-code|gemini-cli>" --target-root "<absolute-git-worktree-root>" --project-id "<proj_ULID>" --api-base-url "<canonical-https-origin>" --python-executable "<absolute-python>" --git-executable "<absolute-git>" --format json --output "<external-path>/acceptora-install-plan.json"
```

`--client` may be omitted when the process environment or target worktree identifies exactly one supported coding agent (`CLAUDECODE` / `CLAUDE_CODE`, `CODEX_HOME` / `CODEX_THREAD_ID`, `GEMINI_CLI`, or a unique `.claude`, `.agents`/`.codex`, or `.gemini` directory). Conflicting or missing signals still require an explicit `--client`. `--format text` prints a human review of the same plan; `--output` always stores the JSON document that `apply` requires. The installer never applies a plan until `apply` receives that exact `plan_sha256`.

Optional `--runtime-base` and `--client-config-dir` paths must remain outside the worktree and inside the current user's verified home directory. They may not cross a symlink or junction, use an untrusted owner, or permit another local account to write or replace a path in the chain; an existing runtime base must already be private to the current user. Review the full source commit, target/runtime/config paths, pinned executables, origin, project ID, fixed token name, per-target MCP alias, every file operation and digest, conflicts, and warnings. The plan is non-mutating and contains no token value.

## Apply

Only after the user explicitly accepts the exact `plan_sha256`:

```text
& "<absolute-python>" -I "<source-directory>/scripts/install.py" apply --plan "<external-path>/acceptora-install-plan.json" --accept-plan-sha256 "<exact-plan-sha256>" --format json
```

Apply reconstructs the canonical plan and fails if source, inputs, or target state changed. Save the returned `trusted_installer`, `runtime_root`, `runtime_base`, receipt, client, and target root. Use only that external `trusted_installer` for later lifecycle commands.

## Client review

The commands below are the post-install checks summarized in the dated [client capability matrix](references/client-capabilities.md).

- Codex: inspect `/skills`; use `/mcp` or `codex mcp list`; inspect `/hooks` and review changed hook definitions. Repeat the MCP and hook checks after branch or configuration changes.
- Claude Code: inspect `/skills` and `/hooks`; use `/mcp` or `claude mcp list`. Restart if a previously absent top-level project skills directory was created.
- Gemini CLI: run `/skills reload`; use `/mcp reload` or `gemini mcp list`; inspect `/hooks panel`; restart after `mcpServers` changes. Automatic MCP trust is intentionally absent.

Hooks enforce the completion gate only while they remain enabled, unchanged, and trusted. Bounded loop protection fails open with a visible warning rather than retrying forever.

For Codex, the generated MCP entry uses `default_tools_approval_mode = "writes"`, the current per-server key documented by the [official Codex configuration reference](https://developers.openai.com/codex/config-reference). Codex CLI 0.144.0 is the minimum supported release for this value; earlier builds can reject or ignore the policy. Treat it as client-side approval policy, not server authorization or proof that a particular client build enforced a prompt. Keep Codex current and confirm the expected write-tool prompt during the disposable post-install smoke test.

## Status and rollback

```text
& "<absolute-python>" -I "<trusted-installer>" status --client "<client>" --target-root "<absolute-git-worktree-root>" --format json

& "<absolute-python>" -I "<trusted-installer>" rollback-plan --client "<client>" --target-root "<absolute-git-worktree-root>" --format json --output "<external-path>/acceptora-rollback-plan.json"

& "<absolute-python>" -I "<trusted-installer>" rollback --plan "<external-path>/acceptora-rollback-plan.json" --accept-rollback-plan-sha256 "<exact-rollback-plan-sha256>" --format json
```

Repeat the original `--runtime-base` on `status` and `rollback-plan` if it was customized. Review every rollback removal and its exact digest. Changed owned content causes rollback to stop.

## Health check and connection confirmation

After apply, client review, and a successful installer status check, run this as the final setup step:

```text
& "<absolute-python>" -I "<runtime-root>/package/scripts/health_check.py" --confirm-connection --format json
```

Do not pass repository `.verification/config.json`. The command first verifies project identity and lifecycle, canonical endpoints, versions, all seven normal workflow scopes, OpenAPI, MCP identity, the exact eight tools, approval annotations, and every input/output schema digest. Only after all those checks pass does it send an exact empty JSON object to the authenticated `POST /api/v1/integrations/connection/confirm` endpoint for the pinned project. The endpoint independently requires all seven normal workflow scopes. This setup-only REST operation marks the connection established; it is not a ninth MCP tool and it invokes no product write tool. A successful result has `connection_confirmation.status: confirmed` and identifies the confirmed project without exposing the credential.

For later read-only compatibility diagnostics, omit `--confirm-connection`:

```text
& "<absolute-python>" -I "<runtime-root>/package/scripts/health_check.py" --format json
```

The diagnostic form never sends the confirmation request and never marks the project connected. Both forms may update ordinary credential-use telemetry and never print the credential.

## REST-only clients

A direct REST client may load the bearer token from any secure secret provider; the `ACCEPTORA_AGENT_TOKEN` name is specific to installed agent clients. Start with the unauthenticated OpenAPI document, then authenticate `GET /api/v1/integrations/project` before writes. The required normal sequence is project preflight, `resolve_feature`, `get_feature_context`, `reconcile_checklist`, `check_completion_gate`, and `get_verification_status`. Feedback work adds `get_verification_feedback` and `address_feedback` before a full reconciliation. Use the exact OpenAPI schemas and read `references/rest-api-contract.md`.

Use the target environment's native HTTP library or an OpenAPI-generated client in any language. Framework-specific SDKs are optional conveniences, never contract authorities or prerequisites.

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

On `SessionStart`, the installer-owned runtime checks a five-minute cache and runs at most one bounded, credential-free `git ls-remote --exit-code --heads` against the canonical repository's exact `refs/heads/main` during that interval. It compares the returned full commit with the commit recorded by either supported installation path. The external runtime cache stores only the commit identity or a generic failure status. A baseline-capture warning does not suppress this separate update check. A different valid commit produces a non-blocking client message with clear coding-agent update instructions. The hook does not fetch source, change repository/client setup, approve tools, or apply an update.

Do not run `git pull` inside the installed runtime, and do not treat the update-cache record digest as install approval. A pull changes only a source checkout; it does not synchronize the installer-owned copied runtime, reconstruct the installation plan, or prove those copied files match the resulting commit. A pull can also retain local commits through merge/rebase behavior. Ask the coding agent to clone a fresh production `main` checkout outside the target repository. Inspect the old installation with its trusted installer, create and accept its rollback plan, and roll it back. Only after rollback, create a fresh install plan with the new checkout, review and accept that exact plan digest, apply it, verify client discovery and installer status, and run the pinned health check with explicit connection confirmation. A pre-rollback new install plan would contain conflicts and cannot remain canonical after rollback.

Among Anthropic clients, version 1 supports Claude Code only; Claude chat/web/Desktop custom connectors require authless or OAuth remote-connector authentication, which this bearer-only endpoint does not provide.
