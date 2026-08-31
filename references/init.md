# Init: install, reconnect, or update

`init` connects a trusted Git worktree to one Acceptora project. It is setup, not verification. Do not reconcile a checklist, invent a third installer, or apply file changes until the user explicitly accepts the exact plan digest.

The authoritative procedure is [SETUP.md](../SETUP.md) in a fresh production source checkout, section **Coding-agent install or update**. Read that file completely from the checkout before changing the target. This reference only routes you there and restates the gates you must not skip.

## When this owns the turn

Follow this file when the user invoked `init`, asked to install, connect, set up, reconnect, or update the Acceptora skill, or when a task-start update notice routed here.

Do not use this file for ordinary finished implementation work. That is the completion workflow in [SKILL.md](../SKILL.md).

## Inputs

For onboarding, use the client fixed by `SETUP-CODEX.md`, `SETUP-CLAUDE-CODE.md`, or `SETUP-GEMINI-CLI.md`, and treat the Git worktree where the prompt was submitted as the target. Use the fixed origin `https://www.acceptora.com`.

Identify the project from the name of its exported `ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>` variable without printing the value. Derive `proj_<ULID>` from that name. If no matching name exists, use **Missing project credential** in [SETUP.md](../SETUP.md): first inspect filenames and Git metadata for an existing ignored project environment store, never its contents. The selected branch validates the private-chat key before either asking the user to place it in that file or storing it in the Windows current-user environment. If several variables or eligible stores exist and a validated receipt does not already bind this worktree, ask the user to identify the correct name or active file; do not guess.

When a task-start update notice routes here, its printed cache path is `<runtime-root>/state/skill-update.json`. Use `<runtime-root>/package/scripts/install.py` as the existing trusted installer and `<runtime-root>/install-receipt.json` as the installed identity record. Read `client`, `target_root`, `project_id`, `api_base_url` (the `acceptora_origin`), and `runtime_base` from the receipt's `inputs`, then validate that receipt with the trusted installer's **Status and rollback** command in [SETUP.md](../SETUP.md) before using those values. Stop if the paths or validation do not match.

Resolve the Git worktree where an onboarding prompt was submitted to its absolute root; for an update notice, use the validated receipt's `target_root`.

## Source

This installed skill copy is not the installer source. Obtain a fresh clone of `https://github.com/Elvesora/acceptora-agent-skill` at `main`, outside the target worktree.

Never install from another remote, tag, mirror, archive, or already-installed copy. Never run the installer from inside the target repository.

Read [SETUP.md](../SETUP.md) from that checkout. Follow **Obtain the production source**, then **Agent client prerequisites**.

## Credential

Confirm the selected project-derived variable exists in the coding-agent process environment without printing its value. If it is absent, follow **Missing project credential** in [SETUP.md](../SETUP.md) and do not plan. Never echo the supplied key. When a safe ignored project environment store exists, use validation-only mode, ask the user to place the exact derived variable there, and stop until a restarted client exposes it through the project's existing loader; never inspect the file. Otherwise use the documented Windows current-user recovery. Confirm only the derived project ID and variable name.

## New installation

Follow **Plan** in [SETUP.md](../SETUP.md). Pass the onboarding `client` value explicitly. If no value was supplied, identify the active supported client before planning and still pass `--client`; never infer Antigravity from `.agents`, which is also a Codex workspace layout. Use `--format text --output "<external-path>/acceptora-install-plan.json"` so the user can read the human plan while `apply` still receives JSON. Show the full source commit with the plan review, and pause for explicit approval of the exact `plan_sha256`. Never approve a plan digest, client trust, hooks, or MCP tool permissions on the user's behalf.

Only after that exact digest is accepted, follow **Apply**, **Client review**, **Status and rollback** (status only), and finally **Health check and connection confirmation**. The last command must use `--confirm-connection`. It marks the project connected only after every contract check passes.

## Update

First use the installed trusted installer to inspect status and create a rollback plan. Pause for explicit approval of the exact `rollback_plan_sha256`, apply that rollback, and only then create a new installation plan from this fresh checkout. Never reuse a plan created before rollback. Continue with the new-install sequence above.

Do not run `git pull` inside the installed runtime.

## Finish

Perform the mechanical commands yourself. Stop at every approval or missing prerequisite required by [SETUP.md](../SETUP.md). Do not make unrelated project changes.

Report the source commit, receipt and runtime locations, installer status, client discovery result, health-check result, and explicit connection confirmation. Then stop.

If the user next asks whether the install worked, load [doctor.md](doctor.md). If they continue implementation, return to the completion workflow in [SKILL.md](../SKILL.md).
