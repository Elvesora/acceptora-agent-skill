# Checklist writing rules

## Required document context

Provide a human-readable title, stable feature ID, project, status, intent, expected outcome, changed scope, source revision, sources, environment/base URL, actual automated evidence, estimated manual time, reconciliation attribution, and visible known limits.

Select only relevant sections from this order:

1. Scope and expected outcome
2. Preconditions
3. Automated baseline
4. Primary workflow
5. Feature-specific behavior
6. Visual and responsive review
7. Validation/error/empty/loading/permission states
8. Data, persistence, integration, or contract checks
9. Documentation/content accuracy
10. Optional live or destructive checks
11. Regression boundaries
12. Reset and cleanup
13. Final criteria
14. Known limits and unverified areas

## One item, one observable claim

Each item must include:

- stable semantic key and immutable item ID when it already exists;
- concise observable title;
- imperative Action;
- concrete Expected result;
- product/safety/compatibility reason under Why this matters;
- one or more coverage anchors;
- risk: `critical`, `high`, `normal`, or `low`;
- required/optional classification;
- exact preconditions, target, test data, estimate, source references, and side-effect warning when applicable.

## Writing quality

1. Start actions with Open, Select, Enter, Run, Compare, Inspect, Confirm, or Attempt.
2. Name exact routes, labels, files, commands, payload fields, status values, devices, or dimensions when evidence permits.
3. Make the expected result independently observable; do not repeat the action.
4. Explain impact rather than paraphrasing the expectation.
5. Separate required checks from optional live/destructive checks.
6. Warn before data creation/deletion, email, billing, external sync, credential rotation, or production impact.
7. Add reset/cleanup for created records, credentials, jobs, messages, and files.
8. Use platform-correct commands and safe fixture IDs/URLs.
9. Redact secrets as `[REDACTED_SECRET]`.
10. Exclude irrelevant pattern prompts instead of padding the document.

## Automated evidence

Record every relevant command/check truthfully, including working directory/target, exit status, concise result, and source revision. Use `outcome: passed`, `failed`, or `warning` only when execution actually occurred; use `not_run` when it did not.

Keep execution and proof quality separate:

- set `evidence_sufficiency` to `sufficient` only when the record is enough for the owner to evaluate the stated requirement;
- set it to `insufficient` when a run result exists but does not prove the requirement, including a superficially passing narrow test;
- for `not_run`, use `evidence_sufficiency: insufficient` when reporting sufficiency and add a machine-readable `blocker_reason` such as `missing_credentials` or `environment_unavailable`;
- omit `blocker_reason` for executed outcomes; explain details safely in `summary` without including credentials.

Historical evidence may omit sufficiency because the fields are backward-compatible additions. Never infer that an omitted value is sufficient. Automated evidence is read-only and never increments manual progress or grants acceptance.

## Structured known limits

Create a structured limit when an applicable boundary cannot currently be verified. Include stable key, description, affected anchors, severity, `blocks_acceptance`, status, reason, mitigation/next action, and source revision.

Use an optional/conditional item instead when the human can perform the check. An open blocking limit prevents final acceptance. Never hide a limit in prose or imply an unavailable check passed.
