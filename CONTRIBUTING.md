# Contributing

Thank you for contributing to Acceptora Agent Skill.

## Development requirements

- Python 3.11 or newer
- Git
- A checkout with repository-local `core.autocrlf=false` on Windows

Run the complete package gate before opening a pull request:

```text
python -m unittest discover -s tests -p "test_*.py"
python -m compileall -q adapters scripts tests
git diff --check
```

The unit suite validates skill metadata, customer documentation links, client templates, installer behavior, production-`main` source capture, the deterministic ZIP bundle and embedded provenance, security boundaries, contract fixtures, offline recovery, and health checks.

## Change expectations

- Keep `SKILL.md` focused, imperative, and under 500 lines. Its YAML frontmatter may contain only `name` and `description`.
- Route install and update through `references/init.md` and diagnostics through `references/doctor.md`. Bare invocation remains the completion workflow so completion hooks still work.
- Put detailed agent guidance in directly linked `references/` files and deterministic operations in tested `scripts/` files.
- Keep repository documentation and community files, including `GETTING-STARTED.md` and `SETUP.md`, outside the installed skill payload.
- Keep the canonical repository's `main` branch as the production source and sole update authority. The downloadable ZIP must remain a deterministic snapshot of a clean `main` commit, never an independently authored or version-selected source.
- Preserve Python 3.11 compatibility unless a major release changes the supported platform.
- Preserve the strict Git, filesystem, executable, origin, credential, plan, receipt, rollback, and external-runtime boundaries.
- Never log or persist bearer tokens, repository contents, private source material, response bodies, credentials, cookies, personal data, or customer data.
- Add `unittest` coverage for every changed success, failure, and boundary path.
- Keep Codex, Claude Code, Antigravity CLI, and Gemini CLI templates aligned with their primary configuration and hook documentation.
- Synchronize routes, authentication, scopes, versions, MCP tools, annotations, schemas, digests, examples, tests, package metadata, and the changelog for every API or MCP contract change.
- Keep fixtures synthetic and free of customer, personal, local-workstation, or unrelated project identifiers.

## Pull requests

Describe the public behavior changed, contract impact, security impact, compatibility impact, and exact verification performed. Keep unrelated changes separate. All continuous-integration jobs must pass across the supported Python matrix before merge.

## Security reports

Do not disclose suspected vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md).
