# Integration, webhook, and synchronization pattern

Apply to external providers, webhooks, callbacks, polling, sync, destinations, or cross-system identity.

## Coverage prompts

- Verify connection/authentication with a disposable or sandbox credential when available.
- Exercise the intended outbound/inbound event and inspect the exact mapped result.
- Check signature/state validation, replay protection, idempotency, duplicate delivery, ordering, and retries.
- Verify provider timeout, rate limit, invalid response, partial failure, and recovery behavior.
- Confirm mappings for missing, null, changed, or unsupported fields.
- Verify disconnect/revocation and stale-credential behavior.
- Check logs/audit/correlation without exposing payload secrets.
- Confirm no live external side effect is implied if the provider was not connected.

Use `api:` or `route:` for provider and webhook operations, `file:` for mappings and workers, `config:` for provider settings, and `data:` for external identities or synchronized state. Make live calls optional unless essential and safe.
