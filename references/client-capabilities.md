# Client capabilities

Capabilities reviewed: **2026-08-29**.

This matrix separates capabilities documented by each client provider from the configuration that the Acceptora installer currently generates. Provider documentation and visible configuration establish only that an extension point and generated entry exist; they do not prove that the client dispatches the lifecycle event. Run the supported client's post-install smoke checks for execution proof.

The recorded builds are Acceptora configuration-review baselines, not claims about each provider's latest release. Codex CLI 0.144.0 is the only pinned minimum because the generated Codex MCP entry depends on its per-server `default_tools_approval_mode = "writes"` policy. No minimum build is currently claimed for Claude Code or Gemini CLI. Antigravity configuration was reviewed against CLI `1.1.22`, but its lifecycle dispatch failed the real client smoke described below. The Gemini integration was reviewed against stable [v0.57.0](https://github.com/google-gemini/gemini-cli/releases/tag/v0.57.0), where `/mcp reload` is the primary interactive command and `/mcp refresh` remains an alias.

Google stopped serving Gemini CLI requests for free-tier, Google AI Pro, and Google AI Ultra individual accounts and directs those users to Antigravity. That authentication transition does not make Antigravity a supported Acceptora lifecycle client. Use Codex, Claude Code, or the Gemini CLI profile with a Gemini Code Assist Standard or Enterprise license, a supported paid API key, or Vertex AI. Sources: [Google's consumer-account deprecation](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals), [transition announcement](https://developers.googleblog.com/en/an-important-update-transitioning-gemini-cli-to-antigravity-cli/), [Antigravity migration](https://antigravity.google/docs/cli/gcli-migration/), and [Gemini CLI authentication](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/authentication.mdx).

Machine-readable provider paths, events, and templates live in `config/client-profiles.json`. This dated matrix is authoritative for whether the generated integration is operationally supported. Update both together whenever a client path, event, template, or supported capability changes.

## Operational support

Codex, Claude Code, and Gemini CLI remain supported. Antigravity CLI is unsupported for new installation, reconnection, upgrade, and project work. A real headless Windows smoke on `1.1.22` confirmed that the client loaded `hooks.json` but dispatched neither `PreInvocation` nor `Stop`; therefore Acceptora's mandatory task-start preflight, update check, and completion gate did not run. The generated commands work when invoked directly, but that is adapter coverage, not client dispatch coverage.

Registry reason: Antigravity CLI 1.1.22 does not reliably dispatch PreInvocation and Stop hooks in real client runs, so automatic task-start and completion-gate enforcement cannot be guaranteed.

No other Antigravity platform/build currently has verified reliable execution of both required events. The upstream reports cover hooks not firing on Windows and macOS ([#222](https://github.com/google-antigravity/antigravity-cli/issues/222)), unreliable Linux/WSL and print-mode behavior ([#395](https://github.com/google-antigravity/antigravity-cli/issues/395)), and the missing reliable cross-platform hook surface ([#528](https://github.com/google-antigravity/antigravity-cli/issues/528)). Keep an existing Antigravity receipt only for `status`, `rollback-plan`, and `rollback`; `/hooks` visibility is not execution proof.

## Provider capabilities

| Client | Provider-documented capability and Acceptora status | Official documentation |
| --- | --- | --- |
| Codex | Repository skills, lifecycle command hooks, and URL-based MCP with bearer-token environment binding | [Skills](https://developers.openai.com/codex/skills), [hooks](https://developers.openai.com/codex/hooks), [MCP](https://developers.openai.com/codex/mcp), [configuration](https://developers.openai.com/codex/config-reference) |
| Claude Code | Project skills, lifecycle command hooks, and remote HTTP MCP with environment-expanded headers | [Skills](https://code.claude.com/docs/en/skills), [hooks](https://code.claude.com/docs/en/hooks), [MCP](https://code.claude.com/docs/en/mcp), [settings](https://code.claude.com/docs/en/settings) |
| Antigravity CLI | Provider-documented workspace skills, `PreInvocation`/`Stop` command-hook schema, local stdio MCP processes, and user configuration under `~/.gemini/config`; live lifecycle dispatch is unsupported by Acceptora | [Plugins and skills](https://antigravity.google/docs/cli/plugins/), [hooks](https://antigravity.google/docs/hooks/), [MCP](https://antigravity.google/docs/cli/mcp/), [CLI reference](https://antigravity.google/docs/cli/reference/) |
| Gemini CLI | Workspace skills, lifecycle command hooks, and Streamable HTTP MCP through `httpUrl` with headers | [Skills](https://geminicli.com/docs/cli/skills/), [hooks](https://geminicli.com/docs/hooks/), [MCP](https://geminicli.com/docs/tools/mcp-server/), [configuration](https://geminicli.com/docs/reference/configuration/) |

## Trust and reload notes

- **Codex:** repository skills use `.agents/skills`; user MCP and hook configuration use `.codex`. Inspect `/hooks` and review changed hook definitions, and use `/mcp` or `codex mcp list` to inspect the server.
- **Claude Code:** creating a previously absent top-level project skills directory can require a restart. Workspace trust applies before project settings hooks run; use `/hooks` and `/mcp` or `claude mcp list` for inspection.
- **Antigravity CLI:** an existing receipt may contain a workspace skill under `.agents/skills` and owned entries in `~/.gemini/config/hooks.json` and `~/.gemini/config/mcp_config.json`. The retained registry discovery label is `/mcp or agy mcp list`. Use its trusted installer only for status and rollback, and do not interpret `/skills`, `/hooks`, or MCP visibility as lifecycle execution proof.
- **Gemini CLI:** workspace skills require activation consent. Use `/skills reload`, `/mcp reload` or `gemini mcp list`, and `/hooks panel`; restart after `mcpServers` configuration changes. Keep `trust: false` so MCP tools are not silently trusted.

## Acceptora-generated defaults

| Client | Recorded build | Project files | User-scope hooks and MCP | Acceptora lifecycle |
| --- | --- | --- | --- | --- |
| Codex | Reference `codex-cli 0.150.1`; minimum `codex-cli 0.144.0` | Skill at `.agents/skills/acceptora`; managed instructions in `AGENTS.md` | Hooks in `~/.codex/hooks.json`; MCP in `~/.codex/config.toml` using `url`, project-derived `bearer_token_env_var = "ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>"`, and `default_tools_approval_mode = "writes"` | Instruction fetch and model-visible reader context before baseline on `SessionStart`/`UserPromptSubmit`; prompt preflight fails closed; completion gate on `Stop` |
| Claude Code | Reference `2.1.114`; no pinned minimum | Skill at `.claude/skills/acceptora`; managed instructions in `CLAUDE.md` | Hooks in `~/.claude/settings.json`; MCP in `~/.claude.json` using `type: "http"`, `url`, and project-derived `Authorization: Bearer ${ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>}` | Instruction fetch and model-visible reader context before baseline on `SessionStart`/`UserPromptSubmit`; prompt preflight fails closed; completion gate on `Stop` |
| Antigravity CLI | Reference `1.1.22`; **unsupported** | Legacy generated skill, instructions, hooks, and MCP entries may remain in an existing receipt for rollback | Existing receipts may own `~/.gemini/config/hooks.json` and `~/.gemini/config/mcp_config.json` entries; do not create or upgrade them | Real client smoke loaded the hook configuration but dispatched neither `PreInvocation` nor `Stop`; task-start preflight, update checks, and completion gating are unavailable |
| Gemini CLI | Reference `0.57.0`; no pinned minimum | Skill at `.gemini/skills/acceptora`; managed instructions in `GEMINI.md` | Hooks and MCP in `~/.gemini/settings.json` using `httpUrl`, project-derived `Authorization: Bearer ${ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>}`, `timeout: 600000`, and `trust: false` | Instruction fetch and model-visible reader context before baseline on `SessionStart`/`BeforeAgent`; `BeforeAgent` preflight denies on failure; completion gate on `AfterAgent` |

For each supported client, the task-start hook writes instruction bodies only to a private installer-owned external snapshot and emits the fixed trusted-reader directive through `hookSpecificOutput.additionalContext`. The bounded, credential-free update check runs on `SessionStart` for Codex, Claude Code, and Gemini CLI. It reports an available commit without downloading source, editing setup, granting client trust, or applying an update.

Each supported installation owns one target/client runtime identity and one target-specific MCP alias. Several repositories and Acceptora projects can coexist in the same user configuration, use separate project-derived token names, and be rolled back in any order. A single client/worktree pair remains bound to one Acceptora project until that installation is rolled back and reinstalled.

## Post-install discovery

| Client | Check after installation |
| --- | --- |
| Codex | Inspect `/skills`; use `/mcp` or `codex mcp list`; inspect `/hooks` and review changed hook definitions. |
| Claude Code | Inspect `/skills` and `/hooks`; use `/mcp` or `claude mcp list`. |
| Antigravity CLI | Unsupported: do not install or upgrade. For an existing receipt, run installer `status`, then follow the digest-bound rollback procedure. Visible entries do not prove hook dispatch. |
| Gemini CLI | Run `/skills reload`; use `/mcp reload` or `gemini mcp list`; inspect `/hooks panel`; restart after `mcpServers` changes. |

For supported clients, the installer merges these defaults into user-scope configuration but does not approve hooks, grant MCP trust, or prove that the client enforced an approval prompt. Those decisions and the disposable end-to-end smoke test remain user-controlled.
