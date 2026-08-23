# Identity and reconciliation

## Resolve feature identity

Use this order:

1. Explicit `feature_id` from current context.
2. Exact `Verification-Feature-ID` marker.
3. Exact immutable repository/PR, issue, task, provider object, or content-object alias.
4. Exact active, non-default temporal branch alias when project policy permits it.
5. Create a new feature only when no exact identity exists and creation is allowed.

Never bind or merge from fuzzy title/description similarity. Treat default/shared branches as non-authoritative. Surface conflicting aliases as ambiguity.

## Preserve stable item identity

- Use the server-issued immutable `item_id` for every existing or retired item operation.
- Use `item_id: null` only for a genuinely new item.
- Keep `semantic_key` stable and invariant-focused, for example `ui.dashboard.range_filter_persists`.
- Rename a key explicitly with the existing item ID; never replace an item to fix its key.

## Select an operation

- `retain`: reuse the exact definition revision and decision.
- `editorial_update`: wording/presentation only; the server may append a validity-preservation event.
- `material_update`: action, expectation, precondition, target, risk, required flag, or behavior changed.
- `add`: new invariant; always pending.
- `retire`: remove from active scope while preserving history.
- `reopen`: implementation changed under a still-relevant definition.
- `rename_key`: correct the key without changing identity; classify content impact separately.

Give a bounded human-readable reason for material updates, reopening, and retirement.

## Coverage anchors

Attach at least one deterministic anchor to every active item. Prefer observed or registered anchors:

```text
file:<repository-relative-path>
route:<method-or-action>:<route-or-screen-identifier>
api:<method-or-operation>:<endpoint-or-contract-identifier>
component:<public-component-or-view-identifier>
config:<configuration-key-or-manifest-identifier>
data:<schema-collection-field-or-record-identifier>
content:<provider-or-document-identifier>
global:<cross-surface-contract-identifier>
```

Use `global:*` only when a narrower surface cannot represent the dependency, and explain why.

Every adapter-observed changed anchor must be covered by a retained/reopened/new/retired item, a structured known limit, or a policy-approved exception. Never suppress an uncovered surface.

## Decision preservation

- Reordering never invalidates.
- Verified editorial equivalence may preserve through a server-authored append-only validity event.
- Material definition changes or changed-anchor intersection invalidate the effective decision and derive `ready_for_recheck`.
- New items remain pending.
- Restored retired items never return silently accepted.
- Declines remain declined until feedback is explicitly addressed and matching reconciliation succeeds.
- Human decisions, comments, evidence, and acceptance are immutable to agent credentials.

## Concurrency and idempotency

Submit `base_checklist_revision`. On `REVISION_CONFLICT`, refetch, re-evaluate, and submit a new logical write with a new idempotency key. Never use last-write-wins.

Reuse an idempotency key only for an exact canonical network retry. Reusing it with different content is an error.

## Acceptance impact

Invalidate prior feature acceptance when a required item is added/restored/made material/reopened, a blocking limit appears, or policy requires new sign-off. Editorial, reorder, or optional-only revisions may preserve acceptance, while their unverified optional boundary remains visible.
