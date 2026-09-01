# Security Policy

## Supported source

Install and update only from the public `acceptora-agent-skill` npm package. The npm version is the update authority, and each project ownership manifest records the SHA-256 digest of every installed payload file. The canonical GitHub repository remains the public source for review, not an installer input.

## Report a vulnerability

Use the private [GitHub Security Advisory form](https://github.com/Elvesora/acceptora-agent-skill/security/advisories/new). If it is unavailable, request a private channel through the [Acceptora contact page](https://www.acceptora.com/contact). Do not put credentials, customer data, private source, production URLs, or exploit details in a public issue or initial contact request.

## Current security boundary

- Every worktree binds its own `.acceptora-env` key to one authenticated public project ID.
- The key is authenticated against Acceptora before installation writes project state.
- The installer writes the key only as `ACCEPTORA_PROJECT_TOKEN` in `.acceptora-env`; add `/.acceptora-env` to `.gitignore`.
- A tracked or linked `.acceptora-env` is rejected. Application `.env` files are not read or changed.
- The installer never copies a key into `.acceptora/config.json`, MCP configuration, logs, or user-facing output.
- Client skills, instructions, MCP configuration, and public binding metadata are project-local. Unrelated settings are preserved.
- Authenticated requests use the fixed HTTPS origin and refuse redirects.
- Agents may synchronize verification material but cannot make human acceptance decisions.

Code running in a worktree can read that worktree's `.acceptora-env`. Keep normal repository permissions restricted and never commit or share the file.
