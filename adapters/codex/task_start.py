#!/usr/bin/env python3
"""Codex SessionStart/UserPromptSubmit adapter: read guidance, then capture baseline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hook_runtime import (  # noqa: E402
    capture_task_baseline,
    check_for_skill_update,
    instruction_additional_context,
    prepare_verification_instructions,
    read_event,
)


PREFLIGHT_FAILURE = (
    "Agent Verification instruction preflight failed safely; fresh owner guidance could not be validated. "
    "Retry after restoring the authenticated Acceptora project connection."
)


def main() -> int:
    try:
        event = read_event()
    except Exception as error:
        sys.stdout.write(json.dumps({"continue": True, "systemMessage": f"Agent Verification baseline warning: {error}"}) + "\n")
        return 0

    event_name = str(event.get("hook_event_name") or event.get("event_name") or "")
    try:
        instruction_snapshot = prepare_verification_instructions(event, "codex")
    except Exception:
        output = (
            {"decision": "block", "reason": PREFLIGHT_FAILURE}
            if event_name == "UserPromptSubmit"
            else {"continue": True, "systemMessage": PREFLIGHT_FAILURE}
        )
        sys.stdout.write(json.dumps(output) + "\n")
        return 0

    messages: list[str] = []
    try:
        capture_task_baseline(event, "codex")
    except Exception as error:
        messages.append(f"Agent Verification baseline warning: {error}")

    try:
        update_notice = check_for_skill_update(event)
    except Exception:
        update_notice = (
            "Agent Verification update check warning: the Git check failed safely; "
            "no skill source was fetched and no setup files were changed."
        )
    if update_notice:
        messages.append(update_notice)
    output: dict[str, object] = {"continue": True}
    if instruction_snapshot is not None:
        output["hookSpecificOutput"] = {
            "hookEventName": event_name,
            "additionalContext": instruction_additional_context(instruction_snapshot),
        }
    if messages:
        output["systemMessage"] = " ".join(messages)
    if len(output) > 1:
        sys.stdout.write(json.dumps(output) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
