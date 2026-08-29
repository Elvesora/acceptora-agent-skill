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
ADAPTER_ROOT = PACKAGE_ROOT / "adapters" / "gemini"


def load_adapter(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, ADAPTER_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    return module


TASK_START = load_adapter("acceptora_gemini_task_start", "task_start.py")
AFTER_AGENT = load_adapter("acceptora_gemini_after_agent", "after_agent.py")


class GeminiTemplateTest(unittest.TestCase):
    def test_hook_template_uses_supported_gemini_events_and_command_fields(self) -> None:
        template = json.loads((ADAPTER_ROOT / "hooks.json.example").read_text(encoding="utf-8"))

        self.assertEqual(
            {"SessionStart", "BeforeAgent", "AfterAgent"},
            set(template["hooks"]),
        )
        self.assertEqual(
            {"SessionStart": 60_000, "BeforeAgent": 60_000, "AfterAgent": 120_000},
            {
                event: definitions[0]["hooks"][0]["timeout"]
                for event, definitions in template["hooks"].items()
            },
        )
        self.assertNotIn("matcher", template["hooks"]["SessionStart"][0])
        self.assertNotIn("matcher", template["hooks"]["BeforeAgent"][0])
        self.assertNotIn("matcher", template["hooks"]["AfterAgent"][0])

        for definitions in template["hooks"].values():
            for definition in definitions:
                for hook in definition["hooks"]:
                    self.assertEqual(
                        {"name", "type", "command", "timeout", "description"},
                        set(hook),
                    )
                    self.assertEqual("command", hook["type"])
                    self.assertIn("{{PYTHON_COMMAND}}", hook["command"])
                    self.assertIn("{{PYTHON_COMMAND}}", hook["command"])
                    self.assertIn("{{RUNTIME_ROOT}}", hook["command"])
                    self.assertNotIn("{{SKILL_ROOT", hook["command"])

    def test_mcp_template_uses_streamable_http_with_explicit_user_approval(self) -> None:
        template = json.loads(
            (PACKAGE_ROOT / "config" / "gemini-mcp.example.json").read_text(encoding="utf-8")
        )
        server = template["mcpServers"]["acceptora"]

        self.assertEqual("https://verify.example.test/mcp", server["httpUrl"])
        self.assertNotIn("ACCEPTORA_MCP_URL", server["httpUrl"])
        self.assertEqual(
            "Bearer ${ACCEPTORA_AGENT_TOKEN_PROJ_REPLACE_WITH_PROJECT_ULID}",
            server["headers"]["Authorization"],
        )
        self.assertIs(False, server["trust"])
        self.assertNotIn("type", server)
        self.assertNotIn("url", server)


class GeminiTaskStartAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight_patcher = patch.object(TASK_START, "prepare_verification_instructions", return_value=None)
        self.preflight_patcher.start()
        self.addCleanup(self.preflight_patcher.stop)

    def test_before_agent_preflight_is_model_visible_and_runs_before_baseline_capture(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "BeforeAgent"}
        snapshot = SimpleNamespace()
        order: list[str] = []
        stdout = io.StringIO()

        with (
            patch.object(
                TASK_START,
                "prepare_verification_instructions",
                side_effect=lambda *_: order.append("instructions") or snapshot,
            ),
            patch.object(
                TASK_START,
                "capture_task_baseline",
                side_effect=lambda *_: order.append("baseline"),
            ),
            patch.object(TASK_START, "instruction_additional_context", return_value="Fixed preflight directive."),
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "check_for_skill_update", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(["instructions", "baseline"], order)
        self.assertEqual(
            {
                "decision": "allow",
                "hookSpecificOutput": {
                    "hookEventName": "BeforeAgent",
                    "additionalContext": "Fixed preflight directive.",
                },
            },
            json.loads(stdout.getvalue()),
        )

    def test_before_agent_preflight_failure_denies_without_leaking_or_capturing_baseline(self) -> None:
        event = {"session_id": "session-1", "cwd": "/workspace", "hook_event_name": "BeforeAgent"}
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(
                TASK_START,
                "prepare_verification_instructions",
                side_effect=RuntimeError("OWNER-ANALYSIS-GUIDANCE"),
            ),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update") as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("deny", payload["decision"])
        self.assertNotIn("OWNER-ANALYSIS-GUIDANCE", stdout.getvalue())
        capture.assert_not_called()
        update.assert_not_called()

    def test_captures_the_event_with_the_gemini_integration_name(self) -> None:
        event = {
            "session_id": "session-1",
            "cwd": "/workspace",
            "hook_event_name": "BeforeAgent",
            "prompt": "Implement the requested change.",
        }
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=event),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update", return_value=None) as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual({}, json.loads(stdout.getvalue()))
        capture.assert_called_once_with(event, "gemini-cli")
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
        capture.assert_called_once_with(event, "gemini-cli")
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

    def test_reports_a_valid_non_blocking_warning_when_capture_fails(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", side_effect=ValueError("invalid event")),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"systemMessage": "Agent Verification baseline warning: invalid event"},
            json.loads(stdout.getvalue()),
        )


class GeminiAfterAgentAdapterTest(unittest.TestCase):
    def test_denies_the_response_to_trigger_a_retry_when_the_gate_blocks(self) -> None:
        event = {
            "session_id": "session-1",
            "cwd": "/workspace",
            "hook_event_name": "AfterAgent",
            "prompt": "Implement the requested change.",
            "prompt_response": "Done.",
            "stop_hook_active": False,
        }
        stdout = io.StringIO()

        with (
            patch.object(AFTER_AGENT, "read_event", return_value=event),
            patch.object(
                AFTER_AGENT,
                "evaluate_completion_gate",
                return_value=SimpleNamespace(block=True, message="Synchronization is incomplete."),
            ) as evaluate,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = AFTER_AGENT.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "decision": "deny",
                "reason": "Synchronization is incomplete.",
                "systemMessage": "Synchronization is incomplete.",
            },
            json.loads(stdout.getvalue()),
        )
        evaluate.assert_called_once_with(event, "gemini-cli")

    def test_allows_the_response_when_the_gate_passes(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(AFTER_AGENT, "read_event", return_value={"stop_hook_active": True}),
            patch.object(
                AFTER_AGENT,
                "evaluate_completion_gate",
                return_value=SimpleNamespace(block=False, message=None),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = AFTER_AGENT.main()

        self.assertEqual(0, exit_code)
        self.assertEqual({"decision": "allow"}, json.loads(stdout.getvalue()))

    def test_preserves_a_visible_loop_protection_message_when_allowing(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(AFTER_AGENT, "read_event", return_value={"stop_hook_active": True}),
            patch.object(
                AFTER_AGENT,
                "evaluate_completion_gate",
                return_value=SimpleNamespace(block=False, message="Loop protection allowed completion."),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = AFTER_AGENT.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "decision": "allow",
                "systemMessage": "Loop protection allowed completion.",
            },
            json.loads(stdout.getvalue()),
        )

    def test_fails_open_with_a_visible_warning_when_the_adapter_raises(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(AFTER_AGENT, "read_event", side_effect=ValueError("invalid event")),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = AFTER_AGENT.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "decision": "allow",
                "systemMessage": "Agent Verification hook failed safely: invalid event",
            },
            json.loads(stdout.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
