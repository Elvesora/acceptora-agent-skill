# Universal pattern

Apply to every eligible change.

## Required coverage prompts

- Confirm the commissioned goal and explicit acceptance boundary.
- Identify every adapter-observed changed surface and map it to an item, structured limit, or allowed exception.
- State environment, base URL/target, safe fixtures, permissions, and prerequisites.
- Record automated checks actually run, including failures and omissions.
- Verify the primary happy path from the user's perspective.
- Verify relevant validation, error, empty, loading, permission, retry, and recovery states.
- Check nearby regression boundaries that share the changed contract.
- Warn about destructive, external, production, billing, messaging, or irreversible side effects.
- Provide cleanup/reset steps for created state.
- Record unavailable checks as structured known limits.
- End with explicit criteria for human feature acceptance.

## Item quality

Write one independently decidable observable claim per item. Use exact actions and expected outcomes. Do not add generic “looks correct” or “test the feature” checks.
