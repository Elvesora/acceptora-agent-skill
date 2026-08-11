# Bug-fix pattern

Apply to corrective changes, including regressions and edge-case fixes.

## Coverage prompts

- Reproduce the original failure with the exact prior trigger, data, role, device, or timing when safe.
- Confirm the old failure no longer occurs and the intended result is observable.
- Verify the smallest adjacent happy path remains unchanged.
- Exercise the boundary conditions that caused the defect.
- Check persisted/cached/background state if the original symptom could outlive a request.
- Verify error handling does not hide or transform the issue into silent success.
- Record the regression test actually run and distinguish it from human verification.
- Include cleanup for any reproduction fixture.

Anchor both the root-cause surface and the user-observable surface. Do not reduce the checklist to “confirm bug is fixed.”
