## Summary

Describe the smallest public behavior changed.

## Boundaries

- [ ] Application and package logic remain separate.
- [ ] No shared client configuration, secret value, hook runtime, or unrelated release flow was added.
- [ ] Any API/MCP contract change is synchronized with the application and package.
- [ ] Documentation describes current behavior but is not used as a test contract.

## Functional verification

List the exact focused tests and runtime checks performed.

- [ ] Relevant focused tests pass.
- [ ] No `__pycache__`, `.pyc`, or `.pyo` exists in the checkout.
- [ ] `git diff --check` passes.
