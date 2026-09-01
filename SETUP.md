# Acceptora agent setup

Run one command from the target Git worktree root:

```text
npx --yes acceptora-agent-skill install --client <codex|claude-code|gemini-cli>
```

The installer performs only the setup work required for that project:

1. resolve the Git root and selected client;
2. obtain the project key from an existing project-scoped variable or a hidden prompt;
3. validate the key and required scopes with Acceptora before any write;
4. copy the packaged skill, add one managed instruction to the client's project instruction file, configure the project MCP server, and record project-local ownership state.

The key identifies the project. Do not provide a project ID or Acceptora origin.

## Missing project key

The variable name is derived from the validated key as `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>`. Different projects therefore use different variable names.

When an existing ignored and untracked project environment file is available, the installer never reads or writes it. It reports the derived variable name and file path, then stops without a partial installation. Add the key through the project's existing environment-file format, restart the client through that environment loader, and rerun the same install command.

When no project environment file exists on Windows, the installer explains the current-user environment scope and asks before storing the validated key. Restart the client and rerun the command. On other systems, configure the derived variable through the project's established secret loader.

Never put the key in a command argument, committed file, log, or shared client configuration.

## Installed project files

- Codex: `.agents/skills/acceptora`, `AGENTS.md`, `.codex/config.toml`
- Claude Code: `.claude/skills/acceptora`, `CLAUDE.md`, `.mcp.json`
- Gemini CLI: `.gemini/skills/acceptora`, `GEMINI.md`, `.gemini/settings.json`
- Shared project binding and ownership: `.acceptora/config.json`, `.acceptora/install-manifest.json`

Unrelated project instructions and client settings are preserved. Restart the selected client after a completed install or update.

## Lifecycle commands

```text
npx --yes acceptora-agent-skill doctor
npx --yes acceptora-agent-skill update
npx --yes acceptora-agent-skill uninstall
```

`doctor` validates the installed project binding and reports update or drift state. `update` replaces only installer-owned payload after validating the bound key and ownership manifest. `uninstall` requires no credential or network access, preserves unrelated project configuration, and never removes the stored project key.
