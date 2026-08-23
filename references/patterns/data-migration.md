# Data model and migration pattern

Apply to schemas, tables, collections, documents, indexes, application-level relations, migrations, backfills, retention, or data transformations.

## Coverage prompts

- Verify migration preconditions and schema outcome on representative data.
- Confirm reads and writes work against the new shape.
- Check null/default/legacy/duplicate/orphan behavior.
- Verify indexes and query plans for materially affected paths when risk warrants it.
- Verify referential and business integrity in every database or application layer where the target project's contract promises it, without assuming a particular database model or constraint mechanism.
- Check concurrency, retries, and idempotency for backfills or transformations.
- Verify rollback only when safe; otherwise record the unperformed boundary and recovery plan as a limit.
- Confirm retention/export/deletion behavior for new stored data.
- Rehearse backup/restore when the change can threaten recoverability.

Use `data:` for schemas, records, relations, queries, and lifecycle state; `file:` for migration or model sources; and `config:` for storage behavior. Warn before destructive or production-scale steps.
