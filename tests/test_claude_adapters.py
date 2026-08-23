from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = PACKAGE_ROOT / "adapters" / "claude"


def load_adapter(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, ADAPTER_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    return module


TASK_START = load_adapter("acceptora_claude_task_start", "task_start.py")
STOP = load_adapter("acceptora_claude_stop", "stop.py")


class ClaudeTemplateTest(unittest.TestCase):
    def test_hook_templates_use_only_pinned_runtime_placeholders(self) -> None:
        for filename in ("settings.json.example", "settings.windows.json.example"):
            template = json.loads((ADAPTER_ROOT / filename).read_text(encoding="utf-8"))

            self.assertEqual({"SessionStart", "UserPromptSubmit", "Stop"}, set(template["hooks"]))
            self.assertEqual(
                {"SessionStart": 60, "UserPromptSubmit": 60, "Stop": 120},
                {
                    event: definitions[0]["hooks"][0]["timeout"]
                    for event, definitions in template["hooks"].items()
                },
            )
            for definitions in template["hooks"].values():
                for definition in definitions:
                    for hook in definition["hooks"]:
                        self.assertIn("{{PYTHON_COMMAND}}", hook["command"])
                        self.assertIn("{{RUNTIME_ROOT}}", hook["command"])
                        self.assertNotIn("{{SKILL_ROOT", hook["command"])
                        self.assertNotIn("python3 ", hook["command"])
                        self.assertNotIn("py -3 ", hook["command"])

    def test_mcp_template_has_an_explicit_rendered_url_and_fixed_token_name(self) -> None:
        template = json.loads(
            (PACKAGE_ROOT / "config" / "claude-mcp.example.json").read_text(encoding="utf-8")
        )
        server = template["mcpServers"]["acceptora"]

        self.assertEqual("http", server["type"])
        self.assertEqual("https://verify.example.test/mcp", server["url"])
        self.assertEqual("Bearer ${ACCEPTORA_AGENT_TOKEN}", server["headers"]["Authorization"])
        self.assertNotIn("ACCEPTORA_MCP_URL", server["url"])


class ClaudeTaskStartAdapterTest(unittest.TestCase):
    def test_captures_the_event_with_the_claude_code_integration_name(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update", return_value=None) as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual("", stdout.getvalue())
        capture.assert_called_once_with(event, "claude-code")
        update.assert_called_once_with(event)

    def test_session_start_emits_a_non_blocking_update_notice(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "SessionStart"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline"),
            patch.object(TASK_START, "check_for_skill_update", return_value="Update available."),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual({"systemMessage": "Update available."}, json.loads(stdout.getvalue()))

    def test_update_failure_is_visible_and_does_not_block_the_session(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "SessionStart"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update", side_effect=RuntimeError("attacker text")),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "systemMessage": (
                    "Agent Verification update check warning: the Git check failed safely; "
                    "no skill source was fetched and no setup files were changed."
                ),
            },
            json.loads(stdout.getvalue()),
        )
        capture.assert_called_once_with(event, "claude-code")
        self.assertNotIn("attacker text", stdout.getvalue())

    def test_baseline_failure_does_not_suppress_the_update_notice(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "SessionStart"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline", side_effect=RuntimeError("capture failed")),
            patch.object(TASK_START, "check_for_skill_update", return_value="Update available.") as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"systemMessage": "Agent Verification baseline warning: capture failed Update available."},
            json.loads(stdout.getvalue()),
        )
        update.assert_called_once_with(event)


class ClaudeStopAdapterTest(unittest.TestCase):
    def test_block_uses_the_claude_stop_decision_contract(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace"}
        stdout = io.StringIO()

        with (
            patch.object(STOP, "read_event", return_value=event),
            patch.object(
                STOP,
                "evaluate_completion_gate",
                return_value=SimpleNamespace(block=True, message="Synchronization is incomplete."),
            ) as evaluate,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = STOP.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "decision": "block",
                "reason": "Synchronization is incomplete.",
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": "Synchronization is incomplete.",
                },
            },
            json.loads(stdout.getvalue()),
        )
        evaluate.assert_called_once_with(event, "claude-code")

    def test_adapter_failure_is_visible_and_fails_open(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(STOP, "read_event", side_effect=ValueError("invalid event")),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = STOP.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"systemMessage": "Agent Verification hook failed safely: invalid event"},
            json.loads(stdout.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
