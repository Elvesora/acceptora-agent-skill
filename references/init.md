# Init: install, reconnect, or update

`init` connects a trusted Git worktree to one Acceptora project. It is setup, not verification. Do not reconcile a checklist, invent a third installer, or apply file changes until the user explicitly accepts the exact plan digest.

The authoritative procedure is [SETUP.md](../SETUP.md) in a fresh production source checkout, section **Coding-agent install or update**. Read that file completely from the checkout before changing the target. This reference only routes you there and restates the gates you must not skip.

## When this owns the turn

Follow this file when the user invoked `init`, asked to install, connect, set up, reconnect, or update the Acceptora skill, or when a `SessionStart` update notice routed here.

Do not use this file for ordinary finished implementation work. That is the completion workflow in [SKILL.md](../SKILL.md).

## Inputs

When an Acceptora onboarding prompt supplies `client`, `target`, `project_id`, and `acceptora_origin`, use those values exactly; do not ask the user to repeat them.

When a `SessionStart` update notice routes here, its printed cache path is `<runtime-root>/state/skill-update.json`. Use `<runtime-root>/package/scripts/install.py` as the existing trusted installer and `<runtime-root>/install-receipt.json` as the installed identity record. Read `client`, `target_root`, `project_id`, `api_base_url` (the `acceptora_origin`), and `runtime_base` from the receipt's `inputs`, then validate that receipt with the trusted installer's **Status and rollback** command in [SETUP.md](../SETUP.md) before using those values. Stop if the paths or validation do not match.

Resolve an onboarding `target: current Git worktree` to its absolute Git worktree root; for an update notice, use the validated receipt's `target_root`.

## Source

This installed skill copy is not the installer source. Obtain either:

- a fresh clone of `https://github.com/Elvesora/acceptora-agent-skill` at `main`, outside the target worktree; or
- the intact canonical ZIP extracted outside the target worktree, with `acceptora-agent-skill-provenance.json` kept beside the extracted `acceptora` directory.

Never install from another remote, tag, mirror, or already-installed copy. Never extract the ZIP into the target project root. Never run the installer from inside the target repository.

Read [SETUP.md](../SETUP.md) from that checkout. Follow **Obtain the production source**, then **Agent client prerequisites**.

## Credential

Confirm `ACCEPTORA_AGENT_TOKEN` exists in the coding-agent process environment without printing, copying, or storing its value. If it is absent, stop and explain how the user can set it outside the conversation. Never request the token value.

## New installation

Follow **Plan** in [SETUP.md](../SETUP.md). Prefer the onboarding `client` value when it was supplied. Otherwise omit `--client` and let the installer detect a unique agent environment or project marker; stop if detection is ambiguous. Use `--format text --output "<external-path>/acceptora-install-plan.json"` so the user can read the human plan while `apply` still receives JSON. Show the full source commit with the plan review, and pause for explicit approval of the exact `plan_sha256`. Never approve a plan digest, client trust, hooks, or MCP tool permissions on the user's behalf.

Only after that exact digest is accepted, follow **Apply**, **Client review**, **Status and rollback** (status only), and finally **Health check and connection confirmation**. The last command must use `--confirm-connection`. It marks the project connected only after every contract check passes.

## Update

First use the installed trusted installer to inspect status and create a rollback plan. Pause for explicit approval of the exact `rollback_plan_sha256`, apply that rollback, and only then create a new installation plan from this fresh checkout. Never reuse a plan created before rollback. Continue with the new-install sequence above.

Do not run `git pull` inside the installed runtime.

## Finish

Perform the mechanical commands yourself. Stop at every approval or missing prerequisite required by [SETUP.md](../SETUP.md). Do not make unrelated project changes.

Report the source commit, receipt and runtime locations, installer status, client discovery result, health-check result, and explicit connection confirmation. Then stop.

If the user next asks whether the install worked, load [doctor.md](doctor.md). If they continue implementation, return to the completion workflow in [SKILL.md](../SKILL.md).
