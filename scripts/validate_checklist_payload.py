#!/usr/bin/env python3
"""Validate the Acceptora v1 reconcile_checklist payload without third-party dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


OPERATIONS = {"retain", "editorial_update", "material_update", "add", "retire", "reopen", "rename_key"}
RISKS = {"critical", "high", "normal", "low"}
LIMIT_STATUSES = {"open", "resolved", "retired"}
SOURCE_KINDS = {"git", "pull_request", "content", "file_manifest", "opaque"}
CHANGE_KINDS = {"added", "modified", "deleted", "renamed"}
OBSERVED_BY_KINDS = {"adapter", "registered_derived", "agent_enrichment"}
EVIDENCE_KINDS = {"command", "test", "build", "lint", "browser", "security", "other"}
EVIDENCE_OUTCOMES = {"passed", "failed", "warning", "not_run"}
EVIDENCE_SUFFICIENCIES = {"sufficient", "insufficient"}
EVIDENCE_BLOCKER_REASONS = {
    "missing_credentials",
    "insufficient_permissions",
    "missing_prerequisite",
    "environment_unavailable",
    "external_dependency_unavailable",
    "resource_limit",
    "other",
}
EVIDENCE_PROPERTIES = {
    "semantic_key",
    "kind",
    "name",
    "target",
    "executed_at",
    "outcome",
    "exit_status",
    "summary",
    "redacted_excerpt",
    "source_revision",
    "evidence_sufficiency",
    "blocker_reason",
    "lineage",
}
LINEAGE_PROPERTIES = {
    "project_id",
    "provider",
    "provider_run_id",
    "environment",
    "started_at",
    "ended_at",
    "duration_ms",
    "artifact",
    "assertion",
    "authentication",
    "cost",
    "usage",
    "stop_reason",
    "original_payload_reference",
}
LINEAGE_REFERENCE_PROPERTIES = {"uri", "digest"}
LINEAGE_ASSERTION_PROPERTIES = {"identity", "details"}
FORBIDDEN_LINEAGE_ASSERTION_DETAIL_KEYS = {
    "body",
    "payload",
    "raw_body",
    "raw_payload",
    "request",
    "request_body",
    "request_payload",
    "response",
    "response_body",
    "response_payload",
    "log",
    "logs",
    "raw_logs",
}
LINEAGE_AUTHENTICATION_PROPERTIES = {"mode", "outcome"}
LINEAGE_COST_PROPERTIES = {"amount", "currency"}
LINEAGE_USAGE_PROPERTIES = {"metric", "quantity", "unit"}
SOURCE_DESCRIPTOR_PROPERTIES = {
    "source_kind",
    "source_locator",
    "opaque_revision",
    "base_revision",
    "adapter_kind",
    "adapter_version",
    "metadata",
}
CHANGED_SURFACE_PROPERTIES = {
    "anchor",
    "change_kind",
    "observed_by",
    "previous_anchor",
    "content_digest",
    "metadata",
}
SOURCE_MANIFEST_PROPERTIES = {"schema_version", "base_digest", "current_digest", "entries", "ignored_entries"}
EVIDENCE_REFERENCE_PROPERTIES = {"kind", "value", "summary"}
EVIDENCE_REFERENCE_KINDS = {"path", "commit", "url", "command", "test", "log", "attachment", "other"}
KNOWN_LIMIT_PROPERTIES = {
    "semantic_key",
    "description",
    "reason",
    "affected_coverage_anchors",
    "severity",
    "blocks_acceptance",
    "status",
    "mitigation",
    "resolution_evidence",
    "policy_rule",
    "policy_version",
}
SECTION_PROPERTIES = {"semantic_key", "title", "description", "display_order", "collapsed_default", "pattern_key"}
ITEM_PROPERTIES = {
    "item_id",
    "item_revision_id",
    "semantic_key",
    "previous_semantic_key",
    "section_semantic_key",
    "display_order",
    "operation",
    "operation_reason",
    "title",
    "action",
    "expected_result",
    "why_it_matters",
    "preconditions",
    "environment",
    "target",
    "test_data",
    "coverage_anchors",
    "global_anchor_reason",
    "source_references",
    "risk",
    "required",
    "estimated_seconds",
    "production_impact",
    "side_effect_warning",
    "invalidation_reason",
}
VERSION_PROPERTIES = {"integration_name", "integration_version", "skill_version", "contract_version"}
VERIFICATION_INSTRUCTION_CONTEXT_PROPERTIES = {
    "account_revision",
    "project_revision",
    "effective_digest",
}
SEMANTIC_KEY = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,199}$")
SEMANTIC_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
ANCHOR = re.compile(r"^(file|route|api|component|config|data|content|global):.+$")
UUID_OR_ULID = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)
FEATURE_ID = re.compile(r"^feat_[0-9A-HJKMNP-TV-Z]{26}$")
PROJECT_ID = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
ITEM_ID = re.compile(r"^item_[0-9A-HJKMNP-TV-Z]{26}$")
ITEM_REVISION_ID = re.compile(r"^irev_[0-9A-HJKMNP-TV-Z]{26}$")
RESOLUTION_ID = re.compile(r"^resolution_[0-9A-HJKMNP-TV-Z]{26}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
LINEAGE_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
PROVIDER_RUN_ID = re.compile(r"^[^\s\r\n](?:[^\r\n]*[^\s\r\n])?$")
LINEAGE_DATE_TIME = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,3})?(?:Z|[+-](?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00))$"
)
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
FORBIDDEN_LINEAGE_URI_SCHEMES = {"data", "file", "javascript", "vbscript"}
SENSITIVE_LINEAGE_URI_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
}
SENSITIVE_LINEAGE_URI_COMPACT_KEYS = {
    "accesstoken",
    "apikey",
    "clientsecret",
    "proxyauthorization",
    "refreshtoken",
    "setcookie",
}
SENSITIVE_QUERY_KEYS = {
    "awsaccesskeyid",
    "signature",
    "sig",
    "x_amz_credential",
    "x_amz_security_token",
    "x_amz_signature",
    "x_goog_credential",
    "x_goog_signature",
}
SENSITIVE_QUERY_COMPACT_KEYS = {key.replace("_", "") for key in SENSITIVE_QUERY_KEYS}
SENSITIVE_LINEAGE_URI_KEY_SEGMENT = re.compile(r"(?:^|_)(?:password|passwd|secret|token)(?:_|$)")
COST_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?$")
COST_CURRENCY = re.compile(r"^[A-Z]{3}$")
AUTHENTICATION_OUTCOMES = {"not_required", "succeeded", "failed", "blocked", "unknown"}
MAX_LINEAGE_DURATION_MS = 2_678_400_000
RFC3339_DATE_TIME = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"[Tt](?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.[0-9]+)?(?:[Zz]|(?P<offset_sign>[+-])(?P<offset_hour>[0-9]{2}):(?P<offset_minute>[0-9]{2}))$"
)
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("acceptora_token", re.compile(r"avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai_or_anthropic_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("stripe_secret_key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE)),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\s*[:=]\s*[\"']?([^\s\"']{8,})",
            re.IGNORECASE,
        ),
    ),
)
SAFE_CREDENTIAL_KEYS = {
    "idempotency_key",
    "pattern_key",
    "previous_semantic_key",
    "section_semantic_key",
    "semantic_key",
}
SENSITIVE_CREDENTIAL_KEYS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passphrase",
    "private_key",
    "secret",
}
SYMBOLIC_CREDENTIAL_VALUE = re.compile(
    r"^(?:Bearer\s+)?\$\{[A-Z_][A-Z0-9_]*\}$|^\[(?:REDACTED|MASKED|NOT SET)\]$|^<(?:REDACTED|MASKED|NOT SET)>$",
    re.IGNORECASE,
)
ROOT_PROPERTIES = {
    "feature_id",
    "base_checklist_revision",
    "verification_instruction_context",
    "source_descriptor",
    "source_digest",
    "source_manifest",
    "implementation_change_summary",
    "intent_summary",
    "scope_summary",
    "expected_outcome",
    "preconditions",
    "automated_evidence",
    "known_limits",
    "sections",
    "items",
    "addressed_resolution_ids",
    "versions",
    "idempotency_key",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _secret_kinds(value: str) -> list[str]:
    return [
        kind
        for kind, pattern in SECRET_PATTERNS
        if pattern.search(value) is not None
    ]


def _normalized_key_name(value: str) -> str:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value.strip())
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")


def _is_sensitive_lineage_uri_parameter_key(value: str) -> bool:
    normalized = _normalized_key_name(unquote(value))
    compact = normalized.replace("_", "")
    return (
        normalized in SENSITIVE_LINEAGE_URI_KEYS
        or compact in SENSITIVE_LINEAGE_URI_COMPACT_KEYS
        or SENSITIVE_LINEAGE_URI_KEY_SEGMENT.search(normalized) is not None
        or normalized in SENSITIVE_QUERY_KEYS
        or compact in SENSITIVE_QUERY_COMPACT_KEYS
    )


def _is_sensitive_credential_key(value: str) -> bool:
    normalized = _normalized_key_name(value)
    if normalized in SAFE_CREDENTIAL_KEYS:
        return False
    return (
        normalized in SENSITIVE_CREDENTIAL_KEYS
        or normalized.endswith("_password")
        or normalized.endswith("_passphrase")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
        or normalized.endswith("_key")
        or normalized.endswith("_authorization")
        or normalized.endswith("_cookie")
        or normalized.endswith("_credential")
        or normalized.endswith("_credentials")
    )


def _is_empty_or_symbolic_credential_value(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    return isinstance(value, str) and SYMBOLIC_CREDENTIAL_VALUE.fullmatch(value.strip()) is not None


def find_secret_paths(value: Any, path: str = "$") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_kinds = _secret_kinds(key) if isinstance(key, str) else []
            if (
                isinstance(key, str)
                and _is_sensitive_credential_key(key)
                and not _is_empty_or_symbolic_credential_value(child)
            ):
                key_kinds.append("credential_field")
            findings.extend({"path": f"{path}.[object-key]", "kind": kind} for kind in key_kinds)
            child_path = f"{path}.[redacted-key]" if key_kinds else f"{path}.{key}"
            findings.extend(find_secret_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_secret_paths(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        findings.extend({"path": path, "kind": kind} for kind in _secret_kinds(value))
    return findings


def _error(errors: list[dict[str, str]], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _property_path(path: str, property_name: Any) -> str:
    if not isinstance(property_name, str) or _secret_kinds(property_name):
        return f"{path}.[redacted-key]"
    return f"{path}.{property_name}"


def _reject_additional_properties(
    value: dict[str, Any], allowed: set[str], path: str, errors: list[dict[str, str]]
) -> None:
    for property_name in sorted(set(value) - allowed, key=str):
        _error(
            errors,
            _property_path(path, property_name),
            "ADDITIONAL_PROPERTY",
            "field is not part of reconciliation-request v1",
        )


def _required_string(
    value: dict[str, Any],
    key: str,
    path: str,
    errors: list[dict[str, str]],
    minimum: int = 1,
    maximum: int | None = None,
) -> str | None:
    candidate = value.get(key)
    if not isinstance(candidate, str) or len(candidate) < minimum:
        _error(errors, f"{path}.{key}", "REQUIRED_STRING", f"{key} must be a string with at least {minimum} character(s)")
        return None
    if maximum is not None and len(candidate) > maximum:
        _error(errors, f"{path}.{key}", "MAX_LENGTH", f"{key} must contain at most {maximum} characters")
    return candidate


def _optional_nullable_string(
    value: dict[str, Any], key: str, path: str, errors: list[dict[str, str]], maximum: int
) -> str | None:
    if key not in value or value[key] is None:
        return None
    candidate = value[key]
    if not isinstance(candidate, str):
        _error(errors, f"{path}.{key}", "INVALID_STRING", f"{key} must be a string or null")
        return None
    if len(candidate) > maximum:
        _error(errors, f"{path}.{key}", "MAX_LENGTH", f"{key} must contain at most {maximum} characters")
    return candidate


def _string_list(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
    minimum: int = 0,
    maximum: int | None = None,
    item_minimum: int = 1,
    item_maximum: int | None = None,
) -> list[str]:
    if not isinstance(value, list):
        _error(errors, path, "STRING_LIST", "must be a list of strings")
        return []
    if len(value) < minimum:
        _error(errors, path, "TOO_SHORT", f"must contain at least {minimum} value(s)")
    if maximum is not None and len(value) > maximum:
        _error(errors, path, "MAX_ITEMS", f"must contain at most {maximum} value(s)")
    strings: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or len(item) < item_minimum:
            _error(errors, item_path, "STRING_LIST", f"must be a string with at least {item_minimum} character(s)")
            continue
        if item_maximum is not None and len(item) > item_maximum:
            _error(errors, item_path, "MAX_LENGTH", f"must contain at most {item_maximum} characters")
        strings.append(item)
    return strings


def _validate_metadata(
    value: dict[str, Any], key: str, path: str, errors: list[dict[str, str]], maximum: int
) -> None:
    if key not in value:
        return
    metadata = value[key]
    metadata_path = f"{path}.{key}"
    if not isinstance(metadata, dict):
        _error(errors, metadata_path, "OBJECT_REQUIRED", f"{key} must be an object")
        return
    if len(metadata) > maximum:
        _error(errors, metadata_path, "MAX_PROPERTIES", f"{key} must contain at most {maximum} properties")
    for property_name, candidate in metadata.items():
        valid_primitive = candidate is None or isinstance(candidate, (str, int, float, bool))
        if isinstance(candidate, float) and not isfinite(candidate):
            valid_primitive = False
        if not valid_primitive:
            _error(
                errors,
                _property_path(metadata_path, property_name),
                "INVALID_METADATA_VALUE",
                "metadata values must be JSON scalar values or null",
            )


def _is_rfc3339_date_time(value: str) -> bool:
    match = RFC3339_DATE_TIME.fullmatch(value)
    if match is None:
        return False
    second = int(match["second"])
    if second > 60:
        return False
    try:
        local_time = datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            min(second, 59),
        )
    except ValueError:
        return False
    if match["offset_sign"] is not None:
        offset_hour = int(match["offset_hour"])
        offset_minute = int(match["offset_minute"])
        if offset_hour > 23 or offset_minute > 59:
            return False
        direction = 1 if match["offset_sign"] == "+" else -1
        try:
            local_time -= timedelta(minutes=direction * ((offset_hour * 60) + offset_minute))
        except OverflowError:
            return False
    if second < 60:
        return True
    return local_time.hour == 23 and local_time.minute == 59 and (
        (local_time.month == 6 and local_time.day == 30)
        or (local_time.month == 12 and local_time.day == 31)
    )


def _lineage_date_time(value: Any, path: str, errors: list[dict[str, str]]) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or LINEAGE_DATE_TIME.fullmatch(value) is None or not _is_rfc3339_date_time(value):
        _error(
            errors,
            path,
            "INVALID_LINEAGE_DATE_TIME",
            "lineage timestamps must use canonical RFC 3339 with a timezone and at most millisecond precision",
        )
        return None
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError:
        _error(errors, path, "INVALID_LINEAGE_DATE_TIME", "lineage timestamp is not a valid calendar date-time")
        return None


def _lineage_nullable_string(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        _error(errors, path, "INVALID_LINEAGE_STRING", "value must be a non-empty string or null")
        return None
    if len(value) > maximum:
        _error(errors, path, "MAX_LENGTH", f"value must contain at most {maximum} characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        _error(errors, path, "INVALID_LINEAGE_STRING", "value does not match the published lineage syntax")
    return value


def _validate_lineage_uri(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, str) or not value or len(value) > 2048 or URI_SCHEME.match(value) is None:
        _error(errors, path, "INVALID_URI", "uri must be an absolute URI containing at most 2048 characters")
        return
    if (
        any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
        or MALFORMED_PERCENT_ESCAPE.search(value) is not None
    ):
        _error(errors, path, "INVALID_URI", "uri contains an invalid character or percent escape")
        return
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        _error(errors, path, "INVALID_URI", "uri must be a valid absolute URI")
        return
    if not parsed.scheme or parsed.username is not None or parsed.password is not None:
        _error(errors, path, "INVALID_URI", "uri must be a valid absolute URI")
        return
    if parsed.scheme.lower() in FORBIDDEN_LINEAGE_URI_SCHEMES:
        _error(errors, path, "INVALID_URI", "evidence references must use a non-executable absolute URI scheme")
        return
    for parameter_string in (parsed.query, parsed.fragment):
        for parameter in re.split(r"[?&;]", parameter_string):
            raw_key = parameter.split("=", 1)[0]
            if _is_sensitive_lineage_uri_parameter_key(raw_key):
                _error(errors, path, "SECRET_REJECTED", "uri contains a credential-bearing parameter")
                return
    if parsed.scheme.lower() in {"http", "https"}:
        if not hostname:
            _error(errors, path, "INVALID_URI", "HTTP uri must include a host")
        return
    if not hostname and not parsed.path:
        _error(errors, path, "INVALID_URI", "non-HTTP uri must include a host or path")


def _validate_lineage_reference(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _error(errors, path, "INVALID_LINEAGE_REFERENCE", "reference must be an object or null")
        return
    _reject_additional_properties(value, LINEAGE_REFERENCE_PROPERTIES, path, errors)
    for key in sorted(LINEAGE_REFERENCE_PROPERTIES):
        if key not in value:
            _error(errors, f"{path}.{key}", "REQUIRED_LINEAGE_FIELD", f"{key} is required")
    if "uri" in value:
        _validate_lineage_uri(value["uri"], f"{path}.uri", errors)
    digest = value.get("digest")
    if "digest" in value and (not isinstance(digest, str) or DIGEST.fullmatch(digest) is None):
        _error(errors, f"{path}.digest", "INVALID_DIGEST", "digest must use sha256:<64 hex>")


def _validate_lineage_assertion(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, dict):
        _error(errors, path, "OBJECT_REQUIRED", "assertion must be an object")
        return
    _reject_additional_properties(value, LINEAGE_ASSERTION_PROPERTIES, path, errors)
    for key in sorted(LINEAGE_ASSERTION_PROPERTIES):
        if key not in value:
            _error(errors, f"{path}.{key}", "REQUIRED_LINEAGE_FIELD", f"{key} is required")
    if "identity" in value:
        _lineage_nullable_string(value["identity"], f"{path}.identity", errors, 500)
    details = value.get("details")
    if "details" not in value or details is None:
        return
    if not isinstance(details, dict):
        _error(errors, f"{path}.details", "INVALID_ASSERTION_DETAILS", "details must be an object or null")
        return
    if not details:
        _error(errors, f"{path}.details", "MIN_PROPERTIES", "details must contain at least one property")
    if len(details) > 50:
        _error(errors, f"{path}.details", "MAX_PROPERTIES", "details must contain at most 50 properties")
    for property_name, candidate in details.items():
        if isinstance(property_name, str) and _normalized_key_name(property_name) in FORBIDDEN_LINEAGE_ASSERTION_DETAIL_KEYS:
            _error(
                errors,
                f"{path}.details.[forbidden-key]",
                "SECRET_REJECTED",
                "assertion details must not contain raw request, response, body, payload, or log fields",
            )
        if isinstance(candidate, str) and len(candidate) > 2000:
            _error(
                errors,
                _property_path(f"{path}.details", property_name),
                "MAX_LENGTH",
                "assertion detail strings must contain at most 2000 characters",
            )
        valid = candidate is None or isinstance(candidate, (str, bool))
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            valid = not isinstance(candidate, float) or isfinite(candidate)
        if not valid:
            _error(
                errors,
                _property_path(f"{path}.details", property_name),
                "INVALID_ASSERTION_DETAIL",
                "assertion detail values must be finite JSON scalar values or null",
            )


def _validate_lineage_authentication(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, dict):
        _error(errors, path, "OBJECT_REQUIRED", "authentication must be an object")
        return
    _reject_additional_properties(value, LINEAGE_AUTHENTICATION_PROPERTIES, path, errors)
    for key in sorted(LINEAGE_AUTHENTICATION_PROPERTIES):
        if key not in value:
            _error(errors, f"{path}.{key}", "REQUIRED_LINEAGE_FIELD", f"{key} is required")
    if "mode" in value:
        _lineage_nullable_string(value["mode"], f"{path}.mode", errors, 64, LINEAGE_SLUG)
    outcome = value.get("outcome")
    if "outcome" in value and outcome is not None and outcome not in AUTHENTICATION_OUTCOMES:
        _error(
            errors,
            f"{path}.outcome",
            "INVALID_AUTHENTICATION_OUTCOME",
            f"outcome must be null or one of {sorted(AUTHENTICATION_OUTCOMES)}",
        )
    mode = value.get("mode")
    if "mode" in value and "outcome" in value and ((mode is None) != (outcome is None)):
        _error(
            errors,
            path,
            "INCOMPLETE_AUTHENTICATION",
            "authentication mode and outcome must either both be null or both be present",
        )
    if mode == "none" and outcome != "not_required":
        _error(
            errors,
            f"{path}.outcome",
            "AUTHENTICATION_MISMATCH",
            "authentication outcome must be not_required when mode is none",
        )


def _validate_lineage_cost(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _error(errors, path, "INVALID_LINEAGE_COST", "cost must be an object or null")
        return
    _reject_additional_properties(value, LINEAGE_COST_PROPERTIES, path, errors)
    for key in sorted(LINEAGE_COST_PROPERTIES):
        if key not in value:
            _error(errors, f"{path}.{key}", "REQUIRED_LINEAGE_FIELD", f"{key} is required")
    amount = value.get("amount")
    if "amount" in value and (not isinstance(amount, str) or COST_AMOUNT.fullmatch(amount) is None):
        _error(errors, f"{path}.amount", "INVALID_COST_AMOUNT", "amount must be a canonical non-negative decimal string")
    currency = value.get("currency")
    if "currency" in value and (not isinstance(currency, str) or COST_CURRENCY.fullmatch(currency) is None):
        _error(errors, f"{path}.currency", "INVALID_COST_CURRENCY", "currency must use three uppercase letters")


def _validate_lineage_usage(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, list):
        _error(errors, path, "ARRAY_REQUIRED", "usage must be an array")
        return
    if len(value) > 50:
        _error(errors, path, "MAX_ITEMS", "usage must contain at most 50 values")
    observed_pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _error(errors, item_path, "OBJECT_REQUIRED", "usage entry must be an object")
            continue
        _reject_additional_properties(item, LINEAGE_USAGE_PROPERTIES, item_path, errors)
        for key in sorted(LINEAGE_USAGE_PROPERTIES):
            if key not in item:
                _error(errors, f"{item_path}.{key}", "REQUIRED_LINEAGE_FIELD", f"{key} is required")
        for key in ("metric", "unit"):
            candidate = item.get(key)
            if key in item and (
                not isinstance(candidate, str)
                or not candidate
                or len(candidate) > 64
                or LINEAGE_SLUG.fullmatch(candidate) is None
            ):
                _error(errors, f"{item_path}.{key}", "INVALID_USAGE_LABEL", f"{key} must use the published lineage syntax")
        quantity = item.get("quantity")
        if "quantity" in item and (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or (isinstance(quantity, float) and not isfinite(quantity))
            or quantity < 0
        ):
            _error(errors, f"{item_path}.quantity", "INVALID_USAGE_QUANTITY", "quantity must be a finite non-negative number")
        metric = item.get("metric")
        unit = item.get("unit")
        if isinstance(metric, str) and isinstance(unit, str):
            pair = (metric, unit)
            if pair in observed_pairs:
                _error(
                    errors,
                    item_path,
                    "DUPLICATE_USAGE",
                    "usage metric and unit pairs must be unique",
                )
            observed_pairs.add(pair)


def _validate_lineage(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
    expected_source_revision: str | None,
    evidence_source_revision: Any,
) -> None:
    if not isinstance(value, dict):
        _error(errors, path, "OBJECT_REQUIRED", "lineage must be an object")
        return
    _reject_additional_properties(value, LINEAGE_PROPERTIES, path, errors)
    for key in sorted(LINEAGE_PROPERTIES):
        if key not in value:
            _error(errors, f"{path}.{key}", "REQUIRED_LINEAGE_FIELD", f"{key} is required")

    project_id = value.get("project_id")
    if "project_id" in value and (not isinstance(project_id, str) or PROJECT_ID.fullmatch(project_id) is None):
        _error(errors, f"{path}.project_id", "INVALID_PROJECT_ID", "project_id must be proj_ followed by one ULID")
    provider = value.get("provider")
    if "provider" in value and (
        not isinstance(provider, str)
        or not provider
        or len(provider) > 100
        or LINEAGE_SLUG.fullmatch(provider) is None
    ):
        _error(errors, f"{path}.provider", "INVALID_PROVIDER", "provider must use the published lineage syntax")
    provider_run_id = value.get("provider_run_id")
    if "provider_run_id" in value and (
        not isinstance(provider_run_id, str)
        or not provider_run_id
        or len(provider_run_id) > 255
        or PROVIDER_RUN_ID.fullmatch(provider_run_id) is None
    ):
        _error(errors, f"{path}.provider_run_id", "INVALID_PROVIDER_RUN_ID", "provider_run_id must be a trimmed single-line string")
    if "environment" in value:
        _lineage_nullable_string(value["environment"], f"{path}.environment", errors, 200)

    started_at = _lineage_date_time(value.get("started_at"), f"{path}.started_at", errors) if "started_at" in value else None
    ended_at = _lineage_date_time(value.get("ended_at"), f"{path}.ended_at", errors) if "ended_at" in value else None
    duration_ms = value.get("duration_ms")
    if "duration_ms" in value and duration_ms is not None and (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms < 0
        or duration_ms > MAX_LINEAGE_DURATION_MS
    ):
        _error(
            errors,
            f"{path}.duration_ms",
            "INVALID_DURATION",
            f"duration_ms must be null or an integer from 0 through {MAX_LINEAGE_DURATION_MS}",
        )
        duration_ms = None
    if started_at is not None and ended_at is not None:
        elapsed = ended_at - started_at
        elapsed_ms = (elapsed.days * 86_400_000) + (elapsed.seconds * 1000) + (elapsed.microseconds // 1000)
        if elapsed_ms < 0:
            _error(errors, f"{path}.ended_at", "INVALID_TIME_RANGE", "ended_at must not be before started_at")
        elif duration_ms is not None and duration_ms != elapsed_ms:
            _error(errors, f"{path}.duration_ms", "DURATION_MISMATCH", "duration_ms must equal the timestamp interval")

    if "artifact" in value:
        _validate_lineage_reference(value["artifact"], f"{path}.artifact", errors)
    if "assertion" in value:
        _validate_lineage_assertion(value["assertion"], f"{path}.assertion", errors)
    if "authentication" in value:
        _validate_lineage_authentication(value["authentication"], f"{path}.authentication", errors)
    if "cost" in value:
        _validate_lineage_cost(value["cost"], f"{path}.cost", errors)
    if "usage" in value:
        _validate_lineage_usage(value["usage"], f"{path}.usage", errors)
    if "stop_reason" in value:
        _lineage_nullable_string(value["stop_reason"], f"{path}.stop_reason", errors, 500)
    if "original_payload_reference" in value:
        _validate_lineage_reference(
            value["original_payload_reference"],
            f"{path}.original_payload_reference",
            errors,
        )
    if (
        expected_source_revision is not None
        and isinstance(evidence_source_revision, str)
        and evidence_source_revision != expected_source_revision
    ):
        _error(
            errors,
            f"{path.rsplit('.', 1)[0]}.source_revision",
            "SOURCE_REVISION_MISMATCH",
            "lineage evidence source_revision must match source_descriptor.opaque_revision",
        )


def _validate_anchor(value: Any, path: str, errors: list[dict[str, str]]) -> str | None:
    if not isinstance(value, str) or len(value) < 4 or len(value) > 500 or not ANCHOR.fullmatch(value):
        _error(errors, path, "INVALID_ANCHOR", "anchor must use a supported type prefix and contain at most 500 characters")
        return None
    return value


def _validate_anchor_list(
    value: Any, path: str, errors: list[dict[str, str]], minimum: int = 1, maximum: int = 100
) -> list[str]:
    anchors = _string_list(value, path, errors, minimum, maximum, item_minimum=4, item_maximum=500)
    for index, anchor in enumerate(anchors):
        _validate_anchor(anchor, f"{path}[{index}]", errors)
    if len(anchors) != len(set(anchors)):
        _error(errors, path, "DUPLICATE_ANCHOR", "coverage anchors must be unique")
    return anchors


def _validate_changed_surface_entries(
    value: Any, path: str, errors: list[dict[str, str]], collect_anchors: bool
) -> set[str]:
    anchors: set[str] = set()
    if not isinstance(value, list):
        _error(errors, path, "ARRAY_REQUIRED", f"{path.rsplit('.', 1)[-1]} must be an array")
        return anchors
    if len(value) > 5000:
        _error(errors, path, "MAX_ITEMS", "changed-surface entries must contain at most 5000 values")
    for index, entry in enumerate(value):
        entry_path = f"{path}[{index}]"
        if not isinstance(entry, dict):
            _error(errors, entry_path, "OBJECT_REQUIRED", "manifest entry must be an object")
            continue
        _reject_additional_properties(entry, CHANGED_SURFACE_PROPERTIES, entry_path, errors)
        anchor = _validate_anchor(entry.get("anchor"), f"{entry_path}.anchor", errors)
        if anchor is not None and collect_anchors:
            anchors.add(anchor)

        change_kind = _required_string(entry, "change_kind", entry_path, errors)
        if change_kind is not None and change_kind not in CHANGE_KINDS:
            _error(
                errors,
                f"{entry_path}.change_kind",
                "INVALID_CHANGE_KIND",
                f"change_kind must be one of {sorted(CHANGE_KINDS)}",
            )
        observed_by = _required_string(entry, "observed_by", entry_path, errors)
        if observed_by is not None and observed_by not in OBSERVED_BY_KINDS:
            _error(
                errors,
                f"{entry_path}.observed_by",
                "INVALID_OBSERVED_BY",
                f"observed_by must be one of {sorted(OBSERVED_BY_KINDS)}",
            )

        previous_anchor = entry.get("previous_anchor")
        if "previous_anchor" in entry and previous_anchor is not None:
            _validate_anchor(previous_anchor, f"{entry_path}.previous_anchor", errors)
        content_digest = entry.get("content_digest")
        if "content_digest" in entry and content_digest is not None:
            if not isinstance(content_digest, str) or not DIGEST.fullmatch(content_digest):
                _error(
                    errors,
                    f"{entry_path}.content_digest",
                    "INVALID_DIGEST",
                    "content_digest must be null or sha256:<64 hex>",
                )
        _validate_metadata(entry, "metadata", entry_path, errors, 20)
    return anchors


def _validate_source(payload: dict[str, Any], errors: list[dict[str, str]]) -> set[str]:
    descriptor = payload.get("source_descriptor")
    if not isinstance(descriptor, dict):
        _error(errors, "$.source_descriptor", "OBJECT_REQUIRED", "source_descriptor must be an object")
    else:
        _reject_additional_properties(descriptor, SOURCE_DESCRIPTOR_PROPERTIES, "$.source_descriptor", errors)
        source_kind = _required_string(descriptor, "source_kind", "$.source_descriptor", errors)
        if source_kind is not None and source_kind not in SOURCE_KINDS:
            _error(
                errors,
                "$.source_descriptor.source_kind",
                "INVALID_SOURCE_KIND",
                f"source_kind must be one of {sorted(SOURCE_KINDS)}",
            )
        _required_string(descriptor, "source_locator", "$.source_descriptor", errors, maximum=500)
        _required_string(descriptor, "opaque_revision", "$.source_descriptor", errors, maximum=500)
        _required_string(descriptor, "adapter_kind", "$.source_descriptor", errors, maximum=100)
        adapter_version = _required_string(descriptor, "adapter_version", "$.source_descriptor", errors)
        if adapter_version is not None and not SEMANTIC_VERSION.fullmatch(adapter_version):
            _error(
                errors,
                "$.source_descriptor.adapter_version",
                "INVALID_VERSION",
                "adapter_version must use semantic version syntax",
            )
        _optional_nullable_string(descriptor, "base_revision", "$.source_descriptor", errors, 500)
        _validate_metadata(descriptor, "metadata", "$.source_descriptor", errors, 50)

    source_digest = _required_string(payload, "source_digest", "$", errors)
    if source_digest is not None and not DIGEST.fullmatch(source_digest):
        _error(errors, "$.source_digest", "INVALID_DIGEST", "source_digest must use sha256:<64 hex>")

    manifest = payload.get("source_manifest")
    if not isinstance(manifest, dict):
        _error(errors, "$.source_manifest", "MANIFEST_REQUIRED", "source_manifest must be an object")
        return set()
    _reject_additional_properties(manifest, SOURCE_MANIFEST_PROPERTIES, "$.source_manifest", errors)
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        _error(errors, "$.source_manifest.schema_version", "INVALID_SCHEMA_VERSION", "source manifest version must be 1")
    current_digest = manifest.get("current_digest")
    if not isinstance(current_digest, str) or not DIGEST.fullmatch(current_digest):
        _error(errors, "$.source_manifest.current_digest", "INVALID_DIGEST", "current_digest must use sha256:<64 hex>")
    elif source_digest is not None and current_digest != source_digest:
        _error(errors, "$.source_manifest.current_digest", "SOURCE_DIGEST_MISMATCH", "current_digest must match source_digest")
    base_digest = manifest.get("base_digest")
    if "base_digest" not in manifest or (
        base_digest is not None and (not isinstance(base_digest, str) or not DIGEST.fullmatch(base_digest))
    ):
        _error(errors, "$.source_manifest.base_digest", "INVALID_DIGEST", "base_digest must be null or sha256:<64 hex>")

    anchors = _validate_changed_surface_entries(
        manifest.get("entries"), "$.source_manifest.entries", errors, collect_anchors=True
    )
    if "ignored_entries" in manifest:
        _validate_changed_surface_entries(
            manifest["ignored_entries"], "$.source_manifest.ignored_entries", errors, collect_anchors=False
        )
    return anchors


def _validate_automated_evidence(
    value: Any,
    errors: list[dict[str, str]],
    expected_source_revision: str | None,
) -> None:
    if not isinstance(value, list):
        _error(errors, "$.automated_evidence", "ARRAY_REQUIRED", "automated_evidence must be an array")
        return
    if len(value) > 200:
        _error(errors, "$.automated_evidence", "MAX_ITEMS", "automated_evidence must contain at most 200 values")

    evidence_keys: set[str] = set()
    for index, item in enumerate(value):
        path = f"$.automated_evidence[{index}]"
        if not isinstance(item, dict):
            _error(errors, path, "OBJECT_REQUIRED", "automated evidence must be an object")
            continue

        _reject_additional_properties(item, EVIDENCE_PROPERTIES, path, errors)

        semantic_key = _required_string(item, "semantic_key", path, errors)
        if semantic_key:
            if not SEMANTIC_KEY.fullmatch(semantic_key):
                _error(errors, f"{path}.semantic_key", "INVALID_SEMANTIC_KEY", "use a stable lowercase key")
            if semantic_key in evidence_keys:
                _error(errors, f"{path}.semantic_key", "DUPLICATE_EVIDENCE_KEY", "evidence keys must be unique")
            evidence_keys.add(semantic_key)

        _required_string(item, "name", path, errors, maximum=200)
        _required_string(item, "target", path, errors, maximum=500)
        executed_at = _required_string(item, "executed_at", path, errors)
        if executed_at is not None and not _is_rfc3339_date_time(executed_at):
            _error(
                errors,
                f"{path}.executed_at",
                "INVALID_DATE_TIME",
                "executed_at must be a valid RFC 3339 date-time with a timezone",
            )
        _required_string(item, "summary", path, errors, maximum=2000)
        _required_string(item, "source_revision", path, errors, maximum=500)

        kind = _required_string(item, "kind", path, errors)
        if kind and kind not in EVIDENCE_KINDS:
            _error(errors, f"{path}.kind", "INVALID_EVIDENCE_KIND", f"kind must be one of {sorted(EVIDENCE_KINDS)}")

        outcome = _required_string(item, "outcome", path, errors)
        if outcome and outcome not in EVIDENCE_OUTCOMES:
            _error(
                errors,
                f"{path}.outcome",
                "INVALID_EVIDENCE_OUTCOME",
                f"outcome must be one of {sorted(EVIDENCE_OUTCOMES)}",
            )

        exit_status = item.get("exit_status")
        if "exit_status" in item and exit_status is not None and (
            isinstance(exit_status, bool) or not isinstance(exit_status, int)
        ):
            _error(errors, f"{path}.exit_status", "INVALID_EXIT_STATUS", "exit_status must be an integer or null")

        redacted_excerpt = item.get("redacted_excerpt")
        if "redacted_excerpt" in item:
            if not isinstance(redacted_excerpt, str):
                _error(errors, f"{path}.redacted_excerpt", "INVALID_REDACTED_EXCERPT", "redacted_excerpt must be a string")
            elif len(redacted_excerpt) > 4000:
                _error(
                    errors,
                    f"{path}.redacted_excerpt",
                    "MAX_LENGTH",
                    "redacted_excerpt must contain at most 4000 characters",
                )

        sufficiency = item.get("evidence_sufficiency")
        if "evidence_sufficiency" in item:
            if not isinstance(sufficiency, str) or sufficiency not in EVIDENCE_SUFFICIENCIES:
                _error(
                    errors,
                    f"{path}.evidence_sufficiency",
                    "INVALID_EVIDENCE_SUFFICIENCY",
                    f"evidence_sufficiency must be one of {sorted(EVIDENCE_SUFFICIENCIES)}",
                )
            elif outcome == "not_run" and sufficiency != "insufficient":
                _error(
                    errors,
                    f"{path}.evidence_sufficiency",
                    "INVALID_EVIDENCE_SUFFICIENCY",
                    "not_run evidence cannot be marked sufficient",
                )

        blocker_reason = item.get("blocker_reason")
        if "blocker_reason" in item:
            if not isinstance(blocker_reason, str) or blocker_reason not in EVIDENCE_BLOCKER_REASONS:
                _error(
                    errors,
                    f"{path}.blocker_reason",
                    "INVALID_BLOCKER_REASON",
                    f"blocker_reason must be one of {sorted(EVIDENCE_BLOCKER_REASONS)}",
                )
            elif outcome != "not_run":
                _error(
                    errors,
                    f"{path}.blocker_reason",
                    "INVALID_BLOCKER_REASON_OUTCOME",
                    "blocker_reason is permitted only when outcome is not_run",
                )
            if outcome != "not_run" and sufficiency == "sufficient":
                _error(
                    errors,
                    f"{path}.evidence_sufficiency",
                    "INVALID_EVIDENCE_SUFFICIENCY",
                    "evidence with blocker_reason cannot be marked sufficient",
                )

        if "lineage" in item:
            _validate_lineage(
                item["lineage"],
                f"{path}.lineage",
                errors,
                expected_source_revision,
                item.get("source_revision"),
            )


def _validate_sections(value: Any, errors: list[dict[str, str]]) -> set[str]:
    section_keys: set[str] = set()
    if not isinstance(value, list):
        _error(errors, "$.sections", "SECTIONS_REQUIRED", "sections must contain at least one section")
        return section_keys
    if not value:
        _error(errors, "$.sections", "SECTIONS_REQUIRED", "sections must contain at least one section")
    if len(value) > 100:
        _error(errors, "$.sections", "MAX_ITEMS", "sections must contain at most 100 values")
    for index, section in enumerate(value):
        path = f"$.sections[{index}]"
        if not isinstance(section, dict):
            _error(errors, path, "OBJECT_REQUIRED", "section must be an object")
            continue
        _reject_additional_properties(section, SECTION_PROPERTIES, path, errors)
        semantic_key = _required_string(section, "semantic_key", path, errors)
        if semantic_key:
            if not SEMANTIC_KEY.fullmatch(semantic_key):
                _error(errors, f"{path}.semantic_key", "INVALID_SEMANTIC_KEY", "use a stable lowercase key")
            if semantic_key in section_keys:
                _error(errors, f"{path}.semantic_key", "DUPLICATE_SECTION_KEY", "section keys must be unique")
            section_keys.add(semantic_key)
        _required_string(section, "title", path, errors, maximum=200)
        _required_string(section, "description", path, errors, minimum=0, maximum=2000)
        pattern_key = _required_string(section, "pattern_key", path, errors)
        if pattern_key is not None and not SEMANTIC_KEY.fullmatch(pattern_key):
            _error(errors, f"{path}.pattern_key", "INVALID_SEMANTIC_KEY", "pattern_key must be a stable lowercase key")
        display_order = section.get("display_order")
        if (
            isinstance(display_order, bool)
            or not isinstance(display_order, int)
            or display_order < 0
            or display_order > 1000
        ):
            _error(errors, f"{path}.display_order", "INVALID_DISPLAY_ORDER", "display_order must be from 0 through 1000")
        if not isinstance(section.get("collapsed_default"), bool):
            _error(errors, f"{path}.collapsed_default", "BOOLEAN_REQUIRED", "collapsed_default must be boolean")
    return section_keys


def _validate_item(
    item: Any,
    path: str,
    errors: list[dict[str, str]],
    section_keys: set[str],
    seen_keys: set[str],
    seen_ids: set[str],
) -> tuple[set[str], bool]:
    if not isinstance(item, dict):
        _error(errors, path, "OBJECT_REQUIRED", "item must be an object")
        return set(), False
    _reject_additional_properties(item, ITEM_PROPERTIES, path, errors)
    operation = _required_string(item, "operation", path, errors)
    if operation and operation not in OPERATIONS:
        _error(errors, f"{path}.operation", "INVALID_OPERATION", f"operation must be one of {sorted(OPERATIONS)}")
    item_id = item.get("item_id")
    item_revision_id = item.get("item_revision_id")
    if "item_id" not in item:
        _error(errors, f"{path}.item_id", "REQUIRED_PROPERTY", "item_id is required")
    if "item_revision_id" not in item:
        _error(errors, f"{path}.item_revision_id", "REQUIRED_PROPERTY", "item_revision_id is required")
    if operation == "add":
        if item_id is not None or item_revision_id is not None:
            _error(errors, f"{path}.item_id", "NEW_ITEM_ID", "add must use null item_id and item_revision_id")
    elif operation:
        if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
            _error(errors, f"{path}.item_id", "EXISTING_ITEM_ID", "existing operations require a valid item_id")
        if not isinstance(item_revision_id, str) or not ITEM_REVISION_ID.fullmatch(item_revision_id):
            _error(errors, f"{path}.item_revision_id", "EXISTING_ITEM_REVISION_ID", "existing operations require a valid item_revision_id")
    if isinstance(item_id, str):
        if item_id in seen_ids:
            _error(errors, f"{path}.item_id", "DUPLICATE_ITEM_ID", "item_id appears more than once")
        seen_ids.add(item_id)

    semantic_key = _required_string(item, "semantic_key", path, errors)
    if semantic_key:
        if not SEMANTIC_KEY.fullmatch(semantic_key):
            _error(errors, f"{path}.semantic_key", "INVALID_SEMANTIC_KEY", "use a stable lowercase key")
        if semantic_key in seen_keys:
            _error(errors, f"{path}.semantic_key", "DUPLICATE_SEMANTIC_KEY", "item keys must be unique")
        seen_keys.add(semantic_key)
    section_key = _required_string(item, "section_semantic_key", path, errors)
    if section_key and section_key not in section_keys:
        _error(errors, f"{path}.section_semantic_key", "UNKNOWN_SECTION", "item section must exist in sections")
    display_order = item.get("display_order")
    if (
        isinstance(display_order, bool)
        or not isinstance(display_order, int)
        or display_order < 0
        or display_order > 1000
    ):
        _error(errors, f"{path}.display_order", "INVALID_DISPLAY_ORDER", "display_order must be from 0 through 1000")

    _required_string(item, "operation_reason", path, errors, maximum=2000)
    _required_string(item, "title", path, errors, maximum=300)
    _required_string(item, "action", path, errors, maximum=5000)
    _required_string(item, "expected_result", path, errors, maximum=5000)
    _required_string(item, "why_it_matters", path, errors, maximum=3000)
    _string_list(item.get("preconditions"), f"{path}.preconditions", errors, maximum=50, item_maximum=1000)
    _string_list(
        item.get("source_references"),
        f"{path}.source_references",
        errors,
        maximum=100,
        item_minimum=0,
        item_maximum=1000,
    )
    if "test_data" in item:
        _string_list(
            item.get("test_data"),
            f"{path}.test_data",
            errors,
            maximum=50,
            item_minimum=0,
            item_maximum=1000,
        )
    anchors = set(_validate_anchor_list(item.get("coverage_anchors"), f"{path}.coverage_anchors", errors, maximum=100))
    if any(anchor.startswith("global:") for anchor in anchors):
        _required_string(item, "global_anchor_reason", path, errors, maximum=1000)
    else:
        _optional_nullable_string(item, "global_anchor_reason", path, errors, 1000)
    _optional_nullable_string(item, "environment", path, errors, 500)
    _optional_nullable_string(item, "target", path, errors, 1000)
    _optional_nullable_string(item, "side_effect_warning", path, errors, 2000)
    _optional_nullable_string(item, "invalidation_reason", path, errors, 2000)
    risk = _required_string(item, "risk", path, errors)
    if risk and risk not in RISKS:
        _error(errors, f"{path}.risk", "INVALID_RISK", f"risk must be one of {sorted(RISKS)}")
    if not isinstance(item.get("required"), bool):
        _error(errors, f"{path}.required", "BOOLEAN_REQUIRED", "required must be boolean")
    if not isinstance(item.get("production_impact"), bool):
        _error(errors, f"{path}.production_impact", "BOOLEAN_REQUIRED", "production_impact must be boolean")
    estimate = item.get("estimated_seconds")
    if "estimated_seconds" in item and (
        isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0 or estimate > 86400
    ):
        _error(errors, f"{path}.estimated_seconds", "INVALID_ESTIMATE", "estimated_seconds must be from 0 through 86400")
    previous = item.get("previous_semantic_key")
    if "previous_semantic_key" in item and previous is not None:
        if not isinstance(previous, str) or not SEMANTIC_KEY.fullmatch(previous):
            _error(errors, f"{path}.previous_semantic_key", "INVALID_SEMANTIC_KEY", "previous key is invalid")
    if operation == "rename_key":
        previous = _required_string(item, "previous_semantic_key", path, errors)
        if previous and not SEMANTIC_KEY.fullmatch(previous):
            _error(errors, f"{path}.previous_semantic_key", "INVALID_SEMANTIC_KEY", "previous key is invalid")
    return anchors, bool(item.get("required"))


def _validate_evidence_reference(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, dict):
        _error(errors, path, "OBJECT_REQUIRED", "evidence reference must be an object")
        return
    _reject_additional_properties(value, EVIDENCE_REFERENCE_PROPERTIES, path, errors)
    kind = _required_string(value, "kind", path, errors)
    if kind is not None and kind not in EVIDENCE_REFERENCE_KINDS:
        _error(
            errors,
            f"{path}.kind",
            "INVALID_EVIDENCE_REFERENCE_KIND",
            f"kind must be one of {sorted(EVIDENCE_REFERENCE_KINDS)}",
        )
    _required_string(value, "value", path, errors, maximum=1000)
    _required_string(value, "summary", path, errors, maximum=1000)


def _validate_limit(limit: Any, path: str, errors: list[dict[str, str]]) -> set[str]:
    if not isinstance(limit, dict):
        _error(errors, path, "OBJECT_REQUIRED", "known limit must be an object")
        return set()
    _reject_additional_properties(limit, KNOWN_LIMIT_PROPERTIES, path, errors)
    semantic_key = _required_string(limit, "semantic_key", path, errors)
    if semantic_key and not SEMANTIC_KEY.fullmatch(semantic_key):
        _error(errors, f"{path}.semantic_key", "INVALID_SEMANTIC_KEY", "use a stable lowercase key")
    _required_string(limit, "description", path, errors, maximum=2000)
    _required_string(limit, "reason", path, errors, maximum=2000)
    _required_string(limit, "mitigation", path, errors, maximum=2000)
    anchors = set(
        _validate_anchor_list(
            limit.get("affected_coverage_anchors"),
            f"{path}.affected_coverage_anchors",
            errors,
            maximum=100,
        )
    )
    severity = _required_string(limit, "severity", path, errors)
    if severity and severity not in RISKS:
        _error(errors, f"{path}.severity", "INVALID_SEVERITY", f"severity must be one of {sorted(RISKS)}")
    if not isinstance(limit.get("blocks_acceptance"), bool):
        _error(errors, f"{path}.blocks_acceptance", "BOOLEAN_REQUIRED", "blocks_acceptance must be boolean")
    status = _required_string(limit, "status", path, errors)
    if status and status not in LIMIT_STATUSES:
        _error(errors, f"{path}.status", "INVALID_STATUS", f"status must be one of {sorted(LIMIT_STATUSES)}")
    if "resolution_evidence" in limit:
        resolution_evidence = limit["resolution_evidence"]
        if not isinstance(resolution_evidence, list):
            _error(errors, f"{path}.resolution_evidence", "ARRAY_REQUIRED", "resolution_evidence must be an array")
        else:
            if len(resolution_evidence) > 50:
                _error(
                    errors,
                    f"{path}.resolution_evidence",
                    "MAX_ITEMS",
                    "resolution_evidence must contain at most 50 values",
                )
            for index, reference in enumerate(resolution_evidence):
                _validate_evidence_reference(reference, f"{path}.resolution_evidence[{index}]", errors)
    if "policy_rule" in limit:
        _required_string(limit, "policy_rule", path, errors, minimum=0, maximum=200)
    if "policy_version" in limit:
        _required_string(limit, "policy_version", path, errors, minimum=0, maximum=50)
    return anchors


def _validate_versions(value: Any, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, dict):
        _error(errors, "$.versions", "OBJECT_REQUIRED", "versions must be an object")
        return
    _reject_additional_properties(value, VERSION_PROPERTIES, "$.versions", errors)
    _required_string(value, "integration_name", "$.versions", errors, maximum=100)
    for key in ("integration_version", "skill_version", "contract_version"):
        version = _required_string(value, key, "$.versions", errors)
        if version and not SEMANTIC_VERSION.fullmatch(version):
            _error(errors, f"$.versions.{key}", "INVALID_VERSION", "version must use semantic version syntax")
    if value.get("contract_version") not in {None, "1.0.0"}:
        _error(errors, "$.versions.contract_version", "CONTRACT_UNSUPPORTED", "contract_version must be 1.0.0")


def _validate_verification_instruction_context(value: Any, errors: list[dict[str, str]]) -> None:
    path = "$.verification_instruction_context"
    if not isinstance(value, dict):
        _error(errors, path, "OBJECT_REQUIRED", "verification_instruction_context must be an object")
        return
    _reject_additional_properties(value, VERIFICATION_INSTRUCTION_CONTEXT_PROPERTIES, path, errors)
    for key in ("account_revision", "project_revision"):
        revision = value.get(key)
        if key not in value:
            _error(errors, f"{path}.{key}", "REQUIRED_PROPERTY", f"{key} is required")
        elif isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            _error(errors, f"{path}.{key}", "INVALID_REVISION", f"{key} must be non-negative")
    digest = value.get("effective_digest")
    if "effective_digest" not in value:
        _error(errors, f"{path}.effective_digest", "REQUIRED_PROPERTY", "effective_digest is required")
    elif not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        _error(
            errors,
            f"{path}.effective_digest",
            "INVALID_DIGEST",
            "effective_digest must use sha256:<64 hex>",
        )


def validate_payload(payload: Any) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return {"valid": False, "errors": [{"path": "$", "code": "OBJECT_REQUIRED", "message": "payload must be an object"}]}

    for finding in find_secret_paths(payload):
        _error(errors, finding["path"], "SECRET_REJECTED", f"possible {finding['kind']} must be removed or redacted")
    _reject_additional_properties(payload, ROOT_PROPERTIES, "$", errors)

    feature_id = _required_string(payload, "feature_id", "$", errors)
    if feature_id and not FEATURE_ID.fullmatch(feature_id):
        _error(errors, "$.feature_id", "INVALID_FEATURE_ID", "feature_id must be feat_ followed by one ULID")
    base_revision = payload.get("base_checklist_revision")
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        _error(errors, "$.base_checklist_revision", "INVALID_REVISION", "base_checklist_revision must be non-negative")
    _validate_verification_instruction_context(payload.get("verification_instruction_context"), errors)
    idempotency_key = _required_string(payload, "idempotency_key", "$", errors)
    if idempotency_key and not UUID_OR_ULID.fullmatch(idempotency_key):
        _error(errors, "$.idempotency_key", "INVALID_IDEMPOTENCY_KEY", "use one UUID or ULID per logical write")

    manifest_anchors = _validate_source(payload, errors)
    for key in ("implementation_change_summary", "intent_summary", "scope_summary", "expected_outcome"):
        _required_string(payload, key, "$", errors, maximum=5000)
    _string_list(payload.get("preconditions"), "$.preconditions", errors, maximum=100, item_maximum=1000)
    source_descriptor = payload.get("source_descriptor")
    expected_source_revision = (
        source_descriptor.get("opaque_revision")
        if isinstance(source_descriptor, dict) and isinstance(source_descriptor.get("opaque_revision"), str)
        else None
    )
    _validate_automated_evidence(payload.get("automated_evidence"), errors, expected_source_revision)

    section_keys = _validate_sections(payload.get("sections"), errors)
    items = payload.get("items")
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    covered_anchors: set[str] = set()
    item_count = 0
    required_count = 0
    if not isinstance(items, list) or not items:
        _error(errors, "$.items", "ITEMS_REQUIRED", "items must contain at least one item")
    else:
        if len(items) > 500:
            _error(errors, "$.items", "MAX_ITEMS", "items must contain at most 500 values")
        for index, item in enumerate(items):
            anchors, required = _validate_item(
                item,
                f"$.items[{index}]",
                errors,
                section_keys,
                seen_keys,
                seen_ids,
            )
            covered_anchors |= anchors
            item_count += 1
            required_count += int(required)

    limits = payload.get("known_limits")
    if not isinstance(limits, list):
        _error(errors, "$.known_limits", "ARRAY_REQUIRED", "known_limits must be an array")
    else:
        if len(limits) > 100:
            _error(errors, "$.known_limits", "MAX_ITEMS", "known_limits must contain at most 100 values")
        limit_keys: set[str] = set()
        for index, limit in enumerate(limits):
            path = f"$.known_limits[{index}]"
            if isinstance(limit, dict) and isinstance(limit.get("semantic_key"), str):
                if limit["semantic_key"] in limit_keys:
                    _error(errors, f"{path}.semantic_key", "DUPLICATE_LIMIT_KEY", "limit keys must be unique")
                limit_keys.add(limit["semantic_key"])
            covered_anchors |= _validate_limit(limit, path, errors)

    for anchor in sorted(manifest_anchors - covered_anchors):
        _error(errors, "$.source_manifest", "UNCOVERED_CHANGED_SURFACE", f"{anchor} is not covered by an item or known limit")

    resolutions = _string_list(
        payload.get("addressed_resolution_ids"),
        "$.addressed_resolution_ids",
        errors,
        maximum=100,
    )
    if len(resolutions) != len(set(resolutions)):
        _error(errors, "$.addressed_resolution_ids", "DUPLICATE_RESOLUTION", "resolution IDs must be unique")
    for index, resolution_id in enumerate(resolutions):
        if not RESOLUTION_ID.fullmatch(resolution_id):
            _error(errors, f"$.addressed_resolution_ids[{index}]", "INVALID_RESOLUTION_ID", "resolution ID is invalid")
    _validate_versions(payload.get("versions"), errors)

    result: dict[str, Any] = {
        "valid": not errors,
        "item_count": item_count,
        "required_item_count": required_count,
        "manifest_anchor_count": len(manifest_anchors),
        "covered_anchor_count": len(covered_anchors),
        "canonical_sha256": hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
    }
    if errors:
        result["errors"] = errors
    return result


def _read_payload(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", nargs="?", default="-", help="JSON file or - for stdin")
    parser.add_argument("--fixture-request", action="store_true", help="validate the request member of a golden fixture")
    parser.add_argument("--pretty", action="store_true", help="pretty-print the validation result")
    arguments = parser.parse_args(argv)
    try:
        payload = _read_payload(arguments.payload)
        if arguments.fixture_request:
            payload = payload.get("request") if isinstance(payload, dict) else None
        result = validate_payload(payload)
    except (OSError, json.JSONDecodeError) as error:
        result = {"valid": False, "errors": [{"path": "$", "code": "INVALID_JSON", "message": str(error)}]}
    if arguments.pretty:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    else:
        sys.stdout.write(canonical_json(result) + "\n")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
