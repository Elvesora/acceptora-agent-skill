# API and SDK pattern

Apply to endpoints, payloads, schemas, clients, public methods, serialization, or compatibility contracts.

## Coverage prompts

- Exercise the exact endpoint/method with safe representative input.
- Verify status/result type, required fields, value semantics, headers, and serialization.
- Check documented validation failures, authentication/authorization, not-found/conflict, and rate/quota behavior when applicable.
- Confirm omitted, null, empty, boundary, and unknown fields behave intentionally.
- Verify backward compatibility or clearly documented breakage for existing callers.
- Check pagination, ordering, filtering, cursor stability, and idempotency when relevant.
- Verify SDK method signatures, exceptions/error objects, retries, and sync/async behavior.
- Compare implementation, generated schema, examples, and public documentation.
- Avoid live production calls unless explicitly optional and safely credentialed.

Use `api:*`, file, contract, and public-symbol anchors. Never include real tokens in commands or payload examples.
