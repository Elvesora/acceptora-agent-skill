# Acceptora agent setup

This procedure installs the project-local Acceptora skill and MCP connection for Codex, Claude Code, or Gemini CLI. It does not install application dependencies or mutate shared client configuration.

The onboarding prompt supplies only the client and current Git worktree. Do not ask for `project_id` or an Acceptora origin: the validated project key identifies the project, and the origin is always `https://www.acceptora.com`.

## 1. Obtain the source

Resolve the current target to its absolute Git root. Fresh-clone only canonical `main` outside that worktree:

```text
git clone --depth 1 --branch main https://github.com/Elvesora/acceptora-agent-skill.git <temporary-source>
```

Run setup commands from that checkout. Do not install from a tag archive, mirror, package already installed in another project, or a checkout inside the target.

## 2. Remove a legacy hook/runtime installation

If the target contains a legacy hook/runtime installation, stop. Use the trusted installer recorded by that installation and its installed rollback procedure. Show the exact rollback digest and wait for the user's approval before applying it.

Only after that rollback succeeds may a fresh installation begin. Do not overwrite or manually dismantle the legacy installation.

## 3. Project credential

The key is project-scoped. Its derived variable is `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>`, and each project gets a different name. Never use a generic default token, select a key from another worktree, print its value, or place it in committed files.

If a single project-derived Acceptora variable is already present in the current client process, use it. The installer validates the key before writing any project file. If several variables are present and this worktree is not already bound, ask the user which variable name belongs to the project and pass only that public name through `--token-env`; never guess.

### Missing key

Before choosing storage, inspect only root-level filenames and Git metadata. Never read, source, execute, diff, or print a possible secret file. A usable project environment store must be:

- an existing ordinary file inside the Git root, such as `.env`, `.env.local`, `.env.*.local`, `.dev.vars*`, or another established dotenv file;
- untracked by Git;
- ignored by Git; and
- neither an example/template file nor a link, junction, reparse point, or special file.

If several usable stores exist, ask which one the project actually loads. If a candidate is tracked, unignored, unsafe, or escapes the worktree, stop and report it.

Ask the user for the project key in the private chat, never repeat it, and follow exactly one branch below.

When a usable ignored environment store exists, validate the key without persistence:

```text
python -B -I <temporary-source>/scripts/project_context.py validate
```

Supply the key only through the helper's hidden standard input, never as a command argument. After it derives and authenticates the exact project and required scopes, stop installation. Tell the user the derived variable name and relative file path, ask them to place their key there, and wait for them to restart the client through the project's existing environment loader. Do not write the file or fall back to Windows storage.

When no project environment store exists on Windows, disclose that current-user environment variables are readable by other processes running as the same OS user. With the user's agreement, validate and then store only the derived project variable through hidden input:

```text
python -B -I <temporary-source>/scripts/project_context.py store-windows
```

The helper authenticates the key and required scopes before writing the current-user environment variable. Restart the client before continuing. On another operating system, or when the project has no proven way to load its ignored secret store, stop and ask the user to choose a project-local secret-loading mechanism. Do not invent one or persist globally.

## 4. Install

Select the client fixed by the onboarding file:

```text
python -B -I <temporary-source>/scripts/install.py install --client <codex|claude-code|gemini-cli> --target-root <absolute-git-root> [--token-env ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>]
```

Omit `--token-env` when exactly one project-scoped variable is visible. Use it only to select the user-confirmed public variable name when several are visible. The installer derives project identity by validating that variable's key; it never accepts a project ID as input.

The installer creates only project-local artifacts:

- `.acceptora/config.json` with public binding metadata;
- `.agents/skills/acceptora`, `.claude/skills/acceptora`, or `.gemini/skills/acceptora`;
- a managed one-line instruction block for the selected client; and
- `.codex/config.toml`, `.mcp.json`, or `.gemini/settings.json` with the project MCP connection.

The MCP URL is `https://www.acceptora.com/mcp`. Configuration must reference the derived environment-variable name, never contain the key value. Preserve unrelated project instructions and client settings. Do not edit shared user configuration.

For Gemini CLI, the project settings also allowlist only the derived variable so its bearer header survives Gemini's environment-variable redaction; preserve every unrelated project allowlist entry. If any client asks for workspace trust or MCP approval, show the exact project and server to the user and wait for their decision. Never approve either on the user's behalf.

## 5. Validate project context

Restart the selected client so it reloads project instructions, MCP configuration, and environment variables. Then run:

```text
python -B -I <skill-root>/scripts/project_context.py preflight --project-root <absolute-git-root>
```

Require `.acceptora/config.json`, the derived variable name, authenticated project, fixed origin, and current worktree to agree. The preflight must fetch fresh effective verification instructions and report the canonical GitHub `main` update state without exposing the key.

## 6. Confirm

After preflight succeeds:

1. Confirm the Acceptora MCP server is visible and exposes its workflow tools.
2. From the fresh canonical checkout obtained in step 1, run:

   ```text
   python -B -I <temporary-source>/scripts/install.py status --client <codex|claude-code|gemini-cli> --target-root <absolute-git-root>
   ```
3. Report the client, target, public project ID, installed commit, instruction preflight, MCP discovery, and update status. Never report the key value.

If MCP is unavailable, test the authenticated project-metadata REST endpoint and use REST as the workflow fallback. See [API and MCP](references/api-mcp.md).

## Update or uninstall

Obtain a fresh canonical `main` checkout as described in step 1, then run the required command from that checkout:

```text
python -B -I <temporary-source>/scripts/install.py update --client <codex|claude-code|gemini-cli> --target-root <absolute-git-root>
python -B -I <temporary-source>/scripts/install.py status --client <codex|claude-code|gemini-cli> --target-root <absolute-git-root>
python -B -I <temporary-source>/scripts/install.py uninstall --client <codex|claude-code|gemini-cli> --target-root <absolute-git-root>
```

Run only the command needed. Update validates the selected project key before writing. Uninstall affects only the selected worktree and client.
