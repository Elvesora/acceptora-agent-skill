# API and MCP

Use this reference when calling Acceptora. The canonical origin is `https://www.acceptora.com`.

## Transport

Prefer Streamable HTTP MCP at `https://www.acceptora.com/mcp`. Use REST only when MCP is unavailable or the integration is intentionally REST-only. Do not add an Acceptora SDK to the target project merely to call either transport.

For REST, fetch the live OpenAPI 3.1 document from:

```text
GET https://www.acceptora.com/api/v1/integrations/openapi.json
```

Use `Authorization: Bearer <project-key>` on protected REST requests. Follow the live OpenAPI request and response schemas instead of reconstructing the contract from examples.

## Project preflight

`GET /api/v1/integrations/project` returns authenticated project metadata, endpoints, granted scopes, and effective verification instructions. Require the authenticated project to match `.acceptora/config.json`.

Effective instructions contain:

- `analysis_guidance`
- `manual_verification_guidance`
- `test_data_guidance`
- account and project revisions plus an effective digest

Fetch them before work and again immediately before writing manual verification steps. Reconciliation must use the matching fresh revisions and digest, not instruction bodies copied into the request.

## Workflow operations

MCP exposes eight tools. REST exposes the same operations:

| MCP tool | REST path |
|---|---|
| `resolve_feature` | `POST /api/v1/integrations/features/resolve` |
| `get_feature_context` | `POST /api/v1/integrations/features/context` |
| `reconcile_checklist` | `POST /api/v1/integrations/checklists/reconcile` |
| `get_verification_feedback` | `POST /api/v1/integrations/feedback/query` |
| `address_feedback` | `POST /api/v1/integrations/feedback/address` |
| `get_verification_status` | `POST /api/v1/integrations/status` |
| `check_completion_gate` | `POST /api/v1/integrations/completion-gate` |
| `record_verification_exception` | `POST /api/v1/integrations/verification-exceptions` |

A typical implementation flow is:

1. `resolve_feature`
2. `get_feature_context`
3. implement and run project-native automated checks
4. rerun project preflight and `get_feature_context`
5. `reconcile_checklist`
6. read status, feedback when relevant, and the completion gate

Use only the operations needed for the task. Do not invoke a write merely to test connectivity.

## Safety and failures

- Keep each request bound to the authenticated project.
- Never send keys, environment files, authorization headers, cookies, production personal data, or raw provider payloads as checklist content or evidence.
- Preserve idempotency: reuse a key only for a byte-equivalent retry of the same logical write.
- On a revision conflict, refetch instructions and feature context, regenerate the affected request, and use a new idempotency key.
- If a write outcome is unknown, read current state before retrying or changing transport.
- Respect the server's stable error and recovery fields. Do not bypass TLS or follow an authenticated request to another origin.

API success and completion-gate success do not grant human acceptance. Only a human can make verification decisions.
