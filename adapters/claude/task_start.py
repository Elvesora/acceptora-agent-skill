#!/usr/bin/env python3
"""Claude Code SessionStart/UserPromptSubmit adapter: capture source baseline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hook_runtime import capture_task_baseline, check_for_skill_update, read_event  # noqa: E402


def main() -> int:
    try:
        event = read_event()
    except Exception as error:
        sys.stdout.write(json.dumps({"systemMessage": f"Agent Verification baseline warning: {error}"}) + "\n")
        return 0

    messages: list[str] = []
    try:
        capture_task_baseline(event, "claude-code")
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
    if messages:
        sys.stdout.write(json.dumps({"systemMessage": " ".join(messages)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
