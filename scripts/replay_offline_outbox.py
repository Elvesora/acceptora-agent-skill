#!/usr/bin/env python3
"""Replay pending Acceptora verification writes without changing their idempotency identity."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from validate_checklist_payload import FEATURE_ID, UUID_OR_ULID, canonical_json, find_secret_paths
from validate_gate_response import GateResponseError, validate_gate_response


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PINNED_RUNTIME_CONFIG = PACKAGE_ROOT.parent / "config" / "runtime-config.json"
PROJECT_TOKEN_ENV_PREFIX = "ACCEPTORA_AGENT_TOKEN_"
TOKEN_PATTERN = re.compile(r"^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$")
PROJECT_ID_PATTERN = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
SOURCE_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
SOURCE_REVISION_ID_PATTERN = re.compile(r"^src_[0-9A-HJKMNP-TV-Z]{26}$")
RESOLUTION_ID_PATTERN = re.compile(r"^resolution_[0-9A-HJKMNP-TV-Z]{26}$")
THREAD_ID_PATTERN = re.compile(r"^thread_[0-9A-HJKMNP-TV-Z]{26}$")
EXCEPTION_ID_PATTERN = re.compile(r"^exception_[0-9A-HJKMNP-TV-Z]{26}$")
SESSION_ID_PATTERN = re.compile(r"^[\x21-\x7e]{1,256}$")
OPERATIONS = {"reconcile_checklist", "address_feedback", "record_verification_exception"}
SUPPORTED_SCHEMA_VERSIONS = {"1.0", "1.1"}
RETRYABLE_CODES = {"RATE_LIMITED", "SERVICE_UNAVAILABLE", "COMPLETION_GATE_UNAVAILABLE"}
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SUCCESS_GATE_OUTCOMES = {"pass", "not_required"}
MAX_RESPONSE_BYTES = 4_000_000
MAX_CONFIG_BYTES = 1_048_576
MAX_ENVELOPE_BYTES = MAX_RESPONSE_BYTES + 1_000_000
SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,99}$")
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SERVER_NAME = "Acceptora Verification"
MCP_SERVER_VERSION = "1.0.0"


class ReplayConfigurationError(RuntimeError):
    pass


class EnvelopeError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
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


def _authenticated_opener(url: str) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirect()]
    if _is_http_loopback_url(url):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


@dataclass
class DeliveryError(RuntimeError):
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class ReplaySettings:
    mcp_url: str
    completion_gate_url: str | None
    token: str
    timeout_seconds: float
    retry_attempts: int
    retry_base_delay_seconds: float
    max_retry_delay_seconds: float
    processed_dir: Path
    outbox_dir: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _safe_message(code: str, message: object, token: str | None = None) -> str:
    candidate = str(message).replace("\r", " ").replace("\n", " ").strip()[:500]
    if token:
        candidate = candidate.replace(token, "[REDACTED]")
    if not candidate:
        return f"{code} occurred."
    if find_secret_paths({"message": candidate}):
        return f"{code} occurred; server details were redacted."
    return candidate


def _safe_error_code(value: object, fallback: str, token: str | None = None) -> str:
    if ERROR_CODE_PATTERN.fullmatch(fallback) is None:
        raise ValueError("The local fallback error code is invalid.")
    if not isinstance(value, str) or ERROR_CODE_PATTERN.fullmatch(value) is None:
        return fallback
    if token and token in value:
        return fallback
    if find_secret_paths({"code": value}):
        return fallback
    return value


def _safe_correlation_id(value: object, token: str | None = None) -> str | None:
    if not isinstance(value, str) or SAFE_CORRELATION_ID.fullmatch(value) is None:
        return None
    if token and token in value:
        return None
    if find_secret_paths({"correlation_id": value}):
        return None
    return value


def _validate_endpoint(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayConfigurationError(f"{label} is required")
    endpoint = value.strip()
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReplayConfigurationError(f"{label} must not contain credentials, a query, or a fragment")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReplayConfigurationError(f"{label} must be an absolute HTTP(S) URL")
    if parsed.scheme == "http" and not _is_http_loopback_url(endpoint):
        raise ReplayConfigurationError(f"{label} must use HTTPS unless it targets localhost")
    return endpoint


def _validated_token(value: object) -> str:
    if not isinstance(value, str) or TOKEN_PATTERN.fullmatch(value) is None:
        raise ReplayConfigurationError("Acceptora agent token is missing or malformed")
    return value


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or bool(is_junction and is_junction()):
        return True
    if os.name == "nt":
        try:
            attributes = path.lstat().st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def _assert_no_link_components(path: Path, label: str) -> None:
    if any(candidate.exists() and _is_linklike(candidate) for candidate in (path, *path.parents)):
        raise ReplayConfigurationError(f"{label} must not cross a symlink or junction")


def _pinned_outbox(config: dict[str, Any]) -> tuple[Path, Path]:
    target_value = config.get("target_root")
    if not isinstance(target_value, str) or not Path(target_value).is_absolute():
        raise ReplayConfigurationError("external config must pin an absolute target_root")
    target_requested = Path(target_value).absolute()
    _assert_no_link_components(target_requested, "target_root")
    target_root = target_requested.resolve(strict=False)
    if not target_root.is_dir():
        raise ReplayConfigurationError("external config target_root must be an existing directory")
    outbox_requested = target_root / ".verification" / "outbox"
    _assert_no_link_components(outbox_requested, "pinned outbox")
    return target_root, outbox_requested.resolve(strict=False)


def _validated_outbox_path(path: Path, outbox_root: Path, label: str) -> Path:
    requested = path.expanduser().absolute()
    _assert_no_link_components(requested, label)
    resolved = requested.resolve(strict=False)
    try:
        resolved.relative_to(outbox_root)
    except ValueError as error:
        raise ReplayConfigurationError(f"{label} must remain inside the pinned .verification/outbox subtree") from error
    return resolved


def _validated_outbox_record(path: Path, outbox_root: Path) -> Path:
    record = _validated_outbox_path(path, outbox_root, "outbox record")
    if record.parent != outbox_root:
        raise ReplayConfigurationError("outbox records must be direct children of the pinned outbox")
    if record.suffix.lower() != ".json":
        raise ReplayConfigurationError("outbox records must use the .json extension")
    if record.exists() and (not record.is_file() or _is_linklike(record)):
        raise ReplayConfigurationError("outbox records must be regular no-follow files")
    return record


def _read_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        if _is_linklike(path):
            raise OSError("symlinks and junctions are not allowed")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("not a regular file")
            if before.st_size > MAX_ENVELOPE_BYTES:
                raise OSError("file exceeds the offline envelope size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ENVELOPE_BYTES:
                    raise OSError("file exceeds the offline envelope size limit")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise OSError("file changed while it was read")
        finally:
            os.close(descriptor)
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnvelopeError(f"{label} is not readable JSON: {error}") from error
    if not isinstance(value, dict):
        raise EnvelopeError(f"{label} must contain a JSON object")
    return value


def _stable_read(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReplayConfigurationError("configuration must be a regular no-follow file") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReplayConfigurationError("configuration must be a regular file")
        if before.st_size > MAX_CONFIG_BYTES:
            raise ReplayConfigurationError("configuration exceeds the 1 MiB size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise ReplayConfigurationError("configuration exceeds the 1 MiB size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ReplayConfigurationError("configuration changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_config(path: Path | None, explicit: bool, body: bytes | None = None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.exists():
        if explicit:
            raise ReplayConfigurationError(f"configuration file does not exist: {path}")
        return {}
    try:
        value = json.loads((body if body is not None else _stable_read(path)).decode("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayConfigurationError(f"configuration is not readable JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReplayConfigurationError("configuration must contain a JSON object")
    return value


def _number(
    value: object,
    label: str,
    minimum: float,
    maximum: float,
    *,
    integer: bool = False,
) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayConfigurationError(f"{label} must be a number")
    if value < minimum or value > maximum:
        raise ReplayConfigurationError(f"{label} must be between {minimum} and {maximum}")
    if integer and not isinstance(value, int):
        raise ReplayConfigurationError(f"{label} must be an integer")
    return int(value) if integer else float(value)


def validate_envelope(path: Path, envelope: dict[str, Any]) -> dict[str, Any]:
    schema_version = envelope.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EnvelopeError(f"unsupported outbox schema_version: {schema_version!r}")
    if envelope.get("status") != "pending":
        raise EnvelopeError("only pending outbox records can be replayed")

    operation = envelope.get("operation")
    if operation not in OPERATIONS:
        raise EnvelopeError(f"operation must be one of {sorted(OPERATIONS)}")
    feature_id = envelope.get("feature_id")
    if not isinstance(feature_id, str) or not FEATURE_ID.fullmatch(feature_id):
        raise EnvelopeError("feature_id must be feat_ followed by one ULID")
    idempotency_key = envelope.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not UUID_OR_ULID.fullmatch(idempotency_key):
        raise EnvelopeError("idempotency_key must be a UUID or ULID")
    if path.name != f"{operation}-{idempotency_key}.json":
        raise EnvelopeError("outbox filename does not match its operation and idempotency key")

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise EnvelopeError("payload must be a JSON object")
    if payload.get("feature_id") != feature_id:
        raise EnvelopeError("payload.feature_id does not match the outbox feature_id")
    if payload.get("idempotency_key") != idempotency_key:
        raise EnvelopeError("payload.idempotency_key does not match the outbox idempotency_key")
    if find_secret_paths(payload):
        raise EnvelopeError("SECRET_REJECTED: pending payload contains possible credential material")
    if envelope.get("canonical_payload_sha256") != _sha256(payload):
        raise EnvelopeError("canonical payload hash does not match the pending payload")

    completion_gate = envelope.get("completion_gate")
    if completion_gate is not None:
        if not isinstance(completion_gate, dict) or not isinstance(completion_gate.get("payload"), dict):
            raise EnvelopeError("completion_gate must contain a payload object")
        gate_payload = completion_gate["payload"]
        if find_secret_paths(gate_payload):
            raise EnvelopeError("SECRET_REJECTED: completion-gate payload contains possible credential material")
        if completion_gate.get("canonical_payload_sha256") != _sha256(gate_payload):
            raise EnvelopeError("completion-gate payload hash does not match its payload")

    attempts = envelope.get("attempt_count", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise EnvelopeError("attempt_count must be a non-negative integer")
    return envelope


def _decode_response(body: bytes, headers: Message, request_id: int | None) -> dict[str, Any]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise DeliveryError("PAYLOAD_TOO_LARGE", "Server response exceeded the replay safety limit.")
    text = body.decode("utf-8", errors="strict").strip()
    if not text:
        return {}
    content_type = headers.get("Content-Type", "")
    candidates: list[str]
    if "text/event-stream" in content_type:
        candidates = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    else:
        candidates = [text]
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and (request_id is None or decoded.get("id") == request_id):
            return decoded
    raise DeliveryError("PROTOCOL_ERROR", "Server returned an unreadable JSON response.")


def _retry_after(headers: Message) -> float | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)


def _error_from_body(
    body: bytes,
    headers: Message,
    status: int,
    token: str,
) -> DeliveryError:
    code = f"HTTP_{status}"
    message: object = f"Server returned HTTP {status}."
    try:
        decoded = _decode_response(body, headers, None)
    except (DeliveryError, UnicodeDecodeError):
        decoded = {}
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if isinstance(error, dict):
        code = _safe_error_code(error.get("code"), code, token)
        if isinstance(error.get("message"), str):
            message = error["message"]
    retryable = status in RETRYABLE_HTTP_STATUSES or code in RETRYABLE_CODES
    return DeliveryError(
        code,
        _safe_message(code, message, token),
        retryable=retryable,
        retry_after_seconds=_retry_after(headers),
    )


def _post_json(
    url: str,
    payload: dict[str, Any],
    token: str,
    timeout_seconds: float,
    *,
    headers: dict[str, str] | None = None,
    request_id: int | None = None,
    allow_empty: bool = False,
) -> tuple[dict[str, Any], Message]:
    token = _validated_token(token)
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "acceptora-offline-replay/1.0.0",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=canonical_json(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    opener = _authenticated_opener(url)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            response_headers = response.headers
    except urllib.error.HTTPError as error:
        try:
            body = error.read(MAX_RESPONSE_BYTES + 1)
            delivery_error = _error_from_body(body, error.headers, error.code, token)
        finally:
            error.close()
        raise delivery_error from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error.__class__.__name__)
        raise DeliveryError(
            "SERVICE_UNAVAILABLE",
            _safe_message("SERVICE_UNAVAILABLE", f"Network request failed: {reason}", token),
            retryable=True,
        ) from None
    if not body and allow_empty:
        return {}, response_headers
    try:
        return _decode_response(body, response_headers, request_id), response_headers
    except UnicodeDecodeError:
        raise DeliveryError("PROTOCOL_ERROR", "Server response was not valid UTF-8.") from None


def _extract_mcp_error(result: dict[str, Any], token: str) -> DeliveryError:
    code = "MCP_TOOL_ERROR"
    message: object = "MCP tool rejected the pending write."
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            continue
        try:
            decoded = json.loads(item["text"])
        except json.JSONDecodeError:
            continue
        error = decoded.get("error") if isinstance(decoded, dict) else None
        if isinstance(error, dict):
            code = _safe_error_code(error.get("code"), "MCP_TOOL_ERROR", token)
            if isinstance(error.get("message"), str):
                message = error["message"]
            retryable = bool(error.get("retryable")) or code in RETRYABLE_CODES
            return DeliveryError(code, _safe_message(code, message, token), retryable=retryable)
    return DeliveryError(code, _safe_message(code, message, token), retryable=code in RETRYABLE_CODES)


def _rpc_result(response: dict[str, Any], request_id: int, token: str) -> dict[str, Any]:
    if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
        raise DeliveryError("PROTOCOL_ERROR", "MCP returned an invalid JSON-RPC envelope.")
    if "error" in response:
        error = response.get("error")
        message = error.get("message") if isinstance(error, dict) else "MCP JSON-RPC error."
        raise DeliveryError("PROTOCOL_ERROR", _safe_message("PROTOCOL_ERROR", message, token))
    result = response.get("result")
    if not isinstance(result, dict):
        raise DeliveryError("PROTOCOL_ERROR", "MCP returned no result object.")
    return result


def _session_id(headers: Message) -> str | None:
    value = headers.get("MCP-Session-Id")
    if value is None:
        return None
    if SESSION_ID_PATTERN.fullmatch(value) is None:
        raise DeliveryError("PROTOCOL_ERROR", "MCP returned an invalid session identifier.")
    return value


def _validate_response_session(headers: Message, expected: str | None) -> None:
    returned = _session_id(headers)
    if returned is not None and returned != expected:
        raise DeliveryError("PROTOCOL_ERROR", "MCP changed the session identifier after initialization.")


def _validate_tool_acknowledgement(operation: str, structured: dict[str, Any], envelope: dict[str, Any]) -> None:
    payload = envelope["payload"]
    if structured.get("feature_id") != envelope["feature_id"]:
        raise DeliveryError("FEATURE_ID_MISMATCH", "MCP response did not match the pending feature identity.")
    source_digest = structured.get("source_digest")
    if (
        not isinstance(source_digest, str)
        or SOURCE_DIGEST_PATTERN.fullmatch(source_digest) is None
        or source_digest != payload.get("source_digest")
    ):
        raise DeliveryError("SOURCE_DIGEST_MISMATCH", "MCP response did not acknowledge the pending source digest.")
    if not isinstance(structured.get("idempotency_replayed"), bool):
        raise DeliveryError("PROTOCOL_ERROR", "MCP response omitted its idempotency acknowledgement.")
    correlation_id = structured.get("correlation_id")
    if not isinstance(correlation_id, str) or SAFE_CORRELATION_ID.fullmatch(correlation_id) is None:
        raise DeliveryError("PROTOCOL_ERROR", "MCP response omitted a safe correlation identifier.")

    if operation == "reconcile_checklist":
        revision = structured.get("new_checklist_revision")
        if (
            not isinstance(structured.get("source_revision_id"), str)
            or SOURCE_REVISION_ID_PATTERN.fullmatch(structured["source_revision_id"]) is None
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise DeliveryError("PROTOCOL_ERROR", "MCP reconciliation response omitted its revision acknowledgement.")
        return

    if operation == "address_feedback":
        resolution_ids = structured.get("resolution_ids")
        thread_states = structured.get("thread_states")
        resolutions = payload.get("resolutions")
        if (
            not isinstance(resolution_ids, list)
            or not resolution_ids
            or not isinstance(resolutions, list)
            or not resolutions
            or len(resolution_ids) != len(resolutions)
            or len(resolution_ids) != len(set(resolution_ids))
            or any(not isinstance(item, str) or RESOLUTION_ID_PATTERN.fullmatch(item) is None for item in resolution_ids)
            or not isinstance(thread_states, list)
            or not thread_states
            or structured.get("completion_gate") != "continue_sync"
        ):
            raise DeliveryError("PROTOCOL_ERROR", "MCP feedback response omitted its resolution acknowledgement.")
        expected_thread_ids = {
            resolution.get("thread_id")
            for resolution in resolutions
            if isinstance(resolution, dict) and isinstance(resolution.get("thread_id"), str)
        }
        if (
            len(expected_thread_ids) != len(resolutions)
            or any(THREAD_ID_PATTERN.fullmatch(thread_id) is None for thread_id in expected_thread_ids)
        ):
            raise DeliveryError("PROTOCOL_ERROR", "Pending feedback payload contains invalid thread identity.")
        acknowledged_thread_ids: set[str] = set()
        for state in thread_states:
            version = state.get("thread_version") if isinstance(state, dict) else None
            if (
                not isinstance(state, dict)
                or not isinstance(state.get("thread_id"), str)
                or THREAD_ID_PATTERN.fullmatch(state["thread_id"]) is None
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
                or state.get("state") != "fix_submitted"
            ):
                raise DeliveryError("PROTOCOL_ERROR", "MCP feedback response contained an invalid thread acknowledgement.")
            acknowledged_thread_ids.add(state["thread_id"])
        if (
            len(acknowledged_thread_ids) != len(thread_states)
            or acknowledged_thread_ids != expected_thread_ids
        ):
            raise DeliveryError("THREAD_ID_MISMATCH", "MCP response did not match the pending feedback thread identities.")
        return

    if operation == "record_verification_exception":
        if (
            not isinstance(structured.get("exception_id"), str)
            or EXCEPTION_ID_PATTERN.fullmatch(structured["exception_id"]) is None
            or structured.get("feature_status") != "no_manual_verification_required"
            or structured.get("completion_gate") != "pass"
        ):
            raise DeliveryError("PROTOCOL_ERROR", "MCP exception response omitted its exception acknowledgement.")
        return

    raise DeliveryError("PROTOCOL_ERROR", "MCP response used an unsupported operation identity.")


def call_mcp_tool(settings: ReplaySettings, envelope: dict[str, Any]) -> dict[str, Any]:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "acceptora-offline-replay", "version": "1.0.0"},
        },
    }
    initialized, headers = _post_json(
        settings.mcp_url,
        initialize,
        settings.token,
        settings.timeout_seconds,
        request_id=1,
    )
    initialize_result = _rpc_result(initialized, 1, settings.token)
    server_info = initialize_result.get("serverInfo")
    capabilities = initialize_result.get("capabilities")
    if (
        initialize_result.get("protocolVersion") != MCP_PROTOCOL_VERSION
        or not isinstance(capabilities, dict)
        or not isinstance(capabilities.get("tools"), dict)
        or not isinstance(server_info, dict)
        or server_info.get("name") != MCP_SERVER_NAME
        or server_info.get("version") != MCP_SERVER_VERSION
    ):
        raise DeliveryError("PROTOCOL_ERROR", "MCP initialize identity did not match the pinned Acceptora contract.")
    session_id = _session_id(headers)
    protocol_headers = {"MCP-Protocol-Version": MCP_PROTOCOL_VERSION}
    if session_id:
        protocol_headers["MCP-Session-Id"] = session_id

    _, notification_headers = _post_json(
        settings.mcp_url,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        settings.token,
        settings.timeout_seconds,
        headers=protocol_headers,
        allow_empty=True,
    )
    _validate_response_session(notification_headers, session_id)
    response, response_headers = _post_json(
        settings.mcp_url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": envelope["operation"], "arguments": envelope["payload"]},
        },
        settings.token,
        settings.timeout_seconds,
        headers=protocol_headers,
        request_id=2,
    )
    _validate_response_session(response_headers, session_id)
    result = _rpc_result(response, 2, settings.token)
    if result.get("isError") is True:
        raise _extract_mcp_error(result, settings.token)
    if "isError" in result and not isinstance(result.get("isError"), bool):
        raise DeliveryError("PROTOCOL_ERROR", "MCP tools/call returned an invalid isError value.")
    content = result.get("content")
    if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
        raise DeliveryError("PROTOCOL_ERROR", "MCP tool response omitted its content array.")

    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise DeliveryError("PROTOCOL_ERROR", "MCP tool success omitted structuredContent.")
    _validate_tool_acknowledgement(envelope["operation"], structured, envelope)
    return structured


def call_completion_gate(
    settings: ReplaySettings,
    gate_payload: dict[str, Any],
    expected_feature_id: str,
    expected_checklist_revision: int | None,
) -> dict[str, Any]:
    if settings.completion_gate_url is None:
        raise DeliveryError(
            "COMPLETION_GATE_URL_REQUIRED",
            "This record includes a completion-gate payload but no completion_gate_url is configured.",
        )
    response, _ = _post_json(
        settings.completion_gate_url,
        gate_payload,
        settings.token,
        settings.timeout_seconds,
        headers={"Accept": "application/json"},
    )
    error = response.get("error")
    if isinstance(error, dict):
        code = _safe_error_code(error.get("code"), "COMPLETION_GATE_ERROR", settings.token)
        message = error.get("message", "Completion gate rejected the request.")
        raise DeliveryError(
            code,
            _safe_message(code, message, settings.token),
            retryable=bool(error.get("retryable")) or code in RETRYABLE_CODES,
        )
    try:
        response = validate_gate_response(
            response,
            gate_payload,
            token=settings.token,
            expected_feature_id=expected_feature_id,
            expected_checklist_revision=expected_checklist_revision,
        )
    except GateResponseError as error:
        raise DeliveryError("PROTOCOL_ERROR", str(error)) from error
    outcome = response["outcome"]
    if outcome in SUCCESS_GATE_OUTCOMES:
        return response
    if outcome == "unavailable":
        raise DeliveryError(
            "COMPLETION_GATE_UNAVAILABLE",
            "Completion gate reported unavailable.",
            retryable=True,
        )
    if outcome in {"continue_sync", "ambiguous"}:
        raise DeliveryError(
            f"COMPLETION_{str(outcome).upper()}",
            f"Completion gate reported {outcome}; the recovery record remains pending.",
        )
    raise DeliveryError("PROTOCOL_ERROR", "Completion gate returned an unsupported outcome.")


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _record_failure(
    path: Path,
    envelope: dict[str, Any],
    error: DeliveryError,
    attempt_count: int,
    token: str,
) -> None:
    code = _safe_error_code(error.code, "DELIVERY_ERROR", token)
    envelope["status"] = "pending"
    envelope["attempt_count"] = attempt_count
    envelope["last_attempted_at"] = _utc_now()
    envelope["last_error_code"] = code
    envelope["last_error_message"] = _safe_message(code, error.message, token)
    envelope.pop("delivery_receipt", None)
    _atomic_write(path, envelope)


def _archive_success(
    path: Path,
    envelope: dict[str, Any],
    settings: ReplaySettings,
    attempt_count: int,
    tool_result: dict[str, Any],
    gate_result: dict[str, Any] | None,
) -> Path:
    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.processed_dir / path.name
    archive_lock = destination.with_suffix(destination.suffix + ".archive.lock")
    try:
        with archive_lock.open("x", encoding="utf-8") as lock:
            lock.write(f"pid={os.getpid()}\n")
    except FileExistsError:
        raise DeliveryError("ARCHIVE_LOCKED", "Another process is archiving this idempotency identity.") from None
    if destination.exists():
        archive_lock.unlink()
        raise DeliveryError("ARCHIVE_CONFLICT", "A processed record already exists with this idempotency identity.")
    correlation_id = _safe_correlation_id(tool_result.get("correlation_id"), settings.token)
    gate_correlation_id = (
        _safe_correlation_id(gate_result.get("correlation_id"), settings.token) if gate_result else None
    )
    envelope["status"] = "delivered"
    envelope["attempt_count"] = attempt_count
    envelope["last_attempted_at"] = _utc_now()
    envelope["last_error_code"] = None
    envelope["last_error_message"] = None
    envelope["delivered_at"] = _utc_now()
    envelope["delivery_receipt"] = {
        "operation": envelope["operation"],
        "mcp_correlation_id": correlation_id,
        "idempotency_replayed": bool(tool_result.get("idempotency_replayed", False)),
        "completion_gate_outcome": gate_result.get("outcome") if gate_result else None,
        "completion_gate_correlation_id": gate_correlation_id,
    }
    temporary = settings.processed_dir / f".{path.name}.{os.getpid()}.tmp"
    created_destination = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        created_destination = True
        if os.name != "nt":
            destination.chmod(0o600)
        path.unlink()
    except Exception:
        if created_destination and destination.exists() and path.exists():
            destination.unlink()
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
        if archive_lock.exists():
            archive_lock.unlink()
    return destination


def replay_record(path: Path, settings: ReplaySettings) -> dict[str, Any]:
    path = _validated_outbox_record(path, settings.outbox_dir)
    envelope = validate_envelope(path, _read_json_file(path, path.name))
    lock_path = path.with_suffix(path.suffix + ".replay.lock")
    try:
        with lock_path.open("x", encoding="utf-8") as lock:
            lock.write(f"pid={os.getpid()}\n")
    except FileExistsError:
        return {
            "path": path.name,
            "operation": envelope["operation"],
            "status": "failed",
            "attempts_this_run": 0,
            "error_code": "REPLAY_LOCKED",
            "error_message": "Another process is replaying this record.",
        }

    starting_attempts = envelope.get("attempt_count", 0)
    last_error: DeliveryError | None = None
    try:
        for index in range(settings.retry_attempts):
            total_attempts = starting_attempts + index + 1
            try:
                tool_result = call_mcp_tool(settings, envelope)
                gate_result = None
                completion_gate = envelope.get("completion_gate")
                if isinstance(completion_gate, dict):
                    expected_revision = (
                        tool_result.get("new_checklist_revision")
                        if envelope["operation"] == "reconcile_checklist"
                        else None
                    )
                    gate_result = call_completion_gate(
                        settings,
                        completion_gate["payload"],
                        envelope["feature_id"],
                        expected_revision,
                    )
                archive = _archive_success(
                    path,
                    envelope,
                    settings,
                    total_attempts,
                    tool_result,
                    gate_result,
                )
                return {
                    "path": path.name,
                    "operation": envelope["operation"],
                    "status": "delivered",
                    "attempts_this_run": index + 1,
                    "completion_gate_outcome": gate_result.get("outcome") if gate_result else None,
                    "processed_path": str(archive),
                }
            except DeliveryError as error:
                safe_code = _safe_error_code(error.code, "DELIVERY_ERROR", settings.token)
                last_error = DeliveryError(
                    safe_code,
                    _safe_message(safe_code, error.message, settings.token),
                    error.retryable,
                    error.retry_after_seconds,
                )
                _record_failure(path, envelope, last_error, total_attempts, settings.token)
                if not error.retryable or index + 1 >= settings.retry_attempts:
                    break
                delay = (
                    error.retry_after_seconds
                    if error.retry_after_seconds is not None
                    else settings.retry_base_delay_seconds * (2**index)
                )
                time.sleep(min(delay, settings.max_retry_delay_seconds))
        assert last_error is not None
        return {
            "path": path.name,
            "operation": envelope["operation"],
            "status": "failed",
            "attempts_this_run": total_attempts - starting_attempts,
            "error_code": last_error.code,
            "error_message": last_error.message,
        }
    finally:
        if lock_path.exists():
            lock_path.unlink()


def _discover(path: Path, outbox_root: Path) -> list[Path]:
    path = _validated_outbox_path(path, outbox_root, "outbox path")
    if path.is_file():
        return [_validated_outbox_record(path, outbox_root)]
    if not path.exists():
        return []
    if not path.is_dir():
        raise ReplayConfigurationError("outbox path must be a JSON file or directory")
    if path != outbox_root:
        raise ReplayConfigurationError("outbox directory must be the pinned .verification/outbox directory")
    return sorted(
        (_validated_outbox_record(candidate, outbox_root) for candidate in path.glob("*.json")),
        key=lambda item: item.name,
    )


def _settings(
    arguments: argparse.Namespace,
    config: dict[str, Any],
    outbox_path: Path,
    outbox_root: Path,
    needs_completion_gate: bool,
) -> ReplaySettings:
    mcp_url = _validate_endpoint(config.get("mcp_url"), "mcp_url")
    completion_value = config.get("completion_gate_url")
    completion_gate_url = (
        _validate_endpoint(completion_value, "completion_gate_url") if completion_value else None
    )
    if needs_completion_gate and completion_gate_url is None:
        raise ReplayConfigurationError("completion_gate_url is required by at least one pending record")

    project_id = config.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ReplayConfigurationError("project_id must use the public proj_<ULID> form")
    expected_token_env = f"{PROJECT_TOKEN_ENV_PREFIX}{project_id.upper()}"
    if config.get("token_env") != expected_token_env:
        raise ReplayConfigurationError(f"token_env must be pinned to {expected_token_env}")
    token = _validated_token(os.environ.get(expected_token_env))
    if completion_gate_url is not None:
        mcp_origin = (urlsplit(mcp_url).scheme, urlsplit(mcp_url).netloc)
        gate_origin = (urlsplit(completion_gate_url).scheme, urlsplit(completion_gate_url).netloc)
        if mcp_origin != gate_origin:
            raise ReplayConfigurationError("MCP and completion-gate endpoints must use the same pinned origin")

    timeout = _number(
        arguments.timeout_seconds if arguments.timeout_seconds is not None else config.get("timeout_seconds", 8),
        "timeout_seconds",
        0.1,
        120,
    )
    retry_attempts = _number(
        arguments.retry_attempts if arguments.retry_attempts is not None else config.get("retry_attempts", 3),
        "retry_attempts",
        1,
        10,
        integer=True,
    )
    retry_base = _number(
        arguments.retry_base_delay_seconds
        if arguments.retry_base_delay_seconds is not None
        else config.get("retry_base_delay_seconds", 0.5),
        "retry_base_delay_seconds",
        0,
        30,
    )
    max_retry = _number(
        arguments.max_retry_delay_seconds
        if arguments.max_retry_delay_seconds is not None
        else config.get("max_retry_delay_seconds", 30),
        "max_retry_delay_seconds",
        0,
        300,
    )

    default_processed = outbox_path.parent / "processed" if outbox_path.is_file() else outbox_path / "processed"
    target_root, canonical_outbox = _pinned_outbox(config)
    if canonical_outbox != outbox_root:
        raise ReplayConfigurationError("the pinned outbox changed while replay settings were loaded")
    processed_value = config.get("processed_outbox")
    if processed_value:
        processed_candidate = Path(processed_value)
        processed = (target_root / processed_candidate).absolute() if not processed_candidate.is_absolute() else processed_candidate.absolute()
    else:
        processed = default_processed.absolute()
    processed = _validated_outbox_path(processed, outbox_root, "processed_outbox")
    return ReplaySettings(
        mcp_url=mcp_url,
        completion_gate_url=completion_gate_url,
        token=token,
        timeout_seconds=float(timeout),
        retry_attempts=int(retry_attempts),
        retry_base_delay_seconds=float(retry_base),
        max_retry_delay_seconds=float(max_retry),
        processed_dir=processed,
        outbox_dir=outbox_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="pending record or outbox directory; defaults to the pinned target outbox")
    parser.add_argument("--config", help="Explicit external config; requires --accept-config-sha256.")
    parser.add_argument("--accept-config-sha256")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--retry-attempts", type=int)
    parser.add_argument("--retry-base-delay-seconds", type=float)
    parser.add_argument("--max-retry-delay-seconds", type=float)
    parser.add_argument("--dry-run", action="store_true", help="validate and list records without network access")
    arguments = parser.parse_args(argv)

    try:
        if arguments.config:
            requested_config = Path(arguments.config).expanduser().absolute()
            if requested_config.is_symlink() or not requested_config.is_file():
                raise ReplayConfigurationError("explicit config must be an absolute regular file")
            config_path = requested_config.resolve()
            config_body = _stable_read(config_path)
            config_digest = "sha256:" + hashlib.sha256(config_body).hexdigest()
            if arguments.accept_config_sha256 != config_digest:
                raise ReplayConfigurationError("explicit config requires its exact accepted SHA-256")
        else:
            config_path = PINNED_RUNTIME_CONFIG
            if config_path.is_symlink() or not config_path.is_file():
                raise ReplayConfigurationError("run replay from the installer-owned external package or pass an accepted external config")
            config_body = _stable_read(config_path)
        config = _load_config(config_path, True, config_body)
        _, outbox_root = _pinned_outbox(config)
        outbox_path = _validated_outbox_path(
            Path(arguments.path) if arguments.path else outbox_root,
            outbox_root,
            "outbox path",
        )
        records = _discover(outbox_path, outbox_root)
        validated: list[tuple[Path, dict[str, Any]]] = []
        validation_results: list[dict[str, Any]] = []
        for record in records:
            try:
                envelope = validate_envelope(record, _read_json_file(record, record.name))
                validated.append((record, envelope))
                validation_results.append(
                    {"path": record.name, "operation": envelope["operation"], "status": "valid"}
                )
            except EnvelopeError as error:
                validation_results.append(
                    {
                        "path": record.name,
                        "status": "invalid",
                        "error_code": "OUTBOX_INVALID",
                        "error_message": _safe_message("OUTBOX_INVALID", error),
                    }
                )

        if arguments.dry_run:
            summary = {
                "dry_run": True,
                "discovered": len(records),
                "valid": len(validated),
                "invalid": len(records) - len(validated),
                "results": validation_results,
            }
            sys.stdout.write(canonical_json(summary) + "\n")
            return 0 if len(validated) == len(records) else 1

        if not validated:
            invalid_results = [item for item in validation_results if item["status"] == "invalid"]
            sys.stdout.write(
                canonical_json(
                    {
                        "dry_run": False,
                        "discovered": len(records),
                        "delivered": 0,
                        "failed": len(invalid_results),
                        "results": invalid_results,
                    }
                )
                + "\n"
            )
            return 0 if not records else 1

        needs_gate = any(isinstance(envelope.get("completion_gate"), dict) for _, envelope in validated)
        settings = _settings(arguments, config, outbox_path, outbox_root, needs_gate)

        results = [item for item in validation_results if item["status"] == "invalid"]
        results.extend(replay_record(path, settings) for path, _ in validated)
        delivered = sum(result["status"] == "delivered" for result in results)
        failed = len(results) - delivered
        summary = {
            "dry_run": False,
            "discovered": len(records),
            "delivered": delivered,
            "failed": failed,
            "results": results,
        }
        sys.stdout.write(canonical_json(summary) + "\n")
        return 0 if failed == 0 else 1
    except (ReplayConfigurationError, EnvelopeError, OSError) as error:
        sys.stderr.write(
            canonical_json(
                {
                    "error": "OUTBOX_REPLAY_FAILED",
                    "message": _safe_message("OUTBOX_REPLAY_FAILED", error),
                }
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
