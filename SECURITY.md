# Security Policy

## Supported source

Install and update only from the canonical repository's `main` branch. A semantic version describes compatibility; it does not replace the recorded Git commit.

## Report a vulnerability

Use the private [GitHub Security Advisory form](https://github.com/Elvesora/acceptora-agent-skill/security/advisories/new). If it is unavailable, request a private channel through the [Acceptora contact page](https://www.acceptora.com/contact). Do not put credentials, customer data, private source, production URLs, or exploit details in a public issue or initial contact request.

## Current security boundary

- Every worktree binds to one public project ID and its derived `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` variable.
- The key is authenticated against Acceptora before installation writes project state.
- The installer never writes a key into the repository, `.acceptora/config.json`, MCP configuration, logs, or output.
- A possible project environment file is inspected only by filename and Git metadata; its contents are never read or written.
- Client skills, instructions, MCP configuration, and public binding metadata are project-local. Unrelated settings are preserved.
- Authenticated requests use the fixed HTTPS origin and refuse redirects.
- Agents may synchronize verification material but cannot make human acceptance decisions.

Environment variables are readable by code running in the same process context. A Windows current-user value can also be read by other processes running as that user. Use separate client processes and each project's existing secret loader when stronger isolation is required.
