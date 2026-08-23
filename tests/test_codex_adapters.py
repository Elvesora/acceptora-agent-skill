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
ADAPTER_ROOT = PACKAGE_ROOT / "adapters" / "codex"


def load_adapter(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, ADAPTER_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    return module


TASK_START = load_adapter("acceptora_codex_task_start", "task_start.py")
STOP = load_adapter("acceptora_codex_stop", "stop.py")


class CodexTemplateTest(unittest.TestCase):
    def test_hook_template_uses_only_pinned_runtime_placeholders(self) -> None:
        template = json.loads((ADAPTER_ROOT / "hooks.json.example").read_text(encoding="utf-8"))

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
                    for field in ("command", "commandWindows"):
                        self.assertIn("{{PYTHON_COMMAND}}", hook[field])
                        self.assertIn("{{RUNTIME_ROOT}}", hook[field])
                        self.assertNotIn("{{SKILL_ROOT", hook[field])
                        self.assertNotIn("python3 ", hook[field])
                        self.assertNotIn("py -3 ", hook[field])

    def test_mcp_template_uses_a_fixed_token_variable_and_write_approval(self) -> None:
        body = (PACKAGE_ROOT / "config" / "codex-mcp.example.toml").read_text(encoding="utf-8")
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")

        self.assertIn('url = "https://verify.example.test/mcp"', body)
        self.assertIn('bearer_token_env_var = "ACCEPTORA_AGENT_TOKEN"', body)
        self.assertIn('default_tools_approval_mode = "writes"', body)
        self.assertNotIn("ACCEPTORA_MCP_URL", body)
        self.assertIn("https://developers.openai.com/codex/config-reference", setup)
        self.assertIn("confirm the expected write-tool prompt", setup)


class CodexTaskStartAdapterTest(unittest.TestCase):
    def test_session_start_emits_a_non_blocking_update_notice(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "SessionStart"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update", return_value="Update available.") as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"continue": True, "systemMessage": "Update available."},
            json.loads(stdout.getvalue()),
        )
        capture.assert_called_once_with(event, "codex")
        update.assert_called_once_with(event)

    def test_current_release_remains_silent(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "SessionStart"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline"),
            patch.object(TASK_START, "check_for_skill_update", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual("", stdout.getvalue())

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
                "continue": True,
                "systemMessage": (
                    "Agent Verification update check warning: the Git check failed safely; "
                    "no skill source was fetched and no setup files were changed."
                ),
            },
            json.loads(stdout.getvalue()),
        )
        capture.assert_called_once_with(event, "codex")
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
            {
                "continue": True,
                "systemMessage": "Agent Verification baseline warning: capture failed Update available.",
            },
            json.loads(stdout.getvalue()),
        )
        update.assert_called_once_with(event)


class CodexStopAdapterTest(unittest.TestCase):
    def test_block_uses_the_codex_continuation_decision_contract(self) -> None:
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
            {"decision": "block", "reason": "Synchronization is incomplete."},
            json.loads(stdout.getvalue()),
        )
        self.assertNotIn("continue", json.loads(stdout.getvalue()))
        evaluate.assert_called_once_with(event, "codex")

    def test_pass_with_a_message_remains_visible_and_non_blocking(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(STOP, "read_event", return_value={}),
            patch.object(
                STOP,
                "evaluate_completion_gate",
                return_value=SimpleNamespace(block=False, message="Loop protection allowed completion."),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = STOP.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"continue": True, "systemMessage": "Loop protection allowed completion."},
            json.loads(stdout.getvalue()),
        )

    def test_adapter_failure_is_visible_and_fails_open(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(STOP, "read_event", side_effect=ValueError("invalid event")),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = STOP.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "continue": True,
                "systemMessage": "Agent Verification hook failed safely: invalid event",
            },
            json.loads(stdout.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
