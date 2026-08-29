from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = PACKAGE_ROOT / "tests" / "fixtures"
SCRIPTS = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import replay_offline_outbox as REPLAY  # noqa: E402
import write_offline_outbox as WRITER  # noqa: E402


FEATURE_ID = "feat_01J00000000000000000000001"
IDEMPOTENCY_KEY = "00000000-0000-4000-8000-000000000001"
TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)
SECOND_TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAA_" + ("B" * 48)
PROJECT_ID = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TOKEN_ENV = f"ACCEPTORA_AGENT_TOKEN_{PROJECT_ID.upper()}"


def sdk_validation_request() -> dict:
    fixture = json.loads((FIXTURES_ROOT / "sdk-validation-initial.json").read_text(encoding="utf-8"))
    return fixture["request"]


def gate_payload() -> dict:
    return json.loads((PACKAGE_ROOT / "tests" / "fixtures" / "hook-gate-payload.json").read_text(encoding="utf-8"))


class ReplayServer(ThreadingHTTPServer):
    state: dict


class ReplayHandler(BaseHTTPRequestHandler):
    server: ReplayServer

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size).decode("utf-8"))
        self.server.state["requests"].append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
                "session": self.headers.get("MCP-Session-Id"),
            }
        )
        if self.path == "/completion-gate":
            self.server.state["gate_payloads"].append(body)
            queued = self.server.state["gate_responses"]
            if queued:
                self._json(200, queued.pop(0))
                return
            self._json(
                200,
                {
                    "outcome": "pass",
                    "feature_id": FEATURE_ID,
                    "reason_code": "VERIFICATION_DOCUMENT_CURRENT",
                    "reason": "Current source is synchronized.",
                    "last_synchronized_digest": body.get("current_source_digest"),
                    "last_synchronized_checklist_revision": 1,
                    "recovery_instruction": None,
                    "correlation_id": "corr-replay-gate",
                },
            )
            return
        if self.path != "/mcp":
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "Not found."}})
            return

        method = body.get("method")
        if method == "initialize":
            if self.server.state["initialize_response"] is not None:
                self._json(200, self.server.state["initialize_response"], self.server.state["initialize_headers"])
                return
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "Acceptora Verification", "version": "1.0.0"},
                    },
                },
                {"MCP-Session-Id": "session-replay-test"},
            )
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/call":
            self.server.state["tool_calls"] += 1
            queued = self.server.state["tool_responses"]
            if queued:
                status, response = queued.pop(0)
                self._json(status, response)
                return
            arguments = body["params"]["arguments"]
            self.server.state["tool_payloads"].append(arguments)
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [],
                        "structuredContent": {
                            "feature_id": arguments["feature_id"],
                            "source_digest": arguments["source_digest"],
                            "source_revision_id": "src_01J00000000000000000000001",
                            "new_checklist_revision": 1,
                            "correlation_id": "corr-replay-tool",
                            "idempotency_replayed": self.server.state["tool_calls"] > 1,
                        },
                        "isError": False,
                    },
                },
            )
            return
        self._json(400, {"error": {"code": "PROTOCOL_ERROR", "message": "Unexpected method."}})

    def _json(self, status: int, value: dict, extra_headers: dict[str, str] | None = None) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, header_value in (extra_headers or {}).items():
            self.send_header(key, header_value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class RedirectHandler(BaseHTTPRequestHandler):
    server: ReplayServer

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        self.server.state["authorizations"].append(self.headers.get("Authorization"))
        self.send_response(302)
        self.send_header("Location", self.server.state["destination"])
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class RedirectDestinationHandler(BaseHTTPRequestHandler):
    server: ReplayServer

    def do_GET(self) -> None:  # noqa: N802
        self.server.state["authorizations"].append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def replay_server(
    tool_responses: list[tuple[int, dict]] | None = None,
    *,
    gate_responses: list[dict] | None = None,
    initialize_response: dict | None = None,
    initialize_headers: dict[str, str] | None = None,
) -> Iterator[tuple[ReplayServer, str]]:
    server = ReplayServer(("127.0.0.1", 0), ReplayHandler)
    server.state = {
        "requests": [],
        "tool_calls": 0,
        "tool_payloads": [],
        "gate_payloads": [],
        "gate_responses": list(gate_responses or []),
        "tool_responses": list(tool_responses or []),
        "initialize_response": initialize_response,
        "initialize_headers": initialize_headers or {"MCP-Session-Id": "session-replay-test"},
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def redirect_servers() -> Iterator[tuple[str, ReplayServer, ReplayServer]]:
    destination = ReplayServer(("127.0.0.1", 0), RedirectDestinationHandler)
    destination.state = {"authorizations": []}
    source = ReplayServer(("127.0.0.1", 0), RedirectHandler)
    source.state = {
        "authorizations": [],
        "destination": f"http://127.0.0.1:{destination.server_port}/capture",
    }
    threads = [
        threading.Thread(target=destination.serve_forever, daemon=True),
        threading.Thread(target=source.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{source.server_port}/redirect", source, destination
    finally:
        source.shutdown()
        destination.shutdown()
        source.server_close()
        destination.server_close()
        for thread in threads:
            thread.join(timeout=2)


class OfflineOutboxTest(unittest.TestCase):
    def test_project_derived_token_environment_is_enforced_before_transport_setup(self) -> None:
        with self.assertRaisesRegex(REPLAY.ReplayConfigurationError, TOKEN_ENV):
            REPLAY._settings(
                mock.Mock(),
                {
                    "mcp_url": "https://acceptora.example/mcp",
                    "project_id": PROJECT_ID,
                    "token_env": "ACCEPTORA_AGENT_TOKEN",
                },
                self.root / "outbox",
                self.root / "outbox",
                False,
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.outbox = self.root / "outbox"
        self.processed = self.outbox / "processed"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def settings(self, base_url: str, token: str, attempts: int = 3) -> REPLAY.ReplaySettings:
        return REPLAY.ReplaySettings(
            mcp_url=f"{base_url}/mcp",
            completion_gate_url=f"{base_url}/completion-gate",
            token=token,
            timeout_seconds=2,
            retry_attempts=attempts,
            retry_base_delay_seconds=0,
            max_retry_delay_seconds=0,
            processed_dir=self.processed,
            outbox_dir=self.outbox.resolve(),
        )

    def write_record(self, include_gate: bool = False) -> Path:
        payload = sdk_validation_request()
        path, _, _ = WRITER.write_outbox(
            payload,
            self.outbox,
            "reconcile_checklist",
            FEATURE_ID,
            IDEMPOTENCY_KEY,
            payload["source_digest"],
            gate_payload() if include_gate else None,
        )
        return path

    def test_writer_binds_feature_and_idempotency_arguments_to_the_exact_payload(self) -> None:
        payload = sdk_validation_request()

        with self.assertRaisesRegex(WRITER.OutboxError, "feature_id must exactly match"):
            WRITER.write_outbox(
                payload,
                self.outbox,
                "reconcile_checklist",
                "feat_01J00000000000000000000002",
                IDEMPOTENCY_KEY,
                payload["source_digest"],
            )
        with self.assertRaisesRegex(WRITER.OutboxError, "idempotency_key must exactly match"):
            WRITER.write_outbox(
                payload,
                self.outbox,
                "reconcile_checklist",
                FEATURE_ID,
                "00000000-0000-4000-8000-000000000002",
                payload["source_digest"],
            )

    def test_writer_replays_only_an_unchanged_existing_envelope(self) -> None:
        path = self.write_record()
        payload = sdk_validation_request()

        replay_path, _, replayed = WRITER.write_outbox(
            payload,
            self.outbox,
            "reconcile_checklist",
            FEATURE_ID,
            IDEMPOTENCY_KEY,
            payload["source_digest"],
        )
        self.assertTrue(replayed)
        self.assertEqual(path, replay_path)

        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["scope_summary"] = "Changed under the original hash"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaisesRegex(WRITER.OutboxError, "different operation or payload"):
            WRITER.write_outbox(
                payload,
                self.outbox,
                "reconcile_checklist",
                FEATURE_ID,
                IDEMPOTENCY_KEY,
                payload["source_digest"],
            )

    def test_secret_metadata_keys_are_rejected_before_write_and_replay_without_echo(self) -> None:
        synthetic_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("K" * 48)
        payload = sdk_validation_request()
        payload["source_descriptor"].setdefault("metadata", {})[synthetic_token] = "safe value"

        with self.assertRaises(WRITER.OutboxError) as write_error:
            WRITER.write_outbox(
                payload,
                self.outbox,
                "reconcile_checklist",
                FEATURE_ID,
                IDEMPOTENCY_KEY,
                payload["source_digest"],
            )
        self.assertNotIn(synthetic_token, str(write_error.exception))
        self.assertFalse(self.outbox.exists())

        for wrapped in (f"[REDACTED {synthetic_token}]", f"<{synthetic_token}>"):
            with self.subTest(wrapper=wrapped[0]):
                wrapped_payload = sdk_validation_request()
                wrapped_payload["source_descriptor"].setdefault("metadata", {})["note"] = wrapped
                with self.assertRaises(WRITER.OutboxError) as wrapped_error:
                    WRITER.write_outbox(
                        wrapped_payload,
                        self.outbox,
                        "reconcile_checklist",
                        FEATURE_ID,
                        IDEMPOTENCY_KEY,
                        wrapped_payload["source_digest"],
                    )
                self.assertNotIn(synthetic_token, str(wrapped_error.exception))

        sensitive_mutations = (
            ("password", "correct-horse-battery-staple"),
            ("apiKey", "abcdefghijklmnopqrstuvwx"),
            ("access_token", "opaquecredentialvalue12345"),
            ("Authorization", "Basic dXNlcjpwYXNz"),
            ("private_key", "-----BEGIN ENCRYPTED PRIVATE KEY-----\nsynthetic\n-----END ENCRYPTED PRIVATE KEY-----"),
            ("note", f"prefix{synthetic_token}"),
            ("note", f"{synthetic_token}suffix"),
            ("note", f"prefix{synthetic_token}suffix"),
        )
        for key, value in sensitive_mutations:
            with self.subTest(writer_key=key, writer_value=value[:12]):
                sensitive_payload = sdk_validation_request()
                sensitive_payload["source_descriptor"].setdefault("metadata", {})[key] = value
                with self.assertRaises(WRITER.OutboxError) as sensitive_error:
                    WRITER.write_outbox(
                        sensitive_payload,
                        self.outbox,
                        "reconcile_checklist",
                        FEATURE_ID,
                        IDEMPOTENCY_KEY,
                        sensitive_payload["source_digest"],
                    )
                self.assertNotIn(value, str(sensitive_error.exception))
                self.assertFalse(self.outbox.exists())

        path = self.write_record()
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            (synthetic_token, "safe value"),
            ("note", f"[REDACTED {synthetic_token}]"),
            ("note", f"<{synthetic_token}>"),
            *sensitive_mutations,
        )
        for key, value in mutations:
            with self.subTest(replay_value=value[:1]):
                envelope = json.loads(json.dumps(original))
                envelope["payload"]["source_descriptor"].setdefault("metadata", {})[key] = value
                envelope["canonical_payload_sha256"] = REPLAY._sha256(envelope["payload"])
                with self.assertRaises(REPLAY.EnvelopeError) as replay_error:
                    REPLAY.validate_envelope(path, envelope)
                self.assertNotIn(synthetic_token, str(replay_error.exception))

    def test_replay_uses_mcp_initialize_exact_tool_payload_and_completion_gate_then_archives(self) -> None:
        token = TOKEN
        path = self.write_record(include_gate=True)

        with replay_server() as (server, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, token))

        self.assertEqual("delivered", result["status"])
        self.assertEqual("pass", result["completion_gate_outcome"])
        self.assertFalse(path.exists())
        delivered = self.processed / path.name
        self.assertTrue(delivered.exists())
        receipt = json.loads(delivered.read_text(encoding="utf-8"))
        self.assertEqual("delivered", receipt["status"])
        self.assertEqual("pass", receipt["delivery_receipt"]["completion_gate_outcome"])
        self.assertEqual(sdk_validation_request(), server.state["tool_payloads"][0])
        self.assertEqual(gate_payload(), server.state["gate_payloads"][0])
        self.assertTrue(all(request["authorization"] == f"Bearer {token}" for request in server.state["requests"]))
        self.assertIsNone(server.state["requests"][0]["session"])
        self.assertTrue(all(request["session"] == "session-replay-test" for request in server.state["requests"][1:3]))

    def test_success_receipt_omits_reflected_token_correlation_ids(self) -> None:
        payload = sdk_validation_request()
        tool_success = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [],
                "structuredContent": {
                    "feature_id": FEATURE_ID,
                    "source_digest": payload["source_digest"],
                    "source_revision_id": "src_01J00000000000000000000001",
                    "new_checklist_revision": 1,
                    "correlation_id": TOKEN,
                    "idempotency_replayed": False,
                },
                "isError": False,
            },
        }
        gate_success = {
            "outcome": "pass",
            "feature_id": FEATURE_ID,
            "reason_code": "VERIFICATION_DOCUMENT_CURRENT",
            "reason": "Current source is synchronized.",
            "last_synchronized_digest": gate_payload()["current_source_digest"],
            "last_synchronized_checklist_revision": 1,
            "recovery_instruction": None,
            "correlation_id": TOKEN,
        }
        path = self.write_record(include_gate=True)

        with replay_server([(200, tool_success)], gate_responses=[gate_success]) as (_, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))

        self.assertEqual("delivered", result["status"])
        delivered = self.processed / path.name
        receipt = json.loads(delivered.read_text(encoding="utf-8"))
        self.assertIsNone(receipt["delivery_receipt"]["mcp_correlation_id"])
        self.assertIsNone(receipt["delivery_receipt"]["completion_gate_correlation_id"])
        self.assertNotIn(TOKEN, delivered.read_text(encoding="utf-8"))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_replay_keeps_the_record_pending_for_malformed_mcp_envelopes_and_empty_acknowledgements(self) -> None:
        path = self.write_record()
        malformed_initialize = {"id": 1, "result": {}}

        with replay_server(initialize_response=malformed_initialize) as (_, base_url):
            initialize_result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))

        self.assertEqual("failed", initialize_result["status"])
        self.assertEqual("PROTOCOL_ERROR", initialize_result["error_code"])
        self.assertTrue(path.exists())
        self.assertFalse(self.processed.exists())

        malformed_tool = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [], "structuredContent": {}, "isError": False},
        }
        with replay_server([(200, malformed_tool)]) as (_, base_url):
            tool_result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))

        self.assertEqual("failed", tool_result["status"])
        self.assertIn(tool_result["error_code"], {"FEATURE_ID_MISMATCH", "PROTOCOL_ERROR"})
        self.assertTrue(path.exists())
        self.assertFalse(self.processed.exists())

    def test_replay_keeps_the_record_pending_for_cross_identity_or_stale_gate_success(self) -> None:
        path = self.write_record(include_gate=True)
        valid = {
            "outcome": "pass",
            "feature_id": FEATURE_ID,
            "reason_code": "VERIFICATION_DOCUMENT_CURRENT",
            "reason": "Current source is synchronized.",
            "last_synchronized_digest": gate_payload()["current_source_digest"],
            "last_synchronized_checklist_revision": 1,
            "recovery_instruction": None,
            "correlation_id": "corr-replay-gate",
        }
        cases = {
            "feature": {**valid, "feature_id": "feat_01J00000000000000000000002"},
            "digest": {
                **valid,
                "last_synchronized_digest": "sha256:" + ("d" * 64),
            },
            "revision": {**valid, "last_synchronized_checklist_revision": 2},
        }

        for label, response in cases.items():
            with self.subTest(label=label), replay_server(gate_responses=[response]) as (_, base_url):
                result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))

            self.assertEqual("failed", result["status"])
            self.assertEqual("PROTOCOL_ERROR", result["error_code"])
            self.assertTrue(path.exists())
            self.assertFalse(self.processed.exists())

    def test_operation_specific_write_acknowledgements_require_bound_identities(self) -> None:
        source_digest = "sha256:" + ("1" * 64)
        payloads = {
            "reconcile_checklist": {"source_digest": source_digest},
            "address_feedback": {
                "source_digest": source_digest,
                "resolutions": [{"thread_id": "thread_01J00000000000000000000001"}],
            },
            "record_verification_exception": {"source_digest": source_digest},
        }
        common = {
            "feature_id": FEATURE_ID,
            "source_digest": source_digest,
            "idempotency_replayed": False,
            "correlation_id": "corr-write-ack",
        }
        cases = {
            "reconcile_checklist": {
                **common,
                "source_revision_id": "src_01J00000000000000000000001",
                "new_checklist_revision": 1,
            },
            "address_feedback": {
                **common,
                "resolution_ids": ["resolution_01J00000000000000000000001"],
                "thread_states": [{
                    "thread_id": "thread_01J00000000000000000000001",
                    "thread_version": 1,
                    "state": "fix_submitted",
                }],
                "completion_gate": "continue_sync",
            },
            "record_verification_exception": {
                **common,
                "exception_id": "exception_01J00000000000000000000001",
                "feature_status": "no_manual_verification_required",
                "completion_gate": "pass",
            },
        }

        for operation, structured in cases.items():
            envelope = {"operation": operation, "feature_id": FEATURE_ID, "payload": payloads[operation]}
            with self.subTest(operation=operation):
                REPLAY._validate_tool_acknowledgement(operation, structured, envelope)
                with self.assertRaises(REPLAY.DeliveryError):
                    REPLAY._validate_tool_acknowledgement(operation, common, envelope)

        mismatched_feedback = json.loads(json.dumps(cases["address_feedback"]))
        mismatched_feedback["thread_states"][0]["thread_id"] = "thread_01J00000000000000000000002"
        feedback_envelope = {
            "operation": "address_feedback",
            "feature_id": FEATURE_ID,
            "payload": payloads["address_feedback"],
        }
        with self.assertRaises(REPLAY.DeliveryError) as mismatch:
            REPLAY._validate_tool_acknowledgement("address_feedback", mismatched_feedback, feedback_envelope)
        self.assertEqual("THREAD_ID_MISMATCH", mismatch.exception.code)

        two_feedback_resolutions = [
            {"thread_id": "thread_01J00000000000000000000001"},
            {"thread_id": "thread_01J00000000000000000000002"},
        ]
        two_feedback_envelope = {
            "operation": "address_feedback",
            "feature_id": FEATURE_ID,
            "payload": {"source_digest": source_digest, "resolutions": two_feedback_resolutions},
        }
        complete_feedback = {
            **common,
            "resolution_ids": [
                "resolution_01J00000000000000000000001",
                "resolution_01J00000000000000000000002",
            ],
            "thread_states": [
                {"thread_id": resolution["thread_id"], "thread_version": 1, "state": "fix_submitted"}
                for resolution in two_feedback_resolutions
            ],
            "completion_gate": "continue_sync",
        }
        REPLAY._validate_tool_acknowledgement("address_feedback", complete_feedback, two_feedback_envelope)
        invalid_resolution_ids = (
            complete_feedback["resolution_ids"][:1],
            [*complete_feedback["resolution_ids"], "resolution_01J00000000000000000000003"],
            [complete_feedback["resolution_ids"][0], complete_feedback["resolution_ids"][0]],
        )
        for resolution_ids in invalid_resolution_ids:
            with self.subTest(resolution_ids=resolution_ids), self.assertRaises(REPLAY.DeliveryError) as cardinality:
                REPLAY._validate_tool_acknowledgement(
                    "address_feedback",
                    {**complete_feedback, "resolution_ids": resolution_ids},
                    two_feedback_envelope,
                )
            self.assertEqual("PROTOCOL_ERROR", cardinality.exception.code)

    def test_transient_failure_retries_boundedly_and_preserves_sanitized_pending_record(self) -> None:
        token = SECOND_TOKEN
        error = {
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": f"Temporary backend error included {token}",
                "retryable": True,
            }
        }
        path = self.write_record()

        with replay_server([(503, error), (503, error)]) as (server, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, token, attempts=2))

        self.assertEqual("failed", result["status"])
        self.assertEqual(2, result["attempts_this_run"])
        self.assertEqual(2, server.state["tool_calls"])
        self.assertTrue(path.exists())
        pending_text = path.read_text(encoding="utf-8")
        pending = json.loads(pending_text)
        self.assertEqual("pending", pending["status"])
        self.assertEqual(2, pending["attempt_count"])
        self.assertEqual("SERVICE_UNAVAILABLE", pending["last_error_code"])
        self.assertNotIn(token, pending_text)
        self.assertNotIn(token, json.dumps(result))

    def test_remote_error_codes_are_normalized_before_result_and_outbox_persistence(self) -> None:
        malicious_code = "AKIAABCDEFGHIJKLMNOP"

        def assert_safe_failure(path: Path, result: dict, expected_code: str) -> None:
            persisted = path.read_text(encoding="utf-8")
            serialized = json.dumps(result)
            self.assertEqual("failed", result["status"])
            self.assertEqual(expected_code, result["error_code"])
            self.assertEqual(expected_code, json.loads(persisted)["last_error_code"])
            self.assertNotIn(malicious_code, persisted)
            self.assertNotIn(malicious_code, serialized)

        rest_error = {
            "error": {
                "code": malicious_code,
                "message": "REST rejected the request.",
                "retryable": False,
            }
        }
        path = self.write_record()
        with replay_server([(400, rest_error)]) as (_, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))
        assert_safe_failure(path, result, "HTTP_400")
        path.unlink()

        mcp_error = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": {
                            "code": malicious_code,
                            "message": "MCP rejected the request.",
                            "retryable": False,
                        }
                    }),
                }],
                "isError": True,
            },
        }
        path = self.write_record()
        with replay_server([(200, mcp_error)]) as (_, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))
        assert_safe_failure(path, result, "MCP_TOOL_ERROR")
        path.unlink()

        gate_error = {
            "error": {
                "code": malicious_code,
                "message": "Completion gate rejected the request.",
                "retryable": False,
            }
        }
        path = self.write_record(include_gate=True)
        with replay_server(gate_responses=[gate_error]) as (_, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))
        assert_safe_failure(path, result, "COMPLETION_GATE_ERROR")
        self.assertFalse(self.processed.exists())

    def test_non_retryable_tool_error_is_preserved_after_one_attempt(self) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "error": {
                                    "code": "REVISION_CONFLICT",
                                    "message": "Refetch current context.",
                                    "retryable": False,
                                }
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        }
        path = self.write_record()

        with replay_server([(200, response)]) as (server, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=3))

        self.assertEqual("failed", result["status"])
        self.assertEqual("REVISION_CONFLICT", result["error_code"])
        self.assertEqual(1, result["attempts_this_run"])
        self.assertEqual(1, server.state["tool_calls"])
        self.assertTrue(path.exists())

    def test_processed_archive_collision_never_overwrites_or_drops_the_pending_record(self) -> None:
        path = self.write_record()
        self.processed.mkdir(parents=True)
        collision = self.processed / path.name
        collision.write_text('{"sentinel":true}\n', encoding="utf-8")

        with replay_server() as (_, base_url):
            result = REPLAY.replay_record(path, self.settings(base_url, TOKEN, attempts=1))

        self.assertEqual("failed", result["status"])
        self.assertEqual("ARCHIVE_CONFLICT", result["error_code"])
        self.assertTrue(path.exists())
        self.assertEqual({"sentinel": True}, json.loads(collision.read_text(encoding="utf-8")))

    def test_tampered_payload_is_rejected_before_network_io(self) -> None:
        path = self.write_record()
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["scope_summary"] = "Tampered after persistence"
        path.write_text(json.dumps(envelope), encoding="utf-8")

        with self.assertRaisesRegex(REPLAY.EnvelopeError, "canonical payload hash"):
            REPLAY.validate_envelope(path, envelope)

    def test_legacy_v1_envelope_remains_readable_and_validatable(self) -> None:
        fixture = PACKAGE_ROOT / "tests" / "fixtures" / "offline-replay-envelope-v1.json"
        envelope = json.loads(fixture.read_text(encoding="utf-8"))
        path = self.outbox / f"{envelope['operation']}-{envelope['idempotency_key']}.json"
        path.parent.mkdir(parents=True)
        shutil.copyfile(fixture, path)

        validated = REPLAY.validate_envelope(path, json.loads(path.read_text(encoding="utf-8")))

        self.assertEqual("1.0", validated["schema_version"])
        self.assertEqual("pending", validated["status"])

    def test_replay_redirect_is_not_followed_with_authorization(self) -> None:
        token = TOKEN

        with redirect_servers() as (source_url, source, destination):
            with self.assertRaises(REPLAY.DeliveryError) as raised:
                REPLAY._post_json(source_url, {"probe": True}, token, 2)

        self.assertEqual("HTTP_302", raised.exception.code)
        self.assertEqual([f"Bearer {token}"], source.state["authorizations"])
        self.assertEqual([], destination.state["authorizations"])

    def test_foreign_secret_is_rejected_before_network_construction(self) -> None:
        with mock.patch.object(REPLAY.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(REPLAY.ReplayConfigurationError, "missing or malformed"):
                REPLAY._post_json("https://acceptora.example/mcp", {"probe": True}, "AWS_SECRET", 2)
        opener.assert_not_called()

    def test_cli_rejects_records_outside_the_pinned_outbox_before_network(self) -> None:
        path = self.write_record()
        outside = self.root / "outside.json"
        shutil.copyfile(path, outside)
        config = self.root / "runtime-config.json"
        config.write_text(json.dumps({
            "target_root": str(self.root),
            "mcp_url": "https://acceptora.example/mcp",
            "completion_gate_url": "https://acceptora.example/api/integrations/completion-gate",
            "project_id": PROJECT_ID,
            "token_env": TOKEN_ENV,
        }), encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()
        with mock.patch.dict(os.environ, {TOKEN_ENV: TOKEN}), mock.patch.object(
            REPLAY, "_post_json"
        ) as post:
            result = REPLAY.main([
                str(outside), "--config", str(config), "--accept-config-sha256", digest,
            ])
        self.assertEqual(2, result)
        post.assert_not_called()

    def test_symlinked_record_is_rejected_without_network_or_external_deletion(self) -> None:
        path = self.write_record()
        outside = self.root / "external" / path.name
        outside.parent.mkdir()
        path.replace(outside)
        try:
            path.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"File symlinks are unavailable: {error}")
        with replay_server() as (_, base_url), mock.patch.object(REPLAY, "_post_json") as post:
            with self.assertRaisesRegex(REPLAY.ReplayConfigurationError, "symlink or junction"):
                REPLAY.replay_record(path, self.settings(base_url, TOKEN))
        post.assert_not_called()
        self.assertTrue(outside.is_file())

    def test_python_311_windows_reparse_points_are_linklike(self) -> None:
        class Python311Path:
            def __init__(self, attributes: int) -> None:
                self.attributes = attributes

            def is_symlink(self) -> bool:
                return False

            def lstat(self) -> object:
                return type("FileStatus", (), {"st_file_attributes": self.attributes})()

        with mock.patch.object(REPLAY.os, "name", "nt"):
            self.assertTrue(REPLAY._is_linklike(Python311Path(0x400)))
            self.assertFalse(REPLAY._is_linklike(Python311Path(0)))

    def test_oversized_record_and_config_are_rejected_before_network(self) -> None:
        record = self.outbox / f"reconcile_checklist-{IDEMPOTENCY_KEY}.json"
        record.parent.mkdir(parents=True)
        record.write_bytes(b"x" * (REPLAY.MAX_ENVELOPE_BYTES + 1))
        with replay_server() as (_, base_url), mock.patch.object(REPLAY, "_post_json") as post:
            with self.assertRaisesRegex(REPLAY.EnvelopeError, "size limit"):
                REPLAY.replay_record(record, self.settings(base_url, TOKEN))
        post.assert_not_called()

        config = self.root / "oversized-config.json"
        config.write_bytes(b"x" * (REPLAY.MAX_CONFIG_BYTES + 1))
        with mock.patch.object(REPLAY, "_post_json") as post:
            with self.assertRaisesRegex(REPLAY.ReplayConfigurationError, "1 MiB"):
                REPLAY._stable_read(config)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
