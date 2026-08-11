## Summary

Describe the public skill or integration behavior changed and why.

## Contract impact

- [ ] No API or MCP contract behavior changed.
- [ ] Routes, authentication, scopes, schemas, versions, manifest, examples, tests, and changelog are synchronized.

## Security and compatibility

- [ ] Installer, filesystem, executable, source-capture, credential, redirect, plan, receipt, rollback, and external-runtime boundaries remain intact.
- [ ] Codex, Claude Code, and Gemini CLI templates remain aligned with their primary documentation.
- [ ] Fixtures, logs, errors, plans, receipts, archives, and documentation contain no credentials, personal data, private source material, customer data, local paths, or unrelated project identifiers.

## Verification

- [ ] The Python unit suite passes.
- [ ] Python sources compile on the affected supported versions.
- [ ] Skill metadata validation passes.
- [ ] The release builder creates deterministic archives from clean immutable Git blobs.
- [ ] The release archive contains only intended customer files.
- [ ] `git diff --check` passes.

## Documentation

- [ ] README, SETUP, CHANGELOG, SECURITY, SUPPORT, and CONTRIBUTING remain accurate for public behavior.
