# Security-sensitive pattern

Apply whenever a change affects trust boundaries, credentials, sensitive data, rendering, uploads, execution, authorization, or abuse resistance.

## Coverage prompts

- Identify the asset, actor, trust boundary, and expected deny behavior.
- Verify authorization at the application-service boundary and direct request path.
- Check input validation, canonicalization, output encoding, sanitized Markdown/HTML, and injection resistance relevant to the change.
- Verify secret rejection/redaction and absence from logs, errors, exports, URLs, and stored content.
- Check credential hashing, one-time display, scope, expiry, rotation, and revocation.
- For uploads, verify content-based type/size limits, quarantine, malware scan, risky active-type rejection, authorized reads, and deletion.
- Exercise tenant/project isolation and non-enumerating errors.
- Verify audit records and correlation without sensitive payloads.
- Record unperformed destructive or production security checks as structured blocking limits when appropriate.

Use auth, policy, renderer, storage, credential, tenant, and global trust-contract anchors. Automated security checks do not create human acceptance.
