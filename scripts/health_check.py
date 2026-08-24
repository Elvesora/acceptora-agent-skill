#!/usr/bin/env python3
"""Verify the authenticated contract and optionally confirm setup as the final step."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import ssl
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MANIFEST = PACKAGE_ROOT / "config" / "package-manifest.json"
PINNED_RUNTIME_CONFIG = PACKAGE_ROOT.parent / "config" / "runtime-config.json"
PINNED_TOKEN_ENV = "ACCEPTORA_AGENT_TOKEN"
TOKEN_PATTERN = re.compile(r"^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
MAX_RESPONSE_BYTES = 4_000_000
MAX_CONFIG_BYTES = 1_048_576
MAX_MCP_TOOL_COUNT = 64
MAX_MCP_TOOL_DECODED_BYTES = 1_000_000
REST_OPERATION_PATHS = {
    "resolve_feature": "/api/v1/integrations/features/resolve",
    "get_feature_context": "/api/v1/integrations/features/context",
    "reconcile_checklist": "/api/v1/integrations/checklists/reconcile",
    "get_verification_feedback": "/api/v1/integrations/feedback/query",
    "address_feedback": "/api/v1/integrations/feedback/address",
    "get_verification_status": "/api/v1/integrations/status",
    "check_completion_gate": "/api/v1/integrations/completion-gate",
    "record_verification_exception": "/api/v1/integrations/verification-exceptions",
}
PROJECT_METADATA_PATH = "/api/v1/integrations/project"
CONNECTION_CONFIRMATION_PATH = "/api/v1/integrations/connection/confirm"
LEGACY_COMPLETION_GATE_PATH = "/api/integrations/completion-gate"
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


class HealthFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        setup_state_write_may_have_occurred: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.setup_state_write_may_have_occurred = setup_state_write_may_have_occurred


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _is_http_loopback_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    mcp_url: str
    contract_version_url: str
    rest_base_url: str
    openapi_url: str
    completion_gate_url: str
    project_id: str
    token_env: str
    token: str
    timeout_seconds: float
    tls_ca_file: str | None


class Transport:
    def __init__(self, settings: Settings, skill_version: str | None = None) -> None:
        context = ssl.create_default_context(cafile=settings.tls_ca_file)
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        self.loopback_opener = build_opener(
            _NoRedirect(),
            ProxyHandler({}),
            HTTPSHandler(context=context),
        )
        self.timeout_seconds = settings.timeout_seconds
        selected_skill_version = skill_version or str(load_manifest()["skill"]["version"])
        self.user_agent = f"acceptora-health/{selected_skill_version}"

    def request(
        self,
        url: str,
        *,
        method: str,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        request_id: int | None = None,
        allow_empty: bool = False,
    ) -> tuple[dict[str, Any] | None, Any]:
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"

        request = Request(url, data=body, headers=request_headers, method=method)
        opener = self.loopback_opener if token is not None and _is_http_loopback_url(url) else self.opener
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise HealthFailure("RESPONSE_TOO_LARGE", "A health response exceeded the safe size limit.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise HealthFailure("RESPONSE_TOO_LARGE", "A health response exceeded the safe size limit.")
                if not raw:
                    if allow_empty:
                        return None, response.headers
                    raise HealthFailure("INVALID_RESPONSE", "A health endpoint returned an empty response.")
                return self._decode(raw, response.headers.get("Content-Type", ""), request_id), response.headers
        except HTTPError as exc:
            raise HealthFailure("HTTP_ERROR", f"A health endpoint returned HTTP {exc.code}.") from None
        except URLError:
            raise HealthFailure("CONNECTION_FAILED", "A health endpoint could not be reached securely.") from None
        except TimeoutError:
            raise HealthFailure("CONNECTION_TIMEOUT", "A health endpoint timed out.") from None
        except ssl.SSLError:
            raise HealthFailure("TLS_FAILED", "TLS verification failed for a health endpoint.") from None

    @staticmethod
    def _decode(raw: bytes, content_type: str, request_id: int | None) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HealthFailure("INVALID_RESPONSE", "A health endpoint returned invalid UTF-8.") from None

        candidates: list[Any] = []
        if "text/event-stream" in content_type.lower() or text.lstrip().startswith("event:"):
            data_lines: list[str] = []
            for line in text.splitlines() + [""]:
                if line == "":
                    if data_lines:
                        try:
                            candidates.append(json.loads("\n".join(data_lines)))
                        except json.JSONDecodeError:
                            raise HealthFailure("INVALID_RESPONSE", "The MCP server returned invalid event data.") from None
                        data_lines = []
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        else:
            try:
                candidates.append(json.loads(text))
            except json.JSONDecodeError:
                raise HealthFailure("INVALID_RESPONSE", "A health endpoint returned invalid JSON.") from None

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if request_id is None or candidate.get("id") == request_id:
                return candidate
        raise HealthFailure("INVALID_RESPONSE", "The MCP response did not match the request ID.")


def _validate_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HealthFailure("CONFIG_INVALID", f"{label} must be an absolute URL.")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HealthFailure("CONFIG_INVALID", f"{label} must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HealthFailure("CONFIG_INVALID", f"{label} must not contain credentials, a query, or a fragment.")
    if parsed.scheme == "http" and not _is_http_loopback_url(value.strip()):
        raise HealthFailure("CONFIG_INVALID", f"{label} must use HTTPS unless it targets local loopback.")
    return value.strip()


def _stable_read(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HealthFailure("CONFIG_INVALID", "The verification config is not a regular no-follow file.") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise HealthFailure("CONFIG_INVALID", "The verification config is not a regular file.")
        if before.st_size > MAX_CONFIG_BYTES:
            raise HealthFailure("CONFIG_INVALID", "The verification config exceeds the 1 MiB size limit.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise HealthFailure("CONFIG_INVALID", "The verification config exceeds the 1 MiB size limit.")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise HealthFailure("CONFIG_INVALID", "The verification config changed while it was read.")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_settings(config_path: Path, config_body: bytes | None = None) -> Settings:
    try:
        config = json.loads((config_body if config_body is not None else _stable_read(config_path)).decode("utf-8"))
    except FileNotFoundError:
        raise HealthFailure("CONFIG_MISSING", "The verification configuration file does not exist.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HealthFailure("CONFIG_INVALID", "The verification configuration file is not valid UTF-8 JSON.") from None
    if not isinstance(config, dict) or config.get("version") != 1:
        raise HealthFailure("CONFIG_INVALID", "The verification configuration must use version 1.")

    token_env = config.get("token_env")
    if token_env != PINNED_TOKEN_ENV:
        raise HealthFailure("CONFIG_INVALID", f"token_env must be pinned to {PINNED_TOKEN_ENV}.")
    token = os.environ.get(token_env, "")
    if not TOKEN_PATTERN.fullmatch(token):
        raise HealthFailure("AUTH_REQUIRED", "The configured credential environment variable is missing or malformed.")

    timeout = config.get("timeout_seconds", 8)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= float(timeout) <= 60:
        raise HealthFailure("CONFIG_INVALID", "timeout_seconds must be between 0.1 and 60.")

    tls_ca_file = config.get("tls_ca_file")
    if tls_ca_file is not None:
        raise HealthFailure("CONFIG_INVALID", "Custom TLS CA files are not supported for credential-bearing health checks.")

    mcp_url = _validate_url(config.get("mcp_url"), "mcp_url")
    contract_version_url = _validate_url(config.get("contract_version_url"), "contract_version_url")
    rest_base_url = _validate_url(config.get("rest_base_url"), "rest_base_url").rstrip("/")
    openapi_url = _validate_url(config.get("openapi_url"), "openapi_url")
    completion_gate_url = _validate_url(config.get("completion_gate_url"), "completion_gate_url")
    origins = {(urlsplit(value).scheme, urlsplit(value).netloc) for value in (mcp_url, contract_version_url, rest_base_url, openapi_url, completion_gate_url)}
    if len(origins) != 1:
        raise HealthFailure("CONFIG_INVALID", "All Acceptora endpoints must use the exact same validated origin.")
    rest_path = urlsplit(rest_base_url).path.rstrip("/")
    rest_suffix = "/api/v1/integrations"
    if not rest_path.endswith(rest_suffix):
        raise HealthFailure("CONFIG_INVALID", "rest_base_url must use the canonical v1 integrations path.")
    base_path = rest_path.removesuffix(rest_suffix)
    expected_paths = {
        "mcp_url": f"{base_path}/mcp",
        "contract_version_url": f"{base_path}/api/contract-version",
        "openapi_url": f"{rest_path}/openapi.json",
        "completion_gate_url": f"{rest_path}/completion-gate",
    }
    observed_paths = {
        "mcp_url": urlsplit(mcp_url).path,
        "contract_version_url": urlsplit(contract_version_url).path,
        "openapi_url": urlsplit(openapi_url).path,
        "completion_gate_url": urlsplit(completion_gate_url).path,
    }
    if observed_paths != expected_paths:
        raise HealthFailure("CONFIG_INVALID", "Acceptora endpoint paths do not match the canonical pinned API layout.")
    project_id = config.get("project_id")
    if not isinstance(project_id, str) or re.fullmatch(r"proj_[0-9A-HJKMNP-TV-Z]{26}", project_id) is None:
        raise HealthFailure("CONFIG_INVALID", "project_id must use the public proj_<ULID> form.")

    return Settings(
        mcp_url=mcp_url,
        contract_version_url=contract_version_url,
        rest_base_url=rest_base_url,
        openapi_url=openapi_url,
        completion_gate_url=completion_gate_url,
        project_id=project_id,
        token_env=token_env,
        token=token,
        timeout_seconds=float(timeout),
        tls_ca_file=tls_ca_file,
    )


def load_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        raise HealthFailure("PACKAGE_INVALID", "The package compatibility manifest is unavailable or invalid.") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise HealthFailure("PACKAGE_INVALID", "The package compatibility manifest must use schema version 1.")
    tools = manifest.get("tools")
    if not isinstance(tools, list) or len(tools) != 8:
        raise HealthFailure("PACKAGE_INVALID", "The package compatibility manifest must declare exactly eight tools.")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != 8 or len(set(names)) != 8 or not all(isinstance(name, str) and TOOL_NAME_PATTERN.fullmatch(name) for name in names):
        raise HealthFailure("PACKAGE_INVALID", "The package compatibility manifest has invalid tool names.")
    for tool in tools:
        required_scope = tool.get("required_scope")
        annotations = tool.get("annotations")
        if not isinstance(required_scope, str) or not required_scope:
            raise HealthFailure("PACKAGE_INVALID", "A package tool is missing required_scope metadata.")
        if not isinstance(annotations, dict) or not annotations or any(
            key not in {"readOnlyHint", "idempotentHint", "destructiveHint", "openWorldHint"}
            or not isinstance(value, bool)
            for key, value in annotations.items()
        ):
            raise HealthFailure("PACKAGE_INVALID", "A package tool has invalid annotations metadata.")
        for field in ("input_schema_sha256", "output_schema_sha256"):
            value = tool.get(field)
            if not isinstance(value, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
                raise HealthFailure("PACKAGE_INVALID", "The package compatibility manifest has an invalid schema digest.")
    return manifest


def _rpc_result(response: dict[str, Any], request_id: int) -> dict[str, Any]:
    if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
        raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP server returned an invalid JSON-RPC envelope.")
    if "error" in response:
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        safe_code = code if isinstance(code, int) else "unknown"
        raise HealthFailure("MCP_RPC_ERROR", f"The MCP server returned JSON-RPC error code {safe_code}.")
    result = response.get("result")
    if not isinstance(result, dict):
        raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP server did not return a result object.")
    return result


def _schema_digest(value: Any) -> str:
    if not isinstance(value, dict):
        raise HealthFailure("MCP_SCHEMA_DRIFT", "An MCP tool omitted a required object schema.")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _openapi_component_schema(document: dict[str, Any], reference: Any) -> dict[str, Any]:
    prefix = "#/components/schemas/"
    if not isinstance(reference, dict) or set(reference) != {"$ref"}:
        raise HealthFailure("REST_SCHEMA_DRIFT", "An OpenAPI operation does not use one reviewed component schema reference.")
    value = reference.get("$ref")
    if not isinstance(value, str) or not value.startswith(prefix) or "/" in value[len(prefix):]:
        raise HealthFailure("REST_SCHEMA_DRIFT", "An OpenAPI operation uses an unsupported schema reference.")
    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    schema = schemas.get(value[len(prefix):]) if isinstance(schemas, dict) else None
    if not isinstance(schema, dict):
        raise HealthFailure("REST_SCHEMA_DRIFT", "An OpenAPI component schema is missing.")
    return schema


def _openapi_operation_schema(operation: dict[str, Any], document: dict[str, Any], direction: str) -> dict[str, Any]:
    if direction == "input":
        request_body = operation.get("requestBody")
        content = request_body.get("content") if isinstance(request_body, dict) and request_body.get("required") is True else None
    else:
        responses = operation.get("responses")
        success = responses.get("200") if isinstance(responses, dict) else None
        content = success.get("content") if isinstance(success, dict) else None
    json_media = content.get("application/json") if isinstance(content, dict) else None
    schema = json_media.get("schema") if isinstance(json_media, dict) else None
    return _openapi_component_schema(document, schema)


def _validate_openapi(
    document: dict[str, Any] | None,
    manifest: dict[str, Any],
    expected_versions: dict[str, str],
) -> None:
    if not isinstance(document, dict) or document.get("openapi") != "3.1.0":
        raise HealthFailure("REST_CONTRACT_DRIFT", "The pinned OpenAPI endpoint does not expose OpenAPI 3.1.0.")
    info = document.get("info")
    if (
        not isinstance(info, dict)
        or info.get("version") != expected_versions["contract_version"]
        or info.get("x-acceptora-integration-version") != expected_versions["integration_version"]
        or info.get("x-acceptora-skill-version") != expected_versions["skill_version"]
    ):
        raise HealthFailure("REST_VERSION_DRIFT", "The REST contract versions do not match this package.")
    if document.get("servers") != [{"url": "/"}]:
        raise HealthFailure("REST_ORIGIN_DRIFT", "The OpenAPI document must use the reviewed relative server root.")

    paths = document.get("paths")
    expected_paths = {
        *REST_OPERATION_PATHS.values(),
        PROJECT_METADATA_PATH,
        CONNECTION_CONFIRMATION_PATH,
        LEGACY_COMPLETION_GATE_PATH,
    }
    if not isinstance(paths, dict) or set(paths) != expected_paths:
        raise HealthFailure("REST_OPERATION_DRIFT", "The OpenAPI document does not expose the exact reviewed REST path set.")
    expected_tools = {tool["name"]: tool for tool in manifest["tools"]}
    if set(expected_tools) != set(REST_OPERATION_PATHS):
        raise HealthFailure("PACKAGE_INVALID", "The package REST and MCP operation sets differ.")
    for name, path in REST_OPERATION_PATHS.items():
        path_item = paths.get(path)
        if not isinstance(path_item, dict) or set(path_item) != {"post"}:
            raise HealthFailure("REST_OPERATION_DRIFT", "A reviewed REST operation has the wrong HTTP method.")
        operation = path_item["post"]
        expected = expected_tools[name]
        if (
            not isinstance(operation, dict)
            or operation.get("operationId") != name
            or operation.get("x-acceptora-required-scope") != expected["required_scope"]
            or operation.get("security") != [{"agentBearer": []}]
        ):
            raise HealthFailure("REST_OPERATION_DRIFT", "A REST operation identity, scope, or bearer policy drifted.")
        if _schema_digest(_openapi_operation_schema(operation, document, "input")) != expected["input_schema_sha256"]:
            raise HealthFailure("REST_SCHEMA_DRIFT", "A REST request schema does not match the MCP/package contract.")
        if _schema_digest(_openapi_operation_schema(operation, document, "output")) != expected["output_schema_sha256"]:
            raise HealthFailure("REST_SCHEMA_DRIFT", "A REST response schema does not match the MCP/package contract.")

    project_item = paths.get(PROJECT_METADATA_PATH)
    project_operation = project_item.get("get") if isinstance(project_item, dict) and set(project_item) == {"get"} else None
    if (
        not isinstance(project_operation, dict)
        or project_operation.get("operationId") != "get_project_metadata"
        or project_operation.get("x-acceptora-required-scope") != "projects:read"
        or project_operation.get("security") != [{"agentBearer": []}]
    ):
        raise HealthFailure("REST_OPERATION_DRIFT", "The REST project metadata operation drifted.")

    confirmation_item = paths.get(CONNECTION_CONFIRMATION_PATH)
    confirmation_operation = (
        confirmation_item.get("post")
        if isinstance(confirmation_item, dict) and set(confirmation_item) == {"post"}
        else None
    )
    if (
        not isinstance(confirmation_operation, dict)
        or confirmation_operation.get("operationId") != "confirm_connection"
        or confirmation_operation.get("x-acceptora-required-scopes") != REQUIRED_WORKFLOW_SCOPES
        or confirmation_operation.get("security") != [{"agentBearer": []}]
    ):
        raise HealthFailure("REST_OPERATION_DRIFT", "The REST setup confirmation operation drifted.")
    confirmation_request_schema = _openapi_operation_schema(confirmation_operation, document, "input")
    if confirmation_request_schema != CONNECTION_CONFIRMATION_REQUEST_SCHEMA:
        raise HealthFailure("REST_SCHEMA_DRIFT", "The setup confirmation request schema drifted.")
    confirmation_response_schema = _openapi_operation_schema(confirmation_operation, document, "output")
    if confirmation_response_schema != CONNECTION_CONFIRMATION_RESPONSE_SCHEMA:
        raise HealthFailure("REST_SCHEMA_DRIFT", "The setup confirmation response schema drifted.")

    legacy_item = paths.get(LEGACY_COMPLETION_GATE_PATH)
    legacy_operation = legacy_item.get("post") if isinstance(legacy_item, dict) and set(legacy_item) == {"post"} else None
    if (
        not isinstance(legacy_operation, dict)
        or legacy_operation.get("operationId") != "legacy_check_completion_gate"
        or legacy_operation.get("deprecated") is not True
        or legacy_operation.get("x-acceptora-required-scope") != "gates:read"
    ):
        raise HealthFailure("REST_OPERATION_DRIFT", "The legacy completion-gate compatibility operation drifted.")


def _validate_connection_confirmation(response: dict[str, Any] | None, expected_project_id: str) -> dict[str, Any]:
    expected_fields = {
        "project_id",
        "connection_status",
        "confirmed_at",
        "already_connected",
        "correlation_id",
    }
    if not isinstance(response, dict) or set(response) != expected_fields:
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation response fields drifted.")
    if response.get("project_id") != expected_project_id or response.get("connection_status") != "connected":
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation did not confirm the pinned project.")
    if not isinstance(response.get("already_connected"), bool):
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation idempotency state is invalid.")
    correlation_id = response.get("correlation_id")
    if not isinstance(correlation_id, str) or not 1 <= len(correlation_id) <= 255:
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation correlation ID is invalid.")
    confirmed_at = response.get("confirmed_at")
    if not isinstance(confirmed_at, str) or "T" not in confirmed_at:
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation timestamp is invalid.")
    try:
        timestamp_value = confirmed_at.removesuffix("Z") + ("+00:00" if confirmed_at.endswith("Z") else "")
        parsed_timestamp = datetime.fromisoformat(timestamp_value)
    except ValueError:
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation timestamp is invalid.") from None
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise HealthFailure("CONNECTION_CONFIRMATION_DRIFT", "The setup confirmation timestamp must include a timezone.")
    return response


def run_health(
    settings: Settings,
    manifest: dict[str, Any],
    *,
    confirm_connection: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    transport = Transport(settings, str(manifest["skill"]["version"]))

    version_response, _ = transport.request(settings.contract_version_url, method="GET")
    if not isinstance(version_response, dict) or not isinstance(version_response.get("data"), dict):
        raise HealthFailure("VERSION_RESPONSE_INVALID", "The contract-version endpoint returned an invalid envelope.")
    actual_versions = version_response["data"]
    expected_versions = {
        "contract_version": manifest["contract"]["version"],
        "integration_version": manifest["integration"]["version"],
        "skill_version": manifest["skill"]["version"],
    }
    if any(actual_versions.get(key) != value for key, value in expected_versions.items()):
        raise HealthFailure("VERSION_DRIFT", "The API contract, integration, or skill version does not match this package.")
    checks.append({"name": "api_versions", "status": "pass", "detail": "All three package/server versions match."})

    openapi_response, _ = transport.request(settings.openapi_url, method="GET")
    _validate_openapi(openapi_response, manifest, expected_versions)
    checks.append({"name": "rest_openapi", "status": "pass", "detail": "OpenAPI 3.1 versions, relative server, operations, scopes, and schemas match."})

    project_response, _ = transport.request(
        f"{settings.rest_base_url}/project",
        method="GET",
        token=settings.token,
    )
    if not isinstance(project_response, dict):
        raise HealthFailure("PROJECT_RESPONSE_INVALID", "The authenticated project endpoint returned an invalid response.")
    if project_response.get("project_id") != settings.project_id:
        raise HealthFailure("PROJECT_ID_MISMATCH", "The bearer token is not scoped to the pinned Acceptora project.")
    if project_response.get("versions") != expected_versions:
        raise HealthFailure("PROJECT_VERSION_DRIFT", "The authenticated project metadata versions do not match this package.")
    expected_endpoints = {
        "mcp": settings.mcp_url,
        "rest": settings.rest_base_url,
        "openapi": settings.openapi_url,
        "completion_gate": settings.completion_gate_url,
    }
    if project_response.get("endpoints") != expected_endpoints:
        raise HealthFailure("PROJECT_ENDPOINT_DRIFT", "The authenticated project metadata endpoints do not match the pinned origin and paths.")
    scopes = project_response.get("granted_scopes")
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise HealthFailure("PROJECT_SCOPE_INVALID", "The authenticated project metadata omitted granted scopes.")
    mandatory_scopes = set(REQUIRED_WORKFLOW_SCOPES)
    missing_scopes = sorted(mandatory_scopes.difference(scopes))
    if missing_scopes:
        raise HealthFailure("PROJECT_SCOPE_MISSING", "The bearer token lacks mandatory verification workflow scopes.")
    optional_warnings = [] if "exceptions:write" in scopes else ["Optional exceptions:write scope is not granted."]
    checks.append({"name": "project_scope", "status": "pass", "detail": "Project identity, endpoint origin, versions, lifecycle access, and mandatory scopes match."})

    protocol_version = manifest["contract"]["mcp_protocol_version"]
    initialize_id = 1
    initialize = {
        "jsonrpc": "2.0",
        "id": initialize_id,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "acceptora-health", "version": manifest["integration"]["version"]},
        },
    }
    initialize_response, initialize_headers = transport.request(
        settings.mcp_url,
        method="POST",
        token=settings.token,
        payload=initialize,
        request_id=initialize_id,
    )
    if initialize_response is None:
        raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP initialize response was empty.")
    initialized = _rpc_result(initialize_response, initialize_id)
    server_info = initialized.get("serverInfo")
    if (
        initialized.get("protocolVersion") != protocol_version
        or not isinstance(server_info, dict)
        or server_info.get("name") != manifest["server"]["name"]
        or server_info.get("version") != manifest["server"]["version"]
    ):
        raise HealthFailure("MCP_VERSION_DRIFT", "The MCP protocol or server identity/version does not match this package.")
    checks.append({"name": "mcp_initialize", "status": "pass", "detail": "Authenticated initialize and server version match."})

    protocol_headers = {"MCP-Protocol-Version": protocol_version}
    session_id = initialize_headers.get("MCP-Session-Id")
    if session_id:
        if not isinstance(session_id, str) or "\r" in session_id or "\n" in session_id:
            raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP server returned an invalid session identifier.")
        protocol_headers["MCP-Session-Id"] = session_id

    transport.request(
        settings.mcp_url,
        method="POST",
        token=settings.token,
        payload={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=protocol_headers,
        allow_empty=True,
    )

    tools: list[dict[str, Any]] = []
    decoded_tool_bytes = 0
    cursor: str | None = None
    request_id = 2
    for _ in range(20):
        params = {} if cursor is None else {"cursor": cursor}
        response, _ = transport.request(
            settings.mcp_url,
            method="POST",
            token=settings.token,
            payload={"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": params},
            headers=protocol_headers,
            request_id=request_id,
        )
        if response is None:
            raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP tools/list response was empty.")
        result = _rpc_result(response, request_id)
        page = result.get("tools")
        if not isinstance(page, list) or not all(isinstance(tool, dict) for tool in page):
            raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP tools/list result is invalid.")
        if len(tools) + len(page) > MAX_MCP_TOOL_COUNT:
            raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP tools/list result exceeded the aggregate tool limit.")
        decoded_tool_bytes += sum(
            len(json.dumps(tool, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            for tool in page
        )
        if decoded_tool_bytes > MAX_MCP_TOOL_DECODED_BYTES:
            raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP tools/list result exceeded the aggregate decoded-size limit.")
        tools.extend(page)
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP tools/list cursor is invalid.")
        cursor = next_cursor
        request_id += 1
    else:
        raise HealthFailure("MCP_PROTOCOL_DRIFT", "The MCP tools/list response exceeded the pagination limit.")

    actual_names = [tool.get("name") for tool in tools]
    expected_tools = {tool["name"]: tool for tool in manifest["tools"]}
    if (
        len(actual_names) != len(expected_tools)
        or len(set(actual_names)) != len(actual_names)
        or any(not isinstance(name, str) or TOOL_NAME_PATTERN.fullmatch(name) is None for name in actual_names)
        or set(actual_names) != set(expected_tools)
    ):
        raise HealthFailure("MCP_TOOL_DRIFT", "The MCP server does not expose the exact expected eight-tool surface.")

    for actual in tools:
        expected = expected_tools[actual["name"]]
        if (
            _schema_digest(actual.get("inputSchema")) != expected["input_schema_sha256"]
            or _schema_digest(actual.get("outputSchema")) != expected["output_schema_sha256"]
        ):
            raise HealthFailure("MCP_SCHEMA_DRIFT", "An MCP tool schema does not match the v1 package contract.")
        if actual.get("annotations") != expected["annotations"]:
            raise HealthFailure("MCP_ANNOTATION_DRIFT", "An MCP tool annotation map does not match the v1 package contract.")
    checks.append({"name": "mcp_tools", "status": "pass", "detail": "Exactly eight tool names and all input/output schema digests match."})

    confirmation: dict[str, Any] = {"requested": confirm_connection, "status": "not_requested"}
    if confirm_connection:
        try:
            confirmation_response, _ = transport.request(
                f"{settings.rest_base_url}/connection/confirm",
                method="POST",
                token=settings.token,
                payload={},
            )
            validated_confirmation = _validate_connection_confirmation(confirmation_response, settings.project_id)
        except HealthFailure as exc:
            raise HealthFailure(
                exc.code,
                str(exc),
                setup_state_write_may_have_occurred=True,
            ) from None
        confirmation = {
            "requested": True,
            "status": "confirmed",
            "project_id": validated_confirmation["project_id"],
            "connection_status": validated_confirmation["connection_status"],
            "confirmed_at": validated_confirmation["confirmed_at"],
            "already_connected": validated_confirmation["already_connected"],
            "correlation_id": validated_confirmation["correlation_id"],
        }
        checks.append({
            "name": "connection_confirmation",
            "status": "pass",
            "detail": "The server explicitly marked setup connected after every compatibility check passed.",
        })

    return {
        "ok": True,
        "operation_mode": "setup_connection_confirmation" if confirm_connection else "read_only_contract_probe",
        "product_write_tools_called": False,
        "setup_state_write_performed": confirm_connection,
        "authentication_telemetry_may_update": True,
        "connection_confirmation": confirmation,
        "checks": checks,
        "warnings": optional_warnings,
        "versions": expected_versions,
        "mcp": {
            "protocol_version": protocol_version,
            "server_name": manifest["server"]["name"],
            "server_version": manifest["server"]["version"],
            "tools": [tool["name"] for tool in manifest["tools"]],
        },
        "credential": {
            "source": "environment",
            "environment_variable": settings.token_env,
            "value_exposed": False,
        },
        "message": (
            "Setup confirmed: every health check passed and the pinned project connection is established."
            if confirm_connection
            else "Health check passed without marking the project connected."
        ),
    }


def _text_output(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"Health check failed [{result['error']['code']}]: {result['error']['message']}\n"
    lines = [result["message"] + " (No product write tools called; credential-use telemetry may update.)"]
    for check in result["checks"]:
        lines.append(f"- {check['name']}: {check['detail']}")
    for warning in result.get("warnings", []):
        lines.append(f"- warning: {warning}")
    lines.append("- credential: loaded from the configured environment variable; value not exposed")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Explicit external config; requires --accept-config-sha256.")
    parser.add_argument("--accept-config-sha256")
    parser.add_argument(
        "--confirm-connection",
        action="store_true",
        help="After every health check passes, explicitly mark the pinned project connection as established.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    token = ""
    confirmation_requested = False
    try:
        if sys.version_info < (3, 11):
            raise HealthFailure("PYTHON_UNSUPPORTED", "acceptora requires Python 3.11 or newer.")
        args = parse_args(argv)
        confirmation_requested = args.confirm_connection
        if args.config:
            requested_config = Path(args.config).expanduser().absolute()
            if requested_config.is_symlink() or not requested_config.is_file():
                raise HealthFailure("CONFIG_INVALID", "The explicit verification config must be an absolute regular file.")
            config_path = requested_config.resolve()
            config_body = _stable_read(config_path)
            digest = "sha256:" + hashlib.sha256(config_body).hexdigest()
            if args.accept_config_sha256 != digest:
                raise HealthFailure("CONFIG_NOT_ACCEPTED", "The explicit config requires its exact accepted SHA-256.")
        else:
            config_path = PINNED_RUNTIME_CONFIG
            if not config_path.is_file() or config_path.is_symlink():
                raise HealthFailure("CONFIG_MISSING", "Run health_check.py from the installer-owned external package or pass an accepted external config.")
            config_body = _stable_read(config_path)
        settings = load_settings(config_path, config_body)
        token = settings.token
        result = run_health(settings, load_manifest(), confirm_connection=confirmation_requested)
        exit_code = 0
    except HealthFailure as exc:
        result = {
            "ok": False,
            "operation_mode": "setup_connection_confirmation" if confirmation_requested else "read_only_contract_probe",
            "product_write_tools_called": False,
            "setup_state_write_performed": None if exc.setup_state_write_may_have_occurred else False,
            "authentication_telemetry_may_update": True,
            "connection_confirmation": {
                "requested": confirmation_requested,
                "status": (
                    "unknown"
                    if exc.setup_state_write_may_have_occurred
                    else ("not_performed" if confirmation_requested else "not_requested")
                ),
            },
            "error": {"code": exc.code, "message": str(exc)},
        }
        exit_code = 1
    except Exception:
        result = {
            "ok": False,
            "operation_mode": "setup_connection_confirmation" if confirmation_requested else "read_only_contract_probe",
            "product_write_tools_called": False,
            "setup_state_write_performed": None if confirmation_requested else False,
            "authentication_telemetry_may_update": True,
            "connection_confirmation": {
                "requested": confirmation_requested,
                "status": "unknown" if confirmation_requested else "not_requested",
            },
            "error": {
                "code": "HEALTH_CHECK_FAILED",
                "message": "The compatibility health check could not complete safely.",
            },
        }
        exit_code = 1

    output = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n" if args.format == "json" else _text_output(result)
    if token:
        output = output.replace(token, "[REDACTED]")
    sys.stdout.write(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
