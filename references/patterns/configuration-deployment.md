# Configuration and deployment pattern

Apply to environment settings, infrastructure manifests, build/release configuration, feature flags, runtime startup, or deployment behavior.

## Coverage prompts

- Validate syntax/schema and render the final effective configuration without secrets.
- Confirm required variables, defaults, precedence, and failure messages for missing/invalid values.
- Exercise startup/health/readiness and the user-visible path affected by configuration.
- Verify worker/scheduler/storage/database/scanner dependencies when changed.
- Check environment-specific differences and prevent local/test defaults from leaking to production.
- Rehearse migration/deploy order, rollback, and backup/restore when risk warrants it.
- Verify feature-flag off/on and stale-cache behavior.
- Check logs/metrics/alerts for clear failure and recovery signals.

Use config, manifest, service, health, deployment, and global contract anchors. Never include committed secret values or claim a deployment ran when only configuration was edited.
