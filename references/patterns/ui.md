# UI and responsive pattern

Apply when screens, routes, views, components, layouts, styling, navigation, forms, command surfaces, or rendered states change on web, mobile, desktop, or another interactive platform.

## Coverage prompts

- Open the exact screen, route, view, or interaction surface and enter the workflow from a realistic preceding state.
- Verify labels, hierarchy, content, controls, default state, and intended primary action.
- Exercise success plus applicable loading, empty, validation, error, disabled, and permission states.
- Verify persistence across refresh/back navigation when expected.
- Check the viewport, window, device, orientation, or form factors relevant to the changed surface; name dimensions when known.
- Check overflow, clipping, sticky controls, safe-area behavior, long text, and large data.
- Verify keyboard reachability, visible focus, semantic labels, status not conveyed only by color, and predictable focus after updates.
- Check reduced motion and touch target behavior where applicable.
- Inspect platform-appropriate browser, device, application, or network diagnostics only when they can reveal a changed-surface failure.
- Verify nearby navigation and shared-component regressions.

Use `component:` for views or interactive units, `route:` for navigable surfaces, `file:` for implementation sources, and `global:` for shared interaction contracts. Keep visual preference checks concrete enough to accept or decline.
