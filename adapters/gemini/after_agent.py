#!/usr/bin/env python3
"""Gemini CLI AfterAgent adapter: enforce exact-source checklist synchronization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hook_runtime import evaluate_completion_gate, read_event  # noqa: E402


def main() -> int:
    try:
        decision = evaluate_completion_gate(read_event(), "gemini-cli")
    except Exception as error:
        output: dict[str, object] = {
            "decision": "allow",
            "systemMessage": f"Agent Verification hook failed safely: {error}",
        }
    else:
        if decision.block:
            message = decision.message or "Manual-verification synchronization is incomplete."
            output = {
                "decision": "deny",
                "reason": message,
                "systemMessage": message,
            }
        else:
            output = {"decision": "allow"}
            if decision.message:
                output["systemMessage"] = decision.message

    sys.stdout.write(json.dumps(output) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
