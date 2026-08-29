# Offline outbox recovery

Use this procedure after a post-resolution MCP write exhausted bounded retries. The outbox supports `reconcile_checklist`, `address_feedback`, and `record_verification_exception`; it does not invent feature identity or replace the required context fetch.

## Persist the exact logical write

Validate a reconciliation request before saving it through the installer-owned runtime. Commands below use one-line PowerShell syntax with quoted paths; replace every placeholder. On POSIX shells, remove only the leading `&`.

```text
& "<absolute-python>" -B -I "<external-runtime>/package/scripts/validate_checklist_payload.py" "<request.json>" --pretty
```

Write the request with the same feature ID and idempotency key present in the payload:

```text
& "<absolute-python>" -B -I "<external-runtime>/package/scripts/write_offline_outbox.py" "<request.json>" --operation reconcile_checklist --feature-id "<feat_ULID>" --idempotency-key "<UUID>" --completion-gate-payload "<completion-gate.json>"
```

The completion-gate payload is optional. Include it for a final reconciliation or exception when the deterministic baseline/current evidence is available. Do not attach it to an intermediate `address_feedback` write whose matching reconciliation has not succeeded.

The writer rejects secret-like content, payload/CLI identity mismatches, and reuse of one idempotency key with different content. It writes atomically and never stores a bearer token.

## Validate without sending

Use `replay_offline_outbox.py` from the installer-owned external runtime. Its configuration pins the target repository, one Acceptora origin, and the `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` name derived from its public project ID. Repository configuration cannot select the credential or destination. Inspect the pinned target's records without network access:

```text
& "<absolute-python>" -B -I "<external-runtime>/package/scripts/replay_offline_outbox.py" --dry-run
```

The replay client refuses credentials in endpoint URLs, refuses plain HTTP except for localhost development, requires every endpoint to share the pinned origin, and never follows redirects. It does not accept repository-controlled endpoint or token-name overrides.

## Replay

```text
& "<absolute-python>" -B -I "<external-runtime>/package/scripts/replay_offline_outbox.py"
```

For each record, the client:

1. verifies schema version, filename identity, payload hash, feature ID, idempotency key, and secret policy;
2. initializes the Streamable HTTP MCP connection and calls the stored tool with the exact stored arguments;
3. retries only transport, rate-limit, service-unavailable, or gate-unavailable failures, with bounded exponential backoff;
4. sends the stored completion-gate payload through the configured HTTP adapter when present;
5. writes only safe correlation/outcome receipt metadata and moves a confirmed record to `processed_outbox`.

A failed record stays `pending` in place with a safe error code, attempt count, and redacted message. `AUTH_REQUIRED`, `SCOPE_DENIED`, `REVISION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `SOURCE_STALE`, `CONTRACT_UNSUPPORTED`, `continue_sync`, and `ambiguous` stop automatic retries. Fix the underlying condition. If current context requires a new logical write, create a new request and a new idempotency key; never modify the old record to make it appear equivalent.

An MCP delivery without a stored gate payload proves delivery only. Run the normal completion hook afterward. A stored gate payload is confirmed only by `pass` or `not_required`; neither outcome means human acceptance.
