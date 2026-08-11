#!/usr/bin/env python3
"""Codex Stop adapter: enforce exact-source checklist synchronization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hook_runtime import evaluate_completion_gate, read_event  # noqa: E402


def main() -> int:
    try:
        decision = evaluate_completion_gate(read_event(), "codex")
    except Exception as error:
        decision_message = f"Agent Verification hook failed safely: {error}"
        sys.stdout.write(json.dumps({"continue": True, "systemMessage": decision_message}) + "\n")
        return 0
    if decision.block:
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "block",
                    "reason": decision.message,
                }
            )
            + "\n"
        )
    elif decision.message:
        sys.stdout.write(json.dumps({"continue": True, "systemMessage": decision.message}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
