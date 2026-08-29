from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = PACKAGE_ROOT / "adapters" / "antigravity"
TOKEN_ENV = "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)


def load_adapter(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, ADAPTER_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVENT = load_adapter("antigravity_event", "antigravity_event.py")
TASK_START = load_adapter("acceptora_antigravity_task_start", "task_start.py")
STOP = load_adapter("acceptora_antigravity_stop", "stop.py")
BRIDGE = load_adapter("acceptora_antigravity_mcp_bridge", "mcp_stdio_bridge.py")


class AntigravityTemplateTest(unittest.TestCase):
    def test_hook_template_uses_named_pre_invocation_and_stop_hooks(self) -> None:
        template = json.loads((ADAPTER_ROOT / "hooks.json.example").read_text(encoding="utf-8"))

        self.assertEqual(["acceptora-target:{{RUNTIME_ID}}"], list(template))
        definition = template["acceptora-target:{{RUNTIME_ID}}"]
        self.assertEqual({"PreInvocation", "Stop"}, set(definition))
        self.assertEqual(60, definition["PreInvocation"][0]["timeout"])
        self.assertEqual(120, definition["Stop"][0]["timeout"])
        for event in ("PreInvocation", "Stop"):
            handler = definition[event][0]
            self.assertEqual({"type", "command", "timeout"}, set(handler))
            self.assertEqual("command", handler["type"])
            self.assertIn("{{PYTHON_COMMAND}}", handler["command"])
            self.assertIn("{{RUNTIME_ROOT}}/trusted_adapters/antigravity/", handler["command"])

    def test_mcp_template_uses_a_credential_safe_stdio_bridge(self) -> None:
        template = json.loads(
            (PACKAGE_ROOT / "config" / "antigravity-mcp.example.json").read_text(encoding="utf-8")
        )
        server = template["mcpServers"]["acceptora"]

        self.assertEqual("{{PYTHON_EXECUTABLE}}", server["command"])
        self.assertEqual(["-B", "-I"], server["args"][:2])
        self.assertIn("/package/adapters/antigravity/mcp_stdio_bridge.py", server["args"][2])
        self.assertIn("ACCEPTORA_AGENT_TOKEN_PROJ_REPLACE_WITH_PROJECT_ULID", server["args"])
        self.assertNotIn("headers", server)
        self.assertNotIn("serverUrl", server)
        self.assertIs(False, server["disabled"])


class AntigravityEventTest(unittest.TestCase):
    def test_normalizes_camel_case_event_for_the_pinned_workspace(self) -> None:
        event = {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT)],
            "invocationNum": 3,
        }

        normalized = EVENT.normalize_event(event, "PreInvocation", str(PACKAGE_ROOT))

        assert normalized is not None
        self.assertEqual(str(PACKAGE_ROOT.resolve()), normalized["cwd"])
        self.assertEqual("conversation-1", normalized["session_id"])
        self.assertEqual("conversation-1:3", normalized["turn_id"])
        self.assertEqual("PreInvocation", normalized["hook_event_name"])

    def test_returns_none_for_another_installed_project(self) -> None:
        event = {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT.parent)],
            "invocationNum": 0,
        }

        self.assertIsNone(EVENT.normalize_event(event, "PreInvocation", str(PACKAGE_ROOT)))


class AntigravityTaskStartAdapterTest(unittest.TestCase):
    def test_pre_invocation_injects_reader_directive_before_baseline(self) -> None:
        raw_event = {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT)],
            "invocationNum": 0,
        }
        reader_argv = (
            str(Path(sys.executable).resolve()),
            "-B",
            "-I",
            str((PACKAGE_ROOT / "scripts" / "read_instruction_snapshot.py").resolve()),
            "--project-id",
            "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )
        snapshot = SimpleNamespace(reader_argv=reader_argv)
        order: list[str] = []
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=raw_event),
            patch.object(TASK_START, "load_config", return_value={"target_root": str(PACKAGE_ROOT)}),
            patch.object(
                TASK_START,
                "prepare_verification_instructions",
                side_effect=lambda *_: order.append("instructions") or snapshot,
            ) as prepare,
            patch.object(
                TASK_START,
                "capture_task_baseline",
                side_effect=lambda *_: order.append("baseline"),
            ) as capture,
            patch.object(TASK_START, "check_for_skill_update", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(["instructions", "baseline"], order)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("run_command", payload["injectSteps"][0]["toolCall"]["name"])
        tool_args = payload["injectSteps"][0]["toolCall"]["args"]
        self.assertEqual(str(PACKAGE_ROOT), tool_args["Cwd"])
        self.assertIn("read_instruction_snapshot.py", tool_args["CommandLine"])
        self.assertIs(False, tool_args["RunPersistent"])
        self.assertIn("required first trajectory step", payload["injectSteps"][1]["ephemeralMessage"])
        normalized = prepare.call_args.args[0]
        self.assertEqual("conversation-1", normalized["session_id"])
        prepare.assert_called_once_with(normalized, "antigravity-cli")
        capture.assert_called_once_with(normalized, "antigravity-cli")

    def test_invalid_reader_argv_fails_closed_before_baseline(self) -> None:
        raw_event = {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT)],
            "invocationNum": 0,
        }
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=raw_event),
            patch.object(TASK_START, "load_config", return_value={"target_root": str(PACKAGE_ROOT)}),
            patch.object(
                TASK_START,
                "prepare_verification_instructions",
                return_value=SimpleNamespace(reader_argv=["mutable-list-is-not-trusted"]),
            ),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update") as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout.getvalue())
        self.assertIn("preflight failed safely", payload["injectSteps"][0]["ephemeralMessage"])
        capture.assert_not_called()
        update.assert_not_called()

    def test_preflight_failure_is_fixed_and_does_not_leak_or_capture(self) -> None:
        raw_event = {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT)],
            "invocationNum": 0,
        }
        stdout = io.StringIO()

        with (
            patch.object(TASK_START, "read_event", return_value=raw_event),
            patch.object(TASK_START, "load_config", return_value={"target_root": str(PACKAGE_ROOT)}),
            patch.object(
                TASK_START,
                "prepare_verification_instructions",
                side_effect=RuntimeError("OWNER-PRIVATE-INSTRUCTION"),
            ),
            patch.object(TASK_START, "capture_task_baseline") as capture,
            patch.object(TASK_START, "check_for_skill_update") as update,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout.getvalue())
        message = payload["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("Do not inspect, plan, run tools, or change", message)
        self.assertNotIn("OWNER-PRIVATE-INSTRUCTION", stdout.getvalue())
        capture.assert_not_called()
        update.assert_not_called()

    def test_another_project_is_a_neutral_noop(self) -> None:
        stdout = io.StringIO()
        raw_event = {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT.parent)],
            "invocationNum": 0,
        }

        with (
            patch.object(TASK_START, "read_event", return_value=raw_event),
            patch.object(TASK_START, "load_config", return_value={"target_root": str(PACKAGE_ROOT)}),
            patch.object(TASK_START, "prepare_verification_instructions") as prepare,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = TASK_START.main()

        self.assertEqual(0, exit_code)
        self.assertEqual({}, json.loads(stdout.getvalue()))
        prepare.assert_not_called()


class AntigravityStopAdapterTest(unittest.TestCase):
    def event(self, **overrides: object) -> dict[str, object]:
        return {
            "conversationId": "conversation-1",
            "workspacePaths": [str(PACKAGE_ROOT)],
            "executionNum": 1,
            "terminationReason": "model_stop",
            "fullyIdle": True,
            **overrides,
        }

    def test_continues_when_the_completion_gate_blocks(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(STOP, "read_event", return_value=self.event()),
            patch.object(STOP, "load_config", return_value={"target_root": str(PACKAGE_ROOT)}),
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
            {"decision": "continue", "reason": "Synchronization is incomplete."},
            json.loads(stdout.getvalue()),
        )
        normalized = evaluate.call_args.args[0]
        evaluate.assert_called_once_with(normalized, "antigravity-cli")

    def test_allows_success_and_skips_non_idle_or_error_stops(self) -> None:
        cases = [
            (self.event(), True),
            (self.event(fullyIdle=False), False),
            (self.event(terminationReason="error"), False),
        ]
        for event, should_evaluate in cases:
            with self.subTest(event=event), contextlib.ExitStack() as stack:
                stdout = io.StringIO()
                stack.enter_context(patch.object(STOP, "read_event", return_value=event))
                stack.enter_context(
                    patch.object(STOP, "load_config", return_value={"target_root": str(PACKAGE_ROOT)})
                )
                evaluate = stack.enter_context(
                    patch.object(
                        STOP,
                        "evaluate_completion_gate",
                        return_value=SimpleNamespace(block=False, message=None),
                    )
                )
                stack.enter_context(contextlib.redirect_stdout(stdout))

                self.assertEqual(0, STOP.main())
                self.assertEqual({"decision": "allow"}, json.loads(stdout.getvalue()))
                self.assertEqual(should_evaluate, evaluate.called)

    def test_failure_allows_completion_without_leaking_exception_text(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(STOP, "read_event", return_value=self.event()),
            patch.object(STOP, "load_config", return_value={"target_root": str(PACKAGE_ROOT)}),
            patch.object(STOP, "evaluate_completion_gate", side_effect=RuntimeError("PRIVATE")),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(0, STOP.main())

        self.assertEqual(
            {"decision": "allow", "reason": STOP.FAIL_OPEN_WARNING},
            json.loads(stdout.getvalue()),
        )
        self.assertNotIn("PRIVATE", stdout.getvalue())


class _Response:
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type, "Mcp-Session-Id": "session-1"}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request = None

    def open(self, request: object, timeout: float) -> _Response:
        self.request = request
        return self.response


class AntigravityMcpBridgeTest(unittest.TestCase):
    def test_forwards_json_rpc_with_bearer_from_memory_and_tracks_session(self) -> None:
        response_body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        bridge = BRIDGE.StdioHttpBridge("https://www.acceptora.com/mcp", TOKEN, 8)
        opener = _Opener(_Response(response_body))
        bridge.opener = opener

        responses = bridge.exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )

        self.assertEqual([{"jsonrpc": "2.0", "id": 1, "result": {}}], responses)
        self.assertEqual("session-1", bridge.session_id)
        assert opener.request is not None
        self.assertEqual(f"Bearer {TOKEN}", opener.request.get_header("Authorization"))
        self.assertEqual("2025-11-25", opener.request.get_header("Mcp-protocol-version"))

    def test_parses_sse_data_messages_and_rejects_redirects(self) -> None:
        body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        bridge = BRIDGE.StdioHttpBridge("https://www.acceptora.com/mcp", TOKEN, 8)
        bridge.opener = _Opener(_Response(body, "text/event-stream"))

        self.assertEqual(
            [{"jsonrpc": "2.0", "id": 1, "result": {}}],
            bridge.exchange({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        )

        error = urllib.error.HTTPError(
            "https://www.acceptora.com/mcp",
            302,
            "Found",
            {},
            None,
        )
        bridge.opener = SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
        with self.assertRaisesRegex(BRIDGE.BridgeError, "HTTP 302"):
            bridge.exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    def test_missing_token_fails_without_printing_the_environment_value(self) -> None:
        stderr = io.StringIO()
        secret = "not-a-valid-acceptora-token"
        with (
            patch.dict(os.environ, {TOKEN_ENV: secret}),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = BRIDGE.main(
                ["--server-url", "https://www.acceptora.com/mcp", "--token-env", TOKEN_ENV]
            )

        self.assertEqual(1, exit_code)
        self.assertIn("missing or malformed", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
