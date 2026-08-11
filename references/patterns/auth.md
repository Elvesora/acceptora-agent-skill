# Authentication and authorization pattern

Apply to sign-in, sessions, tokens, roles, policies, ownership, tenant boundaries, or permission-sensitive UI/API behavior.

## Coverage prompts

- Verify the intended authenticated and unauthenticated paths.
- Check allowed and denied users/resources separately; denial must not leak cross-tenant existence.
- Confirm session creation, renewal, expiry, logout, revocation, and remember-me behavior when changed.
- Verify CSRF/state/redirect handling and safe return URLs.
- Check direct URL/API access, not only hidden UI controls.
- Verify token creation, one-time display, hashing, scopes, expiry, rotation, and revocation when applicable.
- Test project/workspace isolation across reads, writes, search, files, queues, and exports touched by the change.
- Confirm audit attribution without recording credentials.

Use route/API, policy, session-contract, workspace/project, and credential anchors. Treat any unverified cross-tenant boundary as blocking unless project policy says otherwise.
