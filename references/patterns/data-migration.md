# Data model and migration pattern

Apply to tables, indexes, application-level relations, migrations, backfills, retention, or data transformations.

## Coverage prompts

- Verify migration preconditions and schema outcome on representative data.
- Confirm reads and writes work against the new shape.
- Check null/default/legacy/duplicate/orphan behavior.
- Verify indexes and query plans for materially affected paths when risk warrants it.
- Exercise application-level integrity and cleanup because database foreign keys may be intentionally absent.
- Check concurrency, retries, and idempotency for backfills or transformations.
- Verify rollback only when safe; otherwise record the unperformed boundary and recovery plan as a limit.
- Confirm retention/export/deletion behavior for new stored data.
- Rehearse backup/restore when the change can threaten recoverability.

Use data, migration, model, query, and lifecycle anchors. Warn before destructive or production-scale steps.
