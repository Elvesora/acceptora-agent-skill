# Doctor: diagnose a live install

`doctor` reports whether this worktree's Acceptora skill, MCP server, hooks, and authenticated contract are usable in Codex, Claude Code, or Gemini CLI. An Antigravity receipt always reports `unhealthy` with lifecycle `unsupported`, even when installer status, configuration visibility, MCP, or contract health passes. `doctor` does not install, apply, roll back, approve trust, or mark the project connected.

If diagnosis shows a missing, broken, or stale supported-client install, tell the user to run `$acceptora init`. For Antigravity, recommend the existing receipt's digest-bound rollback path instead; do not recommend reinstalling or upgrading it. Do not start `init` as a side effect of `doctor`.

## When this owns the turn

Follow this file when the user invoked `doctor` or `status`, or asked why the skill, MCP server, hooks, or connection is missing, unhealthy, or out of date.

Completion hooks and ordinary finished work in Codex, Claude Code, or Gemini CLI still use the completion workflow in [SKILL.md](../SKILL.md). Installing or updating a supported client uses [init.md](init.md).

## Rules

- Never print, copy, or log the project-derived `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` value or any credential substring.
- Never pass `--confirm-connection`. That flag is the last step of `init`, not a diagnostic.
- Never run `apply`, `rollback`, or `rollback-plan` from this command.
- Load [client-capabilities.md](client-capabilities.md) before using client-specific discovery commands.

## 1. Credential presence

Read the exact credential variable name from the validated installed runtime configuration. Confirm the process environment contains that project-derived name without reading its value into chat, files, or logs. If it is absent, report that and stop; the user must export it outside the conversation. Skip this check for an existing Antigravity receipt because only local status and rollback remain supported.

## 2. Installer identity

If a supported client's task-start update notice printed a runtime cache path, treat `<runtime-root>/package/scripts/install.py` as the trusted installer and `<runtime-root>/install-receipt.json` as the identity record. Otherwise look for the receipt the last successful `init` reported.

Validate the receipt with the trusted installer **before** using its `client`, `target_root`, `project_id`, `api_base_url`, or `runtime_base`. Stop if those values do not match this worktree.

If there is no trusted installer or receipt, report that Acceptora is not installed here. Recommend `$acceptora init` only for Codex, Claude Code, or Gemini CLI. If the active client is Antigravity, report lifecycle `unsupported` and do not clone, plan, reconnect, or upgrade from `doctor`.

## 3. Installer status

From [SETUP.md](../SETUP.md) **Status and rollback**, run only `status`:

```text
& "<absolute-python>" -B -I "<trusted-installer>" status --client "<client>" --target-root "<absolute-git-worktree-root>" --format json
```

Repeat a customized `--runtime-base` if the receipt recorded one. Report whether the owned files still match. If status fails or owned content changed for a supported client, recommend `$acceptora init` rather than repairing files by hand.

If the validated receipt records `client: antigravity-cli`, report `Acceptora doctor: unhealthy` and `Lifecycle: unsupported` regardless of the status result. A real Antigravity CLI `1.1.22` headless Windows smoke loaded `hooks.json` but dispatched neither `PreInvocation` nor `Stop`; `/hooks` visibility and direct command execution are not lifecycle proof. Skip health, discovery, and stale-source diagnosis because this receipt is retained only for status and rollback. Recommend that the user follow [SETUP.md](../SETUP.md) to create, review, explicitly approve, and apply the receipt's rollback plan, then install Codex, Claude Code, or Gemini CLI separately.

## 4. Read-only health

Run the pinned external health check and omit `--confirm-connection`:

```text
& "<absolute-python>" -B -I "<runtime-root>/package/scripts/health_check.py" --format json
```

Do not pass repository `.verification/config.json`. Report the check names and outcomes. A successful diagnostic does not mark the project connected. If health fails with version drift, missing scopes, MCP mismatch, or project identity errors, quote the stable error code and recommend `init` only when a new plan is actually required.

## 5. Client discovery

Use the discovery checks in [client-capabilities.md](client-capabilities.md) for the installed client. Typical surfaces:

- Codex: `/skills`; `/mcp` or `codex mcp list`; `/hooks`
- Claude Code: `/skills`; `/mcp` or `claude mcp list`; `/hooks`
- Gemini CLI: `/skills reload`; `/mcp reload` or `gemini mcp list`; `/hooks panel`

For Codex, Claude Code, and Gemini CLI, report whether the skill, Acceptora MCP server, and managed hooks are visible. Missing discovery after a successful status check is a client reload or trust problem, not a reason to rewrite the plan. Do not apply this inference to Antigravity: visible hook entries are known not to prove dispatch.

## 6. Stale source

If a supported client's task-start update notice reported a different valid `main` commit, say the installed commit is behind production `main` and recommend `$acceptora init` so the user can follow the update path. Do not fetch, pull, or apply an update from `doctor`.

## Report

Use this compact shape:

```text
Acceptora doctor: <ok | not installed | unhealthy | stale>
Client: <codex | claude-code | gemini-cli | antigravity-cli>
Lifecycle: <supported | unsupported>
Installer status: <pass | fail | missing>
Health: <pass | fail | missing>
Discovery: skill <yes/no>, MCP <yes/no>, hooks <yes/no>
Token: <present | missing>
Next: <none | $acceptora init | reload the client | export the reported project-derived variable | review and approve rollback>
```

Do not claim the project is human-verified. Do not claim it is connected unless a prior supported-client `init` health check with `--confirm-connection` already reported `connection_confirmation.status: confirmed` and this diagnostic still passes. Never report an Antigravity receipt as `ok` or lifecycle-supported.
