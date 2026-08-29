# Getting started

You will end this tutorial with the Acceptora Agent Skill installed in a trusted Git worktree, the MCP server discovered by your coding agent, and the completion path ready. Total time: about ten minutes.

[SETUP.md](SETUP.md) remains the security specification. This page is the human path; coding agents follow SETUP.md.

## What you'll have

- The `acceptora` skill in your client
- Acceptora MCP tools available to the agent
- Completion hooks that ask the agent to synchronize a checklist after eligible work
- A project marked connected only after the authenticated health check passes

No agent operation can make a human verification decision or grant final acceptance.

## Prerequisites

- Codex CLI 0.144.0 or newer, Claude Code, or Gemini CLI with a Standard/Enterprise license, supported paid API key, or Vertex AI authentication
- Python 3.11 or newer and Git, installed outside the target repository
- An Acceptora project and a short-lived project credential
- The credential exported as `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` in the coding-agent process environment, using the uppercase project ID shown by Acceptora, never in a prompt, URL, plan, or file
- A trusted Git worktree you own; do not use this on untrusted forks or pull requests

## How it works

The skill is one command with three jobs:

```text
$acceptora            # default: synchronize the checklist
$acceptora init       # install, reconnect, or update
$acceptora doctor     # diagnose without changing files
```

If you only remember one sequence, make it this:

1. Export `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` in your coding agent, replacing `PROJ_<ULID>` with the uppercase project ID.
2. Paste the project-specific onboarding prompt into that agent from the repository you want to connect. Keep the prompt's explicit `client` value.
3. Approve the exact installation-plan SHA-256 when the agent stops.
4. Start a real task in Codex, Claude Code, or Gemini CLI. Its prompt hook fetches fresh project-owner verification guidance and requires the trusted isolated reader before repository analysis. When the work finishes, the completion hook runs `$acceptora` even if you do not type it.

Codex discovers the skill from `/skills` or `$acceptora`. Claude Code uses `/acceptora`. Gemini CLI uses `/skills`. After install, reload the client if the skill, MCP server, or hooks are missing.

Do not choose Antigravity CLI for a new Acceptora installation or upgrade. In a real headless Windows smoke, Antigravity CLI `1.1.22` loaded `hooks.json` but did not dispatch `PreInvocation` or `Stop`, so the required task-start and completion lifecycle is unavailable. Existing Antigravity receipts are supported only for status and rollback. Google no longer serves Gemini CLI requests for free-tier, Google AI Pro, or Google AI Ultra individual accounts; use Codex, Claude Code, or a Gemini CLI environment with a Gemini Code Assist Standard or Enterprise license, supported paid API key, or Vertex AI authentication. See Google's [consumer-account deprecation](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals), [transition announcement](https://developers.googleblog.com/en/an-important-update-transitioning-gemini-cli-to-antigravity-cli/), and [migration guide](https://antigravity.google/docs/cli/gcli-migration/).

## Step 1. Create the credential

In Acceptora, open the project, then **Settings > Connection**. Create a project credential with the seven normal scopes listed in [SETUP.md](SETUP.md#agent-client-prerequisites).

Export it only in the coding-agent environment:

```text
ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>
```

For example, project `proj_01ARZ3NDEKTSV4RRFFQ69G5FAV` uses `ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV`. The installation plan prints the exact name and never reads its value. The same Acceptora project uses the same name in every local repository and supported client; different projects use different names. Install once per target Git worktree: each client/worktree pair binds to one Acceptora project, while target-specific MCP aliases and hooks allow several repositories and projects to coexist in shared user configuration. The onboarding prompt never contains the secret. If the variable is missing, stop and set it outside the conversation. Do not paste the value into chat.

## Step 2. Install

From the signed-in Acceptora onboarding page, copy the prompt for Codex, Claude Code, or Gemini CLI. Paste it into that client **from the Git repository you want to connect**.

The agent will:

1. Fresh-clone `https://github.com/Elvesora/acceptora-agent-skill` at `main` into a temporary directory **outside the target worktree**, or use the intact ZIP from that onboarding page extracted the same way.
2. Read [SETUP.md](SETUP.md) and create a non-mutating installation plan.
3. Stop for your explicit approval of the exact plan SHA-256.
4. Apply only that plan, check client discovery, then run the pinned health check with `--confirm-connection`.

Keep `acceptora-agent-skill-provenance.json` beside the extracted `acceptora` directory if you use the ZIP. Do not extract the archive into the target project root, and do not run the installer from inside the target repository.

You approve the digest, client trust, hooks, and MCP tool permissions. The agent must not approve those on your behalf.

To run the installer yourself from a fresh source checkout outside the target worktree:

```text
"<absolute-python>" -B -I "<source-directory>/scripts/install.py" plan --client "<codex|claude-code|gemini-cli>" --target-root "<absolute-git-worktree-root>" --project-id "<proj_ULID>" --api-base-url "<canonical-https-origin>" --format text --output "<external-path>/acceptora-install-plan.json"
```

Pass one of the three supported `--client` values explicitly. `--format text` prints the human plan, including the Plan SHA-256 and the exact `apply` command. `--output` still writes the JSON file that `apply` requires. The installer does not change files until you pass that exact digest to `apply`. Do not use `npx` or npm as an install source, and do not create or apply an `antigravity-cli` plan.

## Step 3. Confirm it works

After apply, the client should show the skill, the Acceptora MCP server, and the managed hooks. The health check reports `connection_confirmation.status: confirmed` only when every contract check passed.

If anything is missing, run:

```text
$acceptora doctor
```

`doctor` does not change files and does not mark the project connected. For Codex, Claude Code, or Gemini CLI, incomplete setup routes to `init`; an Antigravity receipt reports unsupported and routes only to rollback.

## Step 4. Verify something

In a supported client, make an observable change in the connected repository, or wait until the agent finishes work it already started. At the completion boundary the hook asks the agent to synchronize a checklist.

You can also invoke the default command yourself in that supported client:

```text
$acceptora
```

Expect a compact result with a real feature URL, or a visible recovery artifact if synchronization failed. A synchronized checklist is not human-accepted.

## What to try next

- Keep working in the same repository. Later eligible changes update the same feature instead of creating a disconnected document.
- When Acceptora feedback arrives, ask the agent to address it. It will load the feedback workflow from the skill.
- When a task-start update notice says a newer `main` commit exists, run `$acceptora init` and follow the update path in [SETUP.md](SETUP.md#post-install-and-upgrades). Do not `git pull` inside the installed runtime.

## Common issues

- **The agent says the token is missing.** Export the exact project-derived variable printed by the plan in the coding-agent process environment, then retry. Never put the value in the prompt.
- **The skill, MCP server, or hooks do not appear.** Reload the client. Codex: `/skills`, `/mcp` or `codex mcp list`, `/hooks`. Claude Code: `/skills`, `/mcp` or `claude mcp list`, `/hooks`. Gemini CLI: `/skills reload`, `/mcp reload` or `gemini mcp list`, `/hooks panel`. Then run `$acceptora doctor`.
- **You have an existing Antigravity receipt.** Do not use it for new work, reconnect it, or upgrade it. Use its trusted installer only for `status`, `rollback-plan`, and an explicitly approved `rollback`, then install a supported client separately.
- **Gemini CLI says the individual client is no longer supported.** Do not migrate that installation to Antigravity. Use Codex, Claude Code, or a supported Gemini authentication environment.
- **Install stopped before any files changed.** That is expected. Review the plan and accept the exact `plan_sha256` only if the source commit, paths, and file operations are the ones you want.
- **The project is not marked connected.** `doctor` ran a read-only health check. Finish `$acceptora init` so the pinned health check can use `--confirm-connection`.
- **A completion hook warned that verification is unsynchronized.** The default `$acceptora` command is the completion workflow. Do not skip it because tests passed.
- **You need the exact commands.** [SETUP.md](SETUP.md) is authoritative for plan, apply, rollback, health, and recovery.

## REST-only integrations

Applications that call Acceptora over HTTP do not need this skill. Use the public OpenAPI document and [references/rest-api-contract.md](references/rest-api-contract.md). The project-derived `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` convention is specific to installed agent clients.
