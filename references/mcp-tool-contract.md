# MCP tool contract

Use the configured Acceptora server's eight task-oriented operations. MCP exposes them as tools; REST exposes the same contracts at the paths in [rest-api-contract.md](rest-api-contract.md). Both transports are independent of the target repository's language and framework, and neither requires an Acceptora SDK in the target application. Do not reconstruct workflows with generic CRUD.

## Tool sequence

### `resolve_feature`

Resolve or create one stable feature from project ID, explicit feature ID, exact aliases, source kind/locator, title, intent, and an idempotency key when creation is permitted. Handle `ambiguous` without guessing.

### `get_feature_context`

Fetch feature/checklist/source revisions, immutable item IDs and definitions, evaluation fingerprints, anchors, decisions and effectiveness, feedback, limits, project rules, and concurrency state. Fetch before every reconciliation.

### `reconcile_checklist`

Submit the desired structured document with base revision, final source descriptor/digest, deterministic manifest, context, evidence, limits, ordered sections/items, explicit item operations, coverage anchors, addressed resolution IDs, skill/contract versions, and idempotency key. Automated evidence keeps execution `outcome`, optional `evidence_sufficiency`, and a `not_run` `blocker_reason` separate; none of them is a human decision.

### `get_verification_feedback`

Read current actionable threads with exact originating decision and item-definition revisions, comments, permitted attachment metadata, thread versions, and pagination cursor. Request metadata first with `attachment_read_ids: []`. Only when the content is necessary, repeat the read with the exact returned attachment IDs in `attachment_read_ids`; the server may then issue short-lived, single-project authorized URLs. Never request every attachment speculatively or construct a download URL yourself.

### `address_feedback`

Submit one guarded resolution per addressed thread with checklist/thread/decision/item-definition bases, resulting source digest, exact evidence, and one logical-write idempotency key. The request is atomic and does not alter human state.

### `get_verification_status`

Read compact checklist/source synchronization, human item-state counts, open feedback, recheck counts, final-acceptance status, and URL for an accurate completion response. This status does not relabel automated evidence as a human item decision.

### `check_completion_gate`

Submit project/source identity, adapter/version, baseline and current descriptors/digests, deterministic changed-surface manifest, task correlation ID, and optional explicit feature ID. Interpret only `pass`, `continue_sync`, `not_required`, `ambiguous`, or `unavailable`. A `pass` proves current checklist synchronization, not evidence sufficiency, test success, or human acceptance.

### `record_verification_exception`

Persist a permitted changed-source exception with exact source/baseline descriptors, digest, manifest, allowed category, specific explanation, actual automated evidence, versions, and idempotency key. It derives synchronization without human acceptance.

## Scopes

- `projects:read` for the REST project-metadata preflight; it is not a ninth verification tool.
- `features:resolve`
- `features:read`
- `checklists:write`
- `feedback:read`
- `feedback:address`
- `gates:read`
- `exceptions:write`

No agent scope exists for human decisions, final acceptance, human thread dismissal, comment editing, or hard deletion.

## Error handling

- `AUTH_REQUIRED`: stop retries and report setup action.
- `SCOPE_DENIED`: report missing scope; do not broaden access automatically.
- `FEATURE_AMBIGUOUS`: ask for explicit identity.
- `REVISION_CONFLICT`: refetch and submit a newly evaluated logical write.
- `IDEMPOTENCY_CONFLICT`: use a new key only for genuinely new content.
- `SOURCE_STALE`: reinspect final source and regenerate digest.
- `UNCOVERED_CHANGED_SURFACE`: add item/limit/allowed exception coverage.
- `EXCEPTION_POLICY_DENIED`: create the normal checklist or request a policy decision.
- `SECRET_REJECTED`: remove the secret without echoing it.
- `PAYLOAD_TOO_LARGE`: remove nonessential prose or page evidence, not required checks.
- `RATE_LIMITED`: honor retry metadata with bounded backoff.
- `CONTRACT_UNSUPPORTED`: stop and report the required package/server upgrade.
- `SERVICE_UNAVAILABLE`: bounded retry, then visible offline recovery.

Every write is idempotent. Every response/error should carry a correlation ID. Never place bearer credentials in checklist content or logs. Do not retry the same write through a different transport until its outcome is resolved.

## Compatibility health check

During installation or update, run `<absolute-python> -I <external-runtime>/package/scripts/health_check.py --confirm-connection --format json` as the final step after client review and installer status. Use only the installer-owned external runtime configuration. Repository `.verification/config.json` is non-authoritative source metadata and must never select a credential or network destination. For later read-only compatibility diagnostics, run the same command without `--confirm-connection`.

The health check verifies the public contract and OpenAPI document, credential-bound project ID and lifecycle, one canonical endpoint origin, package/server versions, mandatory and optional scopes, MCP server identity, the exact eight tools, approval annotations, and input/output schema digests. Its authenticated MCP calls are limited to `initialize`, `notifications/initialized`, and `tools/list`; it never invokes a product write tool. With `--confirm-connection`, only after every check passes, it sends an exact empty JSON object to the setup-only REST endpoint that explicitly marks the pinned project connected. That endpoint independently requires all seven normal workflow scopes and is not a ninth MCP tool. Without the flag, no confirmation request is sent and the project is not marked connected. Either mode can update ordinary credential-use telemetry. The credential name is fixed to `ACCEPTORA_AGENT_TOKEN`, and output never includes its value. Use a trusted CA for private certificates; do not bypass TLS verification.

For persisted recovery, replay only the three post-resolution write tools supported by the offline envelope: `reconcile_checklist`, `address_feedback`, and `record_verification_exception`. Read [offline-recovery.md](offline-recovery.md) before replaying; authentication, scope, revision, source, contract, or idempotency conflicts are not transient retries.
