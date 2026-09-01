# Acceptora agent setup

Run one command from the target Git worktree root:

```text
npx --yes acceptora-agent-skill install --client <codex|claude-code|gemini-cli>
```

The installer performs only the setup work required for that project:

1. resolve the Git root and selected client;
2. obtain the project key from `.acceptora-env` or a hidden prompt;
3. validate the key and required scopes with Acceptora before any write;
4. store `ACCEPTORA_PROJECT_TOKEN=<project-key>` in `.acceptora-env`, copy the packaged skill, configure the project MCP server, and record project-local ownership state.

The key identifies the project. Do not provide a project ID or Acceptora origin.

## Missing project key

When `.acceptora-env` is absent or does not contain the key, the installer asks once, validates the key, creates or updates the file, and finishes installation in the same command. Do not rerun install.

Add `/.acceptora-env` to `.gitignore`. Never put the key in a command argument, committed file, log, or shared client configuration.

## Installed project files

- Codex: `.agents/skills/acceptora`, `AGENTS.md`, `.codex/config.toml`
- Claude Code: `.claude/skills/acceptora`, `CLAUDE.md`, `.mcp.json`
- Gemini CLI: `.gemini/skills/acceptora`, `GEMINI.md`, `.gemini/settings.json`
- Shared project binding and ownership: `.acceptora/config.json`, `.acceptora/install-manifest.json`
- Project key: `.acceptora-env`

Unrelated project instructions and client settings are preserved. Restart the selected client after a completed install or update.

## Lifecycle commands

```text
npx --yes acceptora-agent-skill doctor
npx --yes acceptora-agent-skill update
npx --yes acceptora-agent-skill uninstall
```

`doctor` validates the installed project binding and reports update or drift state. `update` replaces only installer-owned payload after validating the bound key and ownership manifest. `uninstall` requires no credential or network access, preserves unrelated project configuration, and never removes the stored project key.
