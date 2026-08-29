#!/usr/bin/env python3
"""Normalize Antigravity CLI hook events for the shared Acceptora runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class AntigravityEventError(RuntimeError):
    pass


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def normalize_event(
    event: dict[str, Any],
    event_name: str,
    target_root: str,
) -> dict[str, Any] | None:
    """Return a legacy-compatible event, or None for another installed project."""

    expected = Path(target_root)
    if not expected.is_absolute():
        raise AntigravityEventError("the pinned Antigravity target root is invalid")
    expected = expected.resolve()

    workspace_paths = event.get("workspacePaths")
    if not isinstance(workspace_paths, list) or any(
        not isinstance(value, str) or not value for value in workspace_paths
    ):
        raise AntigravityEventError("Antigravity hook input has invalid workspacePaths")

    is_target_workspace = False
    for value in workspace_paths:
        observed = Path(value)
        if not observed.is_absolute():
            continue
        observed = observed.resolve()
        if _same_path(observed, expected):
            is_target_workspace = True
            break
        try:
            observed.relative_to(expected)
        except ValueError:
            continue
        is_target_workspace = True
        break

    if not is_target_workspace:
        return None

    conversation_id = event.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise AntigravityEventError("Antigravity hook input has no conversationId")

    normalized = dict(event)
    normalized.update(
        {
            "cwd": str(expected),
            "event_name": event_name,
            "hook_event_name": event_name,
            "session_id": conversation_id,
        }
    )
    sequence = event.get("invocationNum") if event_name == "PreInvocation" else event.get("executionNum")
    normalized["turn_id"] = f"{conversation_id}:{sequence}" if isinstance(sequence, int) else conversation_id

    return normalized


