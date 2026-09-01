# Acceptora Agent Skill

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Acceptora connects a coding agent to a project-scoped manual-verification workflow. The skill reads fresh owner instructions, uses Acceptora through MCP or REST, and keeps verification steps synchronized with implementation changes. Human verification decisions remain human-only.

## What it does

- installs the packaged skill into one project and one supported client;
- validates and isolates each project's key;
- checks the published npm version for skill updates during the project preflight;
- rereads account and project instructions before work and before drafting manual verification steps; and
- uses MCP first, with the equivalent versioned REST API as fallback.

It does not install application dependencies, add an Acceptora SDK to the target, or mutate shared user configuration.

## Install

Create a project credential in Acceptora, open a terminal at the target Git worktree root, and run one command:

**Codex**

```text
npx --yes acceptora-agent-skill install --client codex
```

**Claude Code**

```text
npx --yes acceptora-agent-skill install --client claude-code
```

**Gemini CLI**

```text
npx --yes acceptora-agent-skill install --client gemini-cli
```

The installer asks for the project key immediately when no project-scoped variable is available, validates it with Acceptora before writing, and never includes the secret in the command.

## Project isolation

Every worktree is bound in `.acceptora/config.json` to one public project ID and one derived variable name:

```text
ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>
```

The key is authenticated before storage. If the project already has an untracked, Git-ignored environment store, installation stops and asks the user to place the validated key there. The existing project loader must expose it to a restarted client. If no such store exists, Windows can use the explicit validated current-user fallback; other platforms stop until the user chooses a project-local secret-loading mechanism.

Tokens are never written to Acceptora config or MCP config. Different projects keep different variable names and bindings.

## Installed files

The installer writes only project-local state:

- `.acceptora/config.json`;
- `.acceptora/install-manifest.json` with installer ownership hashes;
- `.agents/skills/acceptora`, `.claude/skills/acceptora`, or `.gemini/skills/acceptora`;
- one managed client-instruction line; and
- `.codex/config.toml`, `.mcp.json`, or `.gemini/settings.json` for the project-native MCP connection.

Unrelated instructions and client settings are preserved. An unmanaged conflicting `acceptora` MCP entry fails visibly instead of being overwritten.

## Verification instructions

Account and project owners can define guidance for:

- project analysis;
- manual verification wording and sequence; and
- test data, including safe seeded records, real test links, and concrete IDs.

The agent fetches the effective instructions before work and fetches them again immediately before it creates or revises manual steps. It never substitutes a stale local snapshot.

## MCP and REST

The project MCP endpoint is `https://www.acceptora.com/mcp`. REST exposes the same workflow operations and a live OpenAPI document, so any language can integrate without this skill. See [API and MCP](references/api-mcp.md).

## Package map

- [SKILL.md](SKILL.md): agent workflow and acceptance boundary
- [SETUP.md](SETUP.md): concise installation and credential behavior
- `cli/acceptora-agent-skill.mjs`: project-local install, update, doctor, and uninstall
- `scripts/project_context.py`: key validation, fresh instructions, and update preflight
- [references/api-mcp.md](references/api-mcp.md): transport and operation summary

## Validate the skill

Run `npm test` and the Codex skill quick validator against the repository root. Functional behavior is covered by executable tests; documentation wording is not a test contract.

## Support

See [SUPPORT.md](SUPPORT.md), [SECURITY.md](SECURITY.md), and [CONTRIBUTING.md](CONTRIBUTING.md).
