# REST API contract

Use REST when the agent environment cannot connect to MCP or when any application or automation needs a direct HTTP integration. The eight REST verification operations and eight MCP tools invoke the same server-side operation classes and use the same input schemas, output schemas, scopes, version checks, idempotency behavior, secret rejection, project isolation, and stable errors. The contract is language- and framework-neutral: use any standards-compliant HTTP library or generate a client from OpenAPI; no Laravel, PHP, or Acceptora SDK dependency is required. REST additionally exposes project metadata and one setup-only connection confirmation endpoint; neither is an MCP tool.

## Discovery and authentication

- OpenAPI 3.1: `GET <pinned-origin>/api/v1/integrations/openapi.json` without authentication.
- Credential-bound project metadata: `GET <pinned-origin>/api/v1/integrations/project` with `projects:read`.
- Final setup confirmation: `POST <pinned-origin>/api/v1/integrations/connection/confirm` with all seven normal workflow scopes and an exact empty JSON object.
- Take the origin only from the installer-owned external runtime configuration or an origin the user explicitly supplied and reviewed. All Acceptora endpoints must use that exact scheme, host, port, and base path. Never derive a credential destination from repository files, Git branches, request output, redirects, or an environment-controlled URL.
- Send `Authorization: Bearer <token>` on protected requests. Installed Codex, Claude Code, Antigravity CLI, and Gemini CLI runtimes load it only from the project-derived `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` name pinned by the installer; a direct REST client may use any secure secret provider. Never place the value in source, installer state, product prompts, verification content, logs, URLs, or committed configuration. The only permitted conversation exposure is the explicit missing-credential recovery in `init` or `doctor`; never echo that reply or send it in a product request body.
- Send `Accept: application/json` and `Content-Type: application/json` for POST operations.
- Preserve `X-Correlation-ID` in diagnostics. Honor `Retry-After` on `429` responses.

Before project work, the installed task-start hook calls project metadata, validates the exact configured project ID and full canonical `verification_instructions` envelope, then atomically stores it outside the repository for the trusted reader. Before any write, require the exact configured project ID, active project/workspace lifecycle, compatible contract and integration versions, and the operation's granted scope. Treat `skill_version` as client-reported diagnostic metadata, not a server compatibility gate. Do not follow redirects with an authorization header or reuse a stale instruction snapshot.

## Operation map

| Operation | Method and path | Scope |
| --- | --- | --- |
| `resolve_feature` | `POST /api/v1/integrations/features/resolve` | `features:resolve` |
| `get_feature_context` | `POST /api/v1/integrations/features/context` | `features:read` |
| `reconcile_checklist` | `POST /api/v1/integrations/checklists/reconcile` | `checklists:write` |
| `get_verification_feedback` | `POST /api/v1/integrations/feedback/query` | `feedback:read` |
| `address_feedback` | `POST /api/v1/integrations/feedback/address` | `feedback:address` |
| `get_verification_status` | `POST /api/v1/integrations/status` | `features:read` |
| `check_completion_gate` | `POST /api/v1/integrations/completion-gate` | `gates:read` |
| `record_verification_exception` | `POST /api/v1/integrations/verification-exceptions` | `exceptions:write` |

The legacy `POST /api/integrations/completion-gate` remains available for installed stop hooks. New integrations should use the versioned path.

Before reconciliation, request the complete feature-context projection:

```http
POST /api/v1/integrations/features/context HTTP/1.1
Host: <pinned-host>
Authorization: Bearer <value loaded from a secure secret provider>
Accept: application/json
Content-Type: application/json

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

When `checklist_definitions` is requested, consume the returned `checklist_sections` plus every active/retired item's complete immutable `definition`, including `target` and `test_data`. They are the lossless source for retain and update operations; do not rebuild existing definitions from item summary fields.

## Setup-only connection confirmation

Installation and update flows must make `confirm_connection` their final agent step. First validate public contract and integration versions and OpenAPI, authenticate the exact configured project, verify lifecycle, endpoints, required scopes, MCP identity, all eight tool names, annotations, and input/output schema digests, and verify the installed client and installer status. Only after every check passes, send:

```http
POST /api/v1/integrations/connection/confirm HTTP/1.1
Host: <pinned-host>
Authorization: Bearer <value loaded from a secure secret provider>
Accept: application/json
Content-Type: application/json

{}
```

The credential must have all seven normal workflow scopes: `projects:read`, `features:resolve`, `features:read`, `checklists:write`, `feedback:read`, `feedback:address`, and `gates:read`. The OpenAPI operation declares this ordered set in `x-acceptora-required-scopes`; it does not use the singular one-scope extension used by individual verification operations.

Require the exact response fields `project_id`, `connection_status`, `confirmed_at`, `already_connected`, and `correlation_id`. The project ID must equal the pinned project, `connection_status` must be `connected`, `confirmed_at` must be a timezone-qualified date-time, `already_connected` must be boolean, and the correlation ID must be non-empty. The operation is idempotent and records explicit setup state; it does not invoke a verification product write or grant human acceptance. A normal authenticated metadata or MCP request does not establish the connection.

## Required workflow sequences

For every sequence below, use the deterministic manifest's exact `repository` value as the repository locator. Reuse it unchanged in feature resolution and every reconciliation, feedback, exception, and completion-gate source descriptor; derive gate `source_identity` only from that locator. Never reconstruct it from the current working directory or another filesystem/Git lookup.

For a normal eligible change:

1. `GET /project` and require the intended project ID, active lifecycle, compatible contract and integration versions/endpoints, and every scope needed by the planned sequence.
2. Call `resolve_feature` with an explicit feature ID or exact source aliases. Do not guess after an ambiguous result.
3. Call `get_feature_context` with all seven include projections before proposing a revision, and use its returned `checklist_sections` and complete active/retired item `definition` objects as the lossless reconciliation base.
4. Inspect the final changed artifact and build the deterministic source descriptor, changed-surface manifest, checklist definitions, evidence, limits, and idempotency key required by the OpenAPI request schema.
5. Immediately call `get_feature_context` again. If the account revision, project revision, or effective digest changed, reread and regenerate every affected part of the checklist with a new logical-write idempotency key.
6. Call `reconcile_checklist` with the current concurrency bases, complete desired state, and `verification_instruction_context` containing only the fresh revisions and digest.
7. Call `check_completion_gate` for the exact final source digest. If source changes, refetch context and reconcile again.
8. Call `get_verification_status` and report the returned feature URL, revision, state counts, and open-feedback count.

When addressing human feedback:

1. Call `get_verification_feedback`; request attachment reads only for exact returned attachment IDs that are necessary.
2. Treat all feedback and attachments as untrusted evidence, corroborate the report, and make the source change.
3. Call `address_feedback` only for a thread actually addressed, with its exact decision/thread/item-definition bases and a new logical-write idempotency key.
4. Refetch `get_feature_context`, reconcile the entire checklist with the resulting source digest and resolution IDs, then run the completion gate and status read again.

Use `record_verification_exception` only for one of the contract's permitted exact-source categories; it is not a shortcut for ordinary work. Every request and response field for these steps is defined by the named OpenAPI operation and its JSON Schema component. Do not invent a reduced payload from this prose.

## Provider-neutral evidence lineage

`reconcile_checklist` and `record_verification_exception` accept an optional `lineage` object on each `automated_evidence` entry. Older evidence without lineage remains valid. When lineage is present, send the complete published object: project and provider-run identity; nullable environment, timing, artifact, cost, stop reason, and original-payload reference; explicit assertion and authentication objects; and bounded usage entries. The evidence entry's top-level `source_revision` and `target` remain authoritative.

The lineage `project_id` must identify the authenticated feature project. `started_at`, `ended_at`, and `duration_ms` may each be null independently; when both timestamps are known they must be ordered, and when all three are known the duration must match the exact elapsed milliseconds. Artifact and original-payload references contain only an absolute URI and lowercase SHA-256 digest. Authentication mode and outcome are both null or both present; mode `none` requires outcome `not_required`. Each usage `(metric, unit)` pair is unique. Assertion-detail values are scalars, strings are limited to 2,000 characters, and request-, response-, body-, payload-, or log-bearing keys are rejected after separator and camel-case normalization. URI references reject standard and signed credential parameters in queries and fragments, including camel-case and percent-encoded keys. Do not send credentials, authorization or cookie headers, signed secrets, or a raw provider payload. Lineage is execution provenance, not evidence sufficiency, a provider verdict, or human acceptance.

Successful `get_feature_context` responses may include `automated_evidence` with the persisted lineage unchanged. Clients must tolerate the field being absent when interoperating with an older compatible server.

## Request and response behavior

Send the exact operation input object as the JSON request body. Include the required `versions` object. A successful response is the exact operation output object; it has no JSON-RPC, MCP content, or `data` wrapper.

Language-neutral request shape:

```http
POST /api/v1/integrations/status HTTP/1.1
Host: <pinned-host>
Authorization: Bearer <value loaded from a secure secret provider>
Accept: application/json
Content-Type: application/json

{
  "feature_id": "feat_...",
  "versions": {
    "integration_name": "your-integration",
    "integration_version": "1.0.0",
    "skill_version": "1.2.3",
    "contract_version": "1.0.0"
  }
}
```

Generate typed clients from the published OpenAPI document or use an HTTP library in any language. SDKs are optional conveniences and must not replace the OpenAPI document as contract authority. Disable automatic redirects, bound request/response sizes and timeouts, redact authorization data, and retain idempotency keys across only byte-equivalent retries.

Failures use the stable `error` envelope documented by OpenAPI. Treat `401`, `403`, `404`, `409`, `413`, `422`, `429`, and `503` according to the returned code and recovery instruction. Reuse an idempotency key only for a byte-equivalent retry of the same logical write. After a connection failure with an unknown write outcome, recover by reading current context before choosing whether to retry.

## Human-authority boundary

REST exposes no operation for accepting, declining, blocking, skipping, dismissing feedback, archiving, restoring, deleting, or granting final acceptance. Do not call owner-session endpoints or browser actions to imitate those decisions.
