# MCP tool contract

Use the configured Acceptora server's eight task-oriented operations. MCP exposes them as tools; REST exposes the same contracts at the paths in [rest-api-contract.md](rest-api-contract.md). Both transports are independent of the target repository's language and framework, and neither requires an Acceptora SDK in the target application. Do not reconstruct workflows with generic CRUD.

## Repository locator invariant

Use the deterministic source manifest's exact `repository` string as the only repository locator. Pass it unchanged as `resolve_feature.source_locator` and as `source_descriptor.source_locator` in `reconcile_checklist`, `address_feedback`, and `record_verification_exception`. Reuse it in both completion-gate source descriptors and derive `source_identity` only by prefixing the source kind. Never reconstruct it from `cwd`, a filesystem-resolved path, or a later Git read. The source adapter has already stripped URL credentials and canonicalized drive-absolute and UNC Windows local remotes.

## Tool sequence

### `resolve_feature`

Resolve or create one stable feature from project ID, explicit feature ID, exact aliases, source kind/locator, title, intent, and an idempotency key when creation is permitted. Handle `ambiguous` without guessing.

### `get_feature_context`

Fetch feature/checklist/source revisions, immutable item IDs and definitions, evaluation fingerprints, anchors, decisions and effectiveness, feedback, limits, project rules, concurrency state, effective `verification_instructions`, and the optional persisted `automated_evidence` baseline. With `checklist_definitions`, the response includes `checklist_sections` and a complete immutable `definition` on every active and retired item, including `target` and `test_data`. Use those returned definitions as the lossless base for every retain or update; never reconstruct an existing definition from summary fields. Fetch before drafting and again immediately before every reconciliation. The instruction envelope contains schema version, account/project revisions, canonical digest, configured state, three effective bodies, and each body's `default`, `account`, or `project` source.

Use the complete include projection when preparing a reconciliation:

```json
{
  "feature_id": "feat_01J00000000000000000000001",
  "include": [
    "checklist_definitions",
    "decisions",
    "comments",
    "attachments",
    "history_summary",
    "source_revisions",
    "automated_evidence"
  ],
  "versions": {
    "integration_name": "acceptora-fixture-client",
    "integration_version": "1.0.0",
    "skill_version": "1.2.3",
    "contract_version": "1.0.0"
  }
}
```

### `reconcile_checklist`

Submit the desired structured document with base revision, fresh `verification_instruction_context`, final source descriptor/digest, deterministic manifest, context, evidence, limits, ordered sections/items, explicit item operations, coverage anchors, addressed resolution IDs, skill/contract versions, and idempotency key. The instruction context contains only `account_revision`, `project_revision`, and `effective_digest`; never echo instruction bodies in the request. Automated evidence keeps execution `outcome`, optional `evidence_sufficiency`, and a `not_run` `blocker_reason` separate; none of them is a human decision.

Send the same request object that passed `scripts/validate_checklist_payload.py`, field for field. Do not manually reconstruct the MCP arguments after validation. The v1 request always contains all 18 required root fields: `feature_id`, `base_checklist_revision`, `source_descriptor`, `source_digest`, `source_manifest`, `verification_instruction_context`, `implementation_change_summary`, `intent_summary`, `scope_summary`, `expected_outcome`, `preconditions`, `automated_evidence`, `known_limits`, `sections`, `items`, `addressed_resolution_ids`, `versions`, and `idempotency_key`. Preserve required arrays even when they are empty. If a client must serialize or translate the validated object before the tool call, run validation again on the final translated object immediately before sending it.

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

## Provider-neutral evidence lineage

Each `automated_evidence` entry may omit `lineage` for backward compatibility. When `lineage` is present, send every published lineage key: `project_id`, `provider`, `provider_run_id`, `environment`, `started_at`, `ended_at`, `duration_ms`, `artifact`, `assertion`, `authentication`, `cost`, `usage`, `stop_reason`, and `original_payload_reference`. Do not invent provider data merely to avoid a nullable value.

The server binds `project_id` to the authenticated feature project and treats the evidence entry's top-level `source_revision` and `target` as authoritative. `started_at`, `ended_at`, and `duration_ms` are independently nullable. When both timestamps exist, `ended_at` cannot precede `started_at`; when all three values exist, `duration_ms` must equal the exact elapsed milliseconds. `artifact` and `original_payload_reference` are nullable; when supplied, each contains an absolute URI and a lowercase `sha256:` digest. `assertion` and `authentication` remain explicit objects even when their inner values are null. Authentication mode and outcome must either both be null or both be present, and mode `none` requires outcome `not_required`. Usage quantities are finite, non-negative numbers, and each `(metric, unit)` pair is unique. Monetary amounts are canonical non-negative decimal strings with a three-letter uppercase currency.

Lineage records provider execution facts, not a provider verdict, evidence-sufficiency decision, or human acceptance. Never place credentials, authorization or cookie headers, signed secrets, or the original raw provider payload in lineage. Store only scalar assertion details; each string is limited to 2,000 characters, and request-, response-, body-, payload-, or log-bearing keys are rejected after separator and camel-case normalization. URI references reject standard and signed credential parameters in queries and fragments, including camel-case and percent-encoded keys. Preserve raw material only through the redacted payload reference.

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
- `REVISION_CONFLICT`: refetch feature context and effective instructions, regenerate affected content, and submit a newly evaluated logical write with a new idempotency key.
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

During installation or update, run `<absolute-python> -B -I <external-runtime>/package/scripts/health_check.py --confirm-connection --format json` as the final step after client review and installer status. Use only the installer-owned external runtime configuration. Repository `.verification/config.json` is non-authoritative source metadata and must never select a credential or network destination. For later read-only compatibility diagnostics, run the same command without `--confirm-connection`.

The health check verifies the public contract and OpenAPI document, credential-bound project ID and lifecycle, canonical verification-instruction envelope/digest without printing its bodies, one canonical endpoint origin, package/server versions, mandatory and optional scopes, MCP server identity, the exact eight tools, approval annotations, and input/output schema digests. Its authenticated MCP calls are limited to `initialize`, `notifications/initialized`, and `tools/list`; it never invokes a product write tool. With `--confirm-connection`, only after every check passes, it sends an exact empty JSON object to the setup-only REST endpoint that explicitly marks the pinned project connected. That endpoint independently requires all seven normal workflow scopes and is not a ninth MCP tool. Without the flag, no confirmation request is sent and the project is not marked connected. Either mode can update ordinary credential-use telemetry. The credential name is derived from the pinned public project ID as `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>`, and output never includes its value. Use a trusted CA for private certificates; do not bypass TLS verification.

For persisted recovery, replay only the three post-resolution write tools supported by the offline envelope: `reconcile_checklist`, `address_feedback`, and `record_verification_exception`. Read [offline-recovery.md](offline-recovery.md) before replaying; authentication, scope, revision, source, contract, or idempotency conflicts are not transient retries.
