# Client capabilities

Capabilities reviewed: **2026-08-23**.

This matrix separates capabilities documented by each client provider from the configuration that the Acceptora installer currently generates. Provider documentation establishes that a client supports the required extension points; it does not prove that a particular Acceptora installation was discovered. Run the post-install client smoke checks for that proof.

The recorded builds are Acceptora configuration-review baselines, not claims about each provider's latest release. Codex CLI 0.144.0 is the only pinned minimum because the generated Codex MCP entry depends on its per-server `default_tools_approval_mode = "writes"` policy. No minimum build is currently claimed for Claude Code or Gemini CLI. The Gemini baseline is the official stable [v0.56.0 release](https://github.com/google-gemini/gemini-cli/releases/tag/v0.56.0), where `/mcp reload` is the primary interactive command and `/mcp refresh` remains an alias.

Machine-readable source of truth in the source distribution: `config/client-profiles.json`. Update the registry and this dated review together whenever a client path, event, template, or supported capability changes.

## Provider capabilities

| Client | Documented capability used by Acceptora | Official documentation |
| --- | --- | --- |
| Codex | Repository skills, lifecycle command hooks, and URL-based MCP with bearer-token environment binding | [Skills](https://developers.openai.com/codex/skills), [hooks](https://developers.openai.com/codex/hooks), [MCP](https://developers.openai.com/codex/mcp), [configuration](https://developers.openai.com/codex/config-reference) |
| Claude Code | Project skills, lifecycle command hooks, and remote HTTP MCP with environment-expanded headers | [Skills](https://code.claude.com/docs/en/skills), [hooks](https://code.claude.com/docs/en/hooks), [MCP](https://code.claude.com/docs/en/mcp), [settings](https://code.claude.com/docs/en/settings) |
| Gemini CLI | Workspace skills, lifecycle command hooks, and Streamable HTTP MCP through `httpUrl` with headers | [Skills](https://geminicli.com/docs/cli/skills/), [hooks](https://geminicli.com/docs/hooks/), [MCP](https://geminicli.com/docs/tools/mcp-server/), [configuration](https://geminicli.com/docs/reference/configuration/) |

## Trust and reload notes

- **Codex:** repository skills use `.agents/skills`; user MCP and hook configuration use `.codex`. Inspect `/hooks` and review changed hook definitions, and use `/mcp` or `codex mcp list` to inspect the server.
- **Claude Code:** creating a previously absent top-level project skills directory can require a restart. Workspace trust applies before project settings hooks run; use `/hooks` and `/mcp` or `claude mcp list` for inspection.
- **Gemini CLI:** workspace skills require activation consent. Use `/skills reload`, `/mcp reload` or `gemini mcp list`, and `/hooks panel`; restart after `mcpServers` configuration changes. Keep `trust: false` so MCP tools are not silently trusted.

## Acceptora-generated defaults

| Client | Recorded build | Project files | User-scope hooks and MCP | Acceptora lifecycle |
| --- | --- | --- | --- | --- |
| Codex | Reference `codex-cli 0.147.0`; minimum `codex-cli 0.144.0` | Skill at `.agents/skills/verify-generated-work`; managed instructions in `AGENTS.md` | Hooks in `~/.codex/hooks.json`; MCP in `~/.codex/config.toml` using `url`, `bearer_token_env_var = "ACCEPTORA_AGENT_TOKEN"`, and `default_tools_approval_mode = "writes"` | Baseline on `SessionStart` and `UserPromptSubmit`; completion gate on `Stop` |
| Claude Code | Reference `2.1.114`; no pinned minimum | Skill at `.claude/skills/verify-generated-work`; managed instructions in `CLAUDE.md` | Hooks in `~/.claude/settings.json`; MCP in `~/.claude.json` using `type: "http"`, `url`, and `Authorization: Bearer ${ACCEPTORA_AGENT_TOKEN}` | Baseline on `SessionStart` and `UserPromptSubmit`; completion gate on `Stop` |
| Gemini CLI | Reference `0.56.0`; no pinned minimum | Skill at `.gemini/skills/verify-generated-work`; managed instructions in `GEMINI.md` | Hooks and MCP in `~/.gemini/settings.json` using `httpUrl`, `Authorization: Bearer ${ACCEPTORA_AGENT_TOKEN}`, `timeout: 600000`, and `trust: false` | Baseline on `SessionStart` and `BeforeAgent`; completion gate on `AfterAgent` |

For every client, `SessionStart` also performs the bounded, credential-free update check against the canonical repository's `main` branch. It reports an available commit without downloading source, editing setup, granting client trust, or applying an update.

## Post-install discovery

| Client | Check after installation |
| --- | --- |
| Codex | Inspect `/skills`; use `/mcp` or `codex mcp list`; inspect `/hooks` and review changed hook definitions. |
| Claude Code | Inspect `/skills` and `/hooks`; use `/mcp` or `claude mcp list`. |
| Gemini CLI | Run `/skills reload`; use `/mcp reload` or `gemini mcp list`; inspect `/hooks panel`; restart after `mcpServers` changes. |

The installer merges these defaults into user-scope configuration but does not approve hooks, grant MCP trust, or prove that the client enforced an approval prompt. Those decisions and the disposable end-to-end smoke test remain user-controlled.
