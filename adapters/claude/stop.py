#!/usr/bin/env python3
"""Claude Code Stop adapter: enforce exact-source checklist synchronization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hook_runtime import evaluate_completion_gate, read_event  # noqa: E402


def main() -> int:
    try:
        decision = evaluate_completion_gate(read_event(), "claude-code")
    except Exception as error:
        sys.stdout.write(json.dumps({"systemMessage": f"Agent Verification hook failed safely: {error}"}) + "\n")
        return 0
    if decision.block:
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "block",
                    "reason": decision.message,
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": decision.message,
                    },
                }
            )
            + "\n"
        )
    elif decision.message:
        sys.stdout.write(json.dumps({"systemMessage": decision.message}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
