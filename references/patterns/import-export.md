# Import, export, and download pattern

Apply to file ingestion, bulk imports, generated exports, downloads, or data portability.

## Coverage prompts

- Use an exact safe fixture and verify accepted type, size, encoding, delimiter, headers, and naming.
- Confirm preview/validation before commit when supported.
- Check malformed, empty, duplicate, oversized, unsupported, and partially valid input.
- Verify atomicity or clearly reported partial results and retry behavior.
- Confirm authorization and tenant scoping for upload, job status, export, and download.
- Inspect exported columns/records/order/escaping/timezone and compare to the requested boundary.
- Verify private storage, expiry, safe filenames/content disposition, and cleanup.
- Check formulas/active content/path traversal or archive expansion risks when applicable.

Use `route:` or `api:` for import/export entry points, `file:` for parsers and workers, `config:` for storage behavior, and `data:` for resulting state. Warn before bulk mutation and specify reset steps.
