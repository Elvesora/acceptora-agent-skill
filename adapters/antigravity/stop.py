#!/usr/bin/env python3
"""Antigravity Stop adapter: enforce exact-source checklist synchronization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

source_path = Path(__file__).resolve()
sys.path.insert(0, str(source_path.parent))
sys.path.insert(0, str(source_path.parents[2] / "package" / "adapters" / "antigravity"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from antigravity_event import normalize_event  # noqa: E402
from hook_runtime import evaluate_completion_gate, load_config, read_event  # noqa: E402


FAIL_OPEN_WARNING = "Agent Verification completion hook failed safely; completion was allowed."


def _emit(decision: str, reason: str | None = None) -> None:
    payload = {"decision": decision}
    if reason:
        payload["reason"] = reason
    sys.stdout.write(json.dumps(payload) + "\n")


def main() -> int:
    try:
        raw_event = read_event()
        config = load_config(Path.cwd())
        target_root = config.get("target_root")
        if not isinstance(target_root, str):
            raise RuntimeError("the pinned Antigravity target root is unavailable")
        event = normalize_event(raw_event, "Stop", target_root)
    except Exception:
        _emit("allow", FAIL_OPEN_WARNING)
        return 0

    if event is None or raw_event.get("fullyIdle") is not True:
        _emit("allow")
        return 0
    if raw_event.get("terminationReason") != "model_stop":
        _emit("allow")
        return 0

    try:
        decision = evaluate_completion_gate(event, "antigravity-cli")
    except Exception:
        _emit("allow", FAIL_OPEN_WARNING)
        return 0

    if decision.block:
        _emit("continue", decision.message or "Manual-verification synchronization is incomplete.")
    else:
        _emit("allow", decision.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
