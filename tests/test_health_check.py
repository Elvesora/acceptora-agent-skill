from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = SKILL_ROOT / "tests" / "fixtures" / "contracts" / "v1"
HEALTH = SKILL_ROOT / "scripts" / "health_check.py"
MANIFEST_PATH = SKILL_ROOT / "config" / "package-manifest.json"
HTTP_API_PATH = CONTRACT_ROOT / "http-api.json"
TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)
WRONG_TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAA_" + ("B" * 48)
REST_PATHS = {
    "resolve_feature": "/api/v1/integrations/features/resolve",
    "get_feature_context": "/api/v1/integrations/features/context",
    "reconcile_checklist": "/api/v1/integrations/checklists/reconcile",
    "get_verification_feedback": "/api/v1/integrations/feedback/query",
    "address_feedback": "/api/v1/integrations/feedback/address",
    "get_verification_status": "/api/v1/integrations/status",
    "check_completion_gate": "/api/v1/integrations/completion-gate",
    "record_verification_exception": "/api/v1/integrations/verification-exceptions",
}
CONNECTION_CONFIRMATION_PATH = "/api/v1/integrations/connection/confirm"
REQUIRED_WORKFLOW_SCOPES = [
    "projects:read",
    "features:resolve",
    "features:read",
    "checklists:write",
    "feedback:read",
    "feedback:address",
    "gates:read",
]
CONNECTION_CONFIRMATION_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
CONNECTION_CONFIRMATION_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "project_id",
        "connection_status",
        "confirmed_at",
        "already_connected",
        "correlation_id",
    ],
    "properties": {
        "project_id": {"type": "string", "pattern": "^proj_[0-9A-HJKMNP-TV-Z]{26}$"},
        "connection_status": {"type": "string", "const": "connected"},
        "confirmed_at": {"type": "string", "format": "date-time"},
        "already_connected": {"type": "boolean"},
        "correlation_id": {"type": "string", "minLength": 1, "maxLength": 255},
    },
}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ContractResolver:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.documents: dict[Path, Any] = {}

    def document(self, path: Path) -> Any:
        path = path.resolve()
        if path != self.root and self.root not in path.parents:
            raise AssertionError("Schema reference left contracts/v1.")
        if path not in self.documents:
            self.documents[path] = json.loads(path.read_text(encoding="utf-8"))
        return self.documents[path]

    def resolve(self, value: Any, current: Path) -> Any:
        if isinstance(value, list):
            return [self.resolve(item, current) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            relative, _, pointer = value["$ref"].partition("#")
            target_path = (current.parent / relative).resolve() if relative else current
            target = self.document(target_path)
            if pointer:
                for segment in pointer.lstrip("/").split("/"):
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
            resolved = self.resolve(target, target_path)
            siblings = self.resolve({key: child for key, child in value.items() if key != "$ref"}, current)
            return {**resolved, **siblings} if isinstance(resolved, dict) else resolved
        return {key: self.resolve(child, current) for key, child in value.items()}

    def schema(self, relative: str) -> dict[str, Any]:
        path = (self.root / relative).resolve()
        resolved = self.resolve(self.document(path), path)
        resolved.pop("$schema", None)
        resolved.pop("$id", None)
        return resolved


def authoritative_tools() -> list[dict[str, Any]]:
    registry = json.loads((CONTRACT_ROOT / "mcp-tools.json").read_text(encoding="utf-8"))
    resolver = ContractResolver(CONTRACT_ROOT)
    result = []
    for entry in registry["tools"]:
        annotations = {"readOnlyHint": entry["read_only"], "openWorldHint": False}
        if not entry["read_only"]:
            annotations.update({"idempotentHint": True, "destructiveHint": False})
        result.append({
            "name": entry["name"],
            "description": f"Contract tool {entry['name']}",
            "inputSchema": resolver.schema(entry["input_schema"]),
            "outputSchema": resolver.schema(entry["output_schema"]),
            "annotations": annotations,
            "required_scope": entry["required_scope"],
        })
    return result


def schema_name(name: str, suffix: str) -> str:
    return "".join(part.title() for part in name.split("_")) + suffix


def openapi_document(state: "HealthState") -> dict[str, Any]:
    paths: dict[str, Any] = {}
    schemas: dict[str, Any] = {
        "ProjectMetadata": {"type": "object"},
        "ConfirmConnectionRequest": CONNECTION_CONFIRMATION_REQUEST_SCHEMA,
        "ConnectionConfirmation": CONNECTION_CONFIRMATION_RESPONSE_SCHEMA,
    }
    for tool in state.openapi_tools:
        request_name = schema_name(tool["name"], "Request")
        response_name = schema_name(tool["name"], "Response")
        schemas[request_name] = tool["inputSchema"]
        schemas[response_name] = tool["outputSchema"]
        paths[REST_PATHS[tool["name"]]] = {"post": {
            "operationId": tool["name"],
            "security": [{"agentBearer": []}],
            "x-acceptora-required-scope": tool["required_scope"],
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{request_name}"},
            }}},
            "responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{response_name}"},
            }}}},
        }}
    paths["/api/v1/integrations/project"] = {"get": {
        "operationId": "get_project_metadata",
        "security": [{"agentBearer": []}],
        "x-acceptora-required-scope": "projects:read",
        "responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/ProjectMetadata"},
        }}}},
    }}
    paths[CONNECTION_CONFIRMATION_PATH] = {"post": {
        "operationId": "confirm_connection",
        "security": [{"agentBearer": []}],
        "x-acceptora-required-scopes": state.confirmation_required_scopes,
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/ConfirmConnectionRequest"},
        }}},
        "responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/ConnectionConfirmation"},
        }}}},
    }}
    paths["/api/integrations/completion-gate"] = {"post": {
        "operationId": "legacy_check_completion_gate",
        "deprecated": True,
        "security": [{"agentBearer": []}],
        "x-acceptora-required-scope": "gates:read",
    }}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Acceptora Agent Verification API",
            "version": state.versions["contract_version"],
            "x-acceptora-integration-version": state.versions["integration_version"],
            "x-acceptora-skill-version": state.versions["skill_version"],
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {"schemas": schemas},
    }


class HealthState:
    def __init__(self) -> None:
        self.versions = {
            "contract_version": "1.0.0",
            "integration_version": "1.0.0",
            "skill_version": "1.1.0",
            "schema_registry": "contracts/v1/mcp-tools.json",
        }
        self.protocol_version = "2025-11-25"
        self.server_name = "Acceptora Verification"
        self.server_version = "1.0.0"
        self.tools = authoritative_tools()
        self.tool_pages: list[list[dict[str, Any]]] | None = None
        self.openapi_tools = authoritative_tools()
        self.base_url = ""
        self.project = {
            "project_id": "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "granted_scopes": [
                "projects:read", "features:resolve", "features:read", "checklists:write",
                "feedback:read", "feedback:address", "gates:read", "exceptions:write",
            ],
            "versions": {key: self.versions[key] for key in ("contract_version", "integration_version", "skill_version")},
        }
        self.confirmation_response = {
            "project_id": self.project["project_id"],
            "connection_status": "connected",
            "confirmed_at": "2026-08-21T01:02:03+00:00",
            "already_connected": False,
            "correlation_id": "health-check-correlation",
        }
        self.confirmation_required_scopes = list(REQUIRED_WORKFLOW_SCOPES)
        self.requests: list[dict[str, Any]] = []


class HealthHandler(BaseHTTPRequestHandler):
    server: "HealthServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path not in {
            "/api/contract-version",
            "/api/v1/integrations/openapi.json",
            "/api/v1/integrations/project",
        }:
            self._json(404, {"error": "not found"})
            return
        authorization = self.headers.get("Authorization")
        self.server.state.requests.append({"method": "GET", "path": self.path, "authorization": authorization})
        if self.path == "/api/contract-version":
            self._json(200, {"data": self.server.state.versions, "meta": {"contract_version": "1.0.0"}})
            return
        if self.path == "/api/v1/integrations/openapi.json":
            self._json(200, openapi_document(self.server.state))
            return
        if authorization != f"Bearer {TOKEN}":
            self._json(401, {"error": "unauthorized"})
            return
        project = {**self.server.state.project, "endpoints": {
            "mcp": f"{self.server.state.base_url}/mcp",
            "rest": f"{self.server.state.base_url}/api/v1/integrations",
            "openapi": f"{self.server.state.base_url}/api/v1/integrations/openapi.json",
            "completion_gate": f"{self.server.state.base_url}/api/v1/integrations/completion-gate",
        }}
        self._json(200, project)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "invalid"})
            return
        authorization = self.headers.get("Authorization")
        self.server.state.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": authorization,
                "rpc_method": body.get("method") if isinstance(body, dict) else None,
                "body": body,
                "raw_body": raw_body,
            }
        )
        if self.path == CONNECTION_CONFIRMATION_PATH:
            if authorization != f"Bearer {TOKEN}":
                self._json(401, {"error": {"message": f"Rejected {TOKEN}"}})
                return
            if body != {}:
                self._json(422, {"error": "invalid"})
                return
            self._json(200, self.server.state.confirmation_response)
            return
        if self.path != "/mcp":
            self._json(404, {"error": "not found"})
            return
        if authorization != f"Bearer {TOKEN}":
            self._json(401, {"error": {"message": f"Rejected {TOKEN}"}})
            return

        method = body.get("method")
        if method == "initialize":
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "protocolVersion": self.server.state.protocol_version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": self.server.state.server_name,
                            "version": self.server.state.server_version,
                        },
                    },
                },
                {"MCP-Session-Id": "health-session"},
            )
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            pages = self.server.state.tool_pages
            if pages is not None:
                cursor = body.get("params", {}).get("cursor")
                index = 0 if cursor is None else int(cursor)
                result: dict[str, Any] = {"tools": pages[index]}
                if index + 1 < len(pages):
                    result["nextCursor"] = str(index + 1)
                self._json(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result})
                return
            self._json(
                200,
                {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": self.server.state.tools}},
            )
            return
        self._json(400, {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601}})

    def _json(self, status: int, value: Any, extra_headers: dict[str, str] | None = None) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)


class HealthServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: HealthState) -> None:
        self.state = state
        super().__init__(address, HealthHandler)


@contextmanager
def health_server(state: HealthState) -> Iterator[str]:
    server = HealthServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state.base_url = f"http://127.0.0.1:{server.server_port}"
        yield state.base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_health(
    state: HealthState,
    token: str = TOKEN,
    config_padding: int = 0,
    output_format: str = "json",
    confirm_connection: bool = False,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temporary, health_server(state) as base_url:
        config = Path(temporary) / "config.json"
        config_value = {
                    "version": 1,
                    "mcp_url": f"{base_url}/mcp",
                    "contract_version_url": f"{base_url}/api/contract-version",
                    "rest_base_url": f"{base_url}/api/v1/integrations",
                    "openapi_url": f"{base_url}/api/v1/integrations/openapi.json",
                    "completion_gate_url": f"{base_url}/api/v1/integrations/completion-gate",
                    "project_id": "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "token_env": "ACCEPTORA_AGENT_TOKEN",
                    "timeout_seconds": 3,
                }
        if config_padding:
            config_value["padding"] = "x" * config_padding
        config.write_text(json.dumps(config_value), encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()
        command = [
            sys.executable,
            "-I",
            str(HEALTH),
            "--config",
            str(config),
            "--accept-config-sha256",
            digest,
            "--format",
            output_format,
        ]
        if confirm_connection:
            command.append("--confirm-connection")
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**os.environ, "ACCEPTORA_AGENT_TOKEN": token},
            check=False,
        )


class HealthCheckTest(unittest.TestCase):
    def test_http_registry_declares_confirmation_with_all_normal_workflow_scopes(self) -> None:
        registry = json.loads(HTTP_API_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            {
                "name": "confirm_connection",
                "method": "POST",
                "path": CONNECTION_CONFIRMATION_PATH,
                "authentication": "bearer",
                "required_scopes": REQUIRED_WORKFLOW_SCOPES,
            },
            registry["connection_confirmation"],
        )
        self.assertEqual(8, len(registry["operations"]))

    def test_package_manifest_matches_authoritative_contract_registry_and_schemas(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        expected = manifest["tools"]
        actual = authoritative_tools()
        self.assertEqual([tool["name"] for tool in actual], [tool["name"] for tool in expected])
        self.assertEqual(8, len(expected))
        for contract, package in zip(actual, expected, strict=True):
            self.assertEqual(canonical_digest(contract["inputSchema"]), package["input_schema_sha256"])
            self.assertEqual(canonical_digest(contract["outputSchema"]), package["output_schema_sha256"])
            self.assertEqual(contract["required_scope"], package["required_scope"])
            self.assertEqual(contract["annotations"], package["annotations"])

    def test_health_passes_only_for_exact_versions_tools_and_schemas_without_exposing_secret(self) -> None:
        state = HealthState()
        result = run_health(state)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("read_only_contract_probe", payload["operation_mode"])
        self.assertFalse(payload["product_write_tools_called"])
        self.assertFalse(payload["setup_state_write_performed"])
        self.assertEqual(
            {"requested": False, "status": "not_requested"},
            payload["connection_confirmation"],
        )
        self.assertTrue(payload["authentication_telemetry_may_update"])
        self.assertFalse(payload["credential"]["value_exposed"])
        self.assertEqual(8, len(payload["mcp"]["tools"]))
        self.assertEqual(
            ["GET", "GET", "GET", "initialize", "notifications/initialized", "tools/list"],
            [request.get("rpc_method", "GET") for request in state.requests],
        )
        self.assertIsNone(state.requests[0]["authorization"])
        self.assertIsNone(state.requests[1]["authorization"])
        self.assertTrue(all(request["authorization"] == f"Bearer {TOKEN}" for request in state.requests[2:]))

    def test_explicit_confirmation_is_the_final_request_after_every_health_check(self) -> None:
        state = HealthState()

        result = run_health(state, confirm_connection=True)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("setup_connection_confirmation", payload["operation_mode"])
        self.assertFalse(payload["product_write_tools_called"])
        self.assertTrue(payload["setup_state_write_performed"])
        self.assertEqual(
            {
                "requested": True,
                "status": "confirmed",
                **state.confirmation_response,
            },
            payload["connection_confirmation"],
        )
        self.assertEqual("connection_confirmation", payload["checks"][-1]["name"])
        self.assertEqual(CONNECTION_CONFIRMATION_PATH, state.requests[-1]["path"])
        self.assertEqual("POST", state.requests[-1]["method"])
        self.assertEqual({}, state.requests[-1]["body"])
        self.assertEqual(b"{}", state.requests[-1]["raw_body"])
        self.assertEqual(f"Bearer {TOKEN}", state.requests[-1]["authorization"])
        self.assertEqual(
            ["GET", "GET", "GET", "initialize", "notifications/initialized", "tools/list", "POST"],
            [request.get("rpc_method") or request["method"] for request in state.requests],
        )

    def test_text_output_surfaces_missing_optional_scope_warning(self) -> None:
        state = HealthState()
        state.project["granted_scopes"].remove("exceptions:write")

        result = run_health(state, output_format="text")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("warning: Optional exceptions:write scope is not granted.", result.stdout)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_health_rejects_version_tool_and_schema_drift(self) -> None:
        cases: list[tuple[str, HealthState, str]] = []

        version_state = HealthState()
        version_state.versions["contract_version"] = "1.1.0"
        cases.append(("version", version_state, "VERSION_DRIFT"))

        tool_state = HealthState()
        tool_state.tools = tool_state.tools[:-1]
        cases.append(("tool", tool_state, "MCP_TOOL_DRIFT"))

        schema_state = HealthState()
        schema_state.tools[0] = {**schema_state.tools[0], "inputSchema": {"type": "object"}}
        cases.append(("schema", schema_state, "MCP_SCHEMA_DRIFT"))

        annotation_state = HealthState()
        annotation_state.tools[0] = {**annotation_state.tools[0], "annotations": {"readOnlyHint": True}}
        cases.append(("annotations", annotation_state, "MCP_ANNOTATION_DRIFT"))

        rest_schema_state = HealthState()
        rest_schema_state.openapi_tools[0] = {**rest_schema_state.openapi_tools[0], "inputSchema": {"type": "object"}}
        cases.append(("rest-schema", rest_schema_state, "REST_SCHEMA_DRIFT"))

        rest_scope_state = HealthState()
        rest_scope_state.openapi_tools[0] = {**rest_scope_state.openapi_tools[0], "required_scope": "foreign:scope"}
        cases.append(("rest-scope", rest_scope_state, "REST_OPERATION_DRIFT"))

        confirmation_scope_state = HealthState()
        confirmation_scope_state.confirmation_required_scopes = REQUIRED_WORKFLOW_SCOPES[:-1]
        cases.append(("confirmation-scopes", confirmation_scope_state, "REST_OPERATION_DRIFT"))

        project_state = HealthState()
        project_state.project["project_id"] = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAA"
        cases.append(("project", project_state, "PROJECT_ID_MISMATCH"))

        scope_state = HealthState()
        scope_state.project["granted_scopes"] = ["projects:read"]
        cases.append(("scope", scope_state, "PROJECT_SCOPE_MISSING"))

        for label, state, expected_code in cases:
            with self.subTest(label=label):
                result = run_health(state, confirm_connection=True)
                self.assertEqual(1, result.returncode)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(expected_code, payload["error"]["code"])
                self.assertFalse(
                    any(request["path"] == CONNECTION_CONFIRMATION_PATH for request in state.requests)
                )

    def test_confirmation_response_drift_fails_without_claiming_the_write_was_confirmed(self) -> None:
        state = HealthState()
        state.confirmation_response["unexpected"] = True

        result = run_health(state, confirm_connection=True)

        self.assertEqual(1, result.returncode)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("CONNECTION_CONFIRMATION_DRIFT", payload["error"]["code"])
        self.assertIsNone(payload["setup_state_write_performed"])
        self.assertEqual(
            {"requested": True, "status": "unknown"},
            payload["connection_confirmation"],
        )
        self.assertEqual(CONNECTION_CONFIRMATION_PATH, state.requests[-1]["path"])

    def test_auth_failure_never_echoes_server_body_or_credential(self) -> None:
        state = HealthState()
        result = run_health(state, token=WRONG_TOKEN)
        self.assertEqual(1, result.returncode)
        self.assertNotIn(WRONG_TOKEN, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("HTTP_ERROR", payload["error"]["code"])
        self.assertNotIn("Rejected", payload["error"]["message"])

    def test_foreign_secret_is_rejected_before_any_network_request(self) -> None:
        state = HealthState()
        result = run_health(state, token="aws-secret-that-is-not-an-acceptora-token")
        self.assertEqual(1, result.returncode)
        self.assertEqual([], state.requests)
        payload = json.loads(result.stdout)
        self.assertEqual("AUTH_REQUIRED", payload["error"]["code"])

    def test_oversized_config_is_rejected_before_any_network_request(self) -> None:
        state = HealthState()
        result = run_health(state, config_padding=1_100_000)
        self.assertEqual(1, result.returncode)
        self.assertEqual([], state.requests)
        payload = json.loads(result.stdout)
        self.assertEqual("CONFIG_INVALID", payload["error"]["code"])

    def test_tools_list_rejects_aggregate_count_and_decoded_size_before_contract_comparison(self) -> None:
        count_state = HealthState()
        count_state.tool_pages = [[{"name": f"tool_{index}"} for index in range(65)]]

        count_result = run_health(count_state)

        self.assertEqual(1, count_result.returncode)
        count_payload = json.loads(count_result.stdout)
        self.assertEqual("MCP_PROTOCOL_DRIFT", count_payload["error"]["code"])
        self.assertIn("aggregate tool limit", count_payload["error"]["message"])

        size_state = HealthState()
        size_state.tool_pages = [
            [{"name": "first", "description": "x" * 600_000}],
            [{"name": "second", "description": "y" * 600_000}],
        ]

        size_result = run_health(size_state)

        self.assertEqual(1, size_result.returncode)
        size_payload = json.loads(size_result.stdout)
        self.assertEqual("MCP_PROTOCOL_DRIFT", size_payload["error"]["code"])
        self.assertIn("aggregate decoded-size limit", size_payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
