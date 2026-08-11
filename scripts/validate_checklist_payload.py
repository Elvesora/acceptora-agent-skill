#!/usr/bin/env python3
"""Validate the Acceptora v1 reconcile_checklist payload without third-party dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


OPERATIONS = {"retain", "editorial_update", "material_update", "add", "retire", "reopen", "rename_key"}
RISKS = {"critical", "high", "normal", "low"}
LIMIT_STATUSES = {"open", "resolved", "retired"}
SEMANTIC_KEY = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,199}$")
SEMANTIC_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
ANCHOR = re.compile(r"^(file|route|api|component|config|data|content|global):.+$")
UUID_OR_ULID = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)
FEATURE_ID = re.compile(r"^feat_[0-9A-HJKMNP-TV-Z]{26}$")
ITEM_ID = re.compile(r"^item_[0-9A-HJKMNP-TV-Z]{26}$")
ITEM_REVISION_ID = re.compile(r"^irev_[0-9A-HJKMNP-TV-Z]{26}$")
RESOLUTION_ID = re.compile(r"^resolution_[0-9A-HJKMNP-TV-Z]{26}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
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
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    return re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")


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


def _required_string(
    value: dict[str, Any], key: str, path: str, errors: list[dict[str, str]], minimum: int = 1
) -> str | None:
    candidate = value.get(key)
    if not isinstance(candidate, str) or len(candidate.strip()) < minimum:
        _error(errors, f"{path}.{key}", "REQUIRED_STRING", f"{key} must be a non-empty string")
        return None
    return candidate.strip()


def _string_list(value: Any, path: str, errors: list[dict[str, str]], minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        _error(errors, path, "STRING_LIST", "must be a list of non-empty strings")
        return []
    if len(value) < minimum:
        _error(errors, path, "TOO_SHORT", f"must contain at least {minimum} value(s)")
    return [item.strip() for item in value]


def _validate_anchor_list(value: Any, path: str, errors: list[dict[str, str]], minimum: int = 1) -> list[str]:
    anchors = _string_list(value, path, errors, minimum)
    for index, anchor in enumerate(anchors):
        if not ANCHOR.fullmatch(anchor):
            _error(errors, f"{path}[{index}]", "INVALID_ANCHOR", "anchor must use a supported type prefix")
    if len(anchors) != len(set(anchors)):
        _error(errors, path, "DUPLICATE_ANCHOR", "coverage anchors must be unique")
    return anchors


def _validate_source(payload: dict[str, Any], errors: list[dict[str, str]]) -> set[str]:
    descriptor = payload.get("source_descriptor")
    if not isinstance(descriptor, dict):
        _error(errors, "$.source_descriptor", "OBJECT_REQUIRED", "source_descriptor must be an object")
    else:
        for key in ("source_kind", "source_locator", "opaque_revision", "adapter_kind", "adapter_version"):
            _required_string(descriptor, key, "$.source_descriptor", errors)

    source_digest = _required_string(payload, "source_digest", "$", errors)
    if source_digest and not DIGEST.fullmatch(source_digest):
        _error(errors, "$.source_digest", "INVALID_DIGEST", "source_digest must use sha256:<64 hex>")

    manifest = payload.get("source_manifest")
    anchors: set[str] = set()
    if not isinstance(manifest, dict):
        _error(errors, "$.source_manifest", "MANIFEST_REQUIRED", "source_manifest must be an object")
        return anchors
    if manifest.get("schema_version") != 1:
        _error(errors, "$.source_manifest.schema_version", "INVALID_SCHEMA_VERSION", "source manifest version must be 1")
    current_digest = manifest.get("current_digest")
    if not isinstance(current_digest, str) or not DIGEST.fullmatch(current_digest):
        _error(errors, "$.source_manifest.current_digest", "INVALID_DIGEST", "current_digest must use sha256:<64 hex>")
    elif source_digest and current_digest != source_digest:
        _error(errors, "$.source_manifest.current_digest", "SOURCE_DIGEST_MISMATCH", "current_digest must match source_digest")
    base_digest = manifest.get("base_digest")
    if base_digest is not None and (not isinstance(base_digest, str) or not DIGEST.fullmatch(base_digest)):
        _error(errors, "$.source_manifest.base_digest", "INVALID_DIGEST", "base_digest must be null or sha256:<64 hex>")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        _error(errors, "$.source_manifest.entries", "ARRAY_REQUIRED", "source_manifest.entries must be an array")
        return anchors
    for index, entry in enumerate(entries):
        path = f"$.source_manifest.entries[{index}]"
        if not isinstance(entry, dict):
            _error(errors, path, "OBJECT_REQUIRED", "manifest entry must be an object")
            continue
        anchor = entry.get("anchor")
        if not isinstance(anchor, str) or not ANCHOR.fullmatch(anchor):
            _error(errors, f"{path}.anchor", "INVALID_ANCHOR", "manifest entry needs a supported anchor")
        else:
            anchors.add(anchor)
        for key in ("change_kind", "observed_by"):
            _required_string(entry, key, path, errors)
    return anchors


def _validate_sections(value: Any, errors: list[dict[str, str]]) -> set[str]:
    section_keys: set[str] = set()
    if not isinstance(value, list) or not value:
        _error(errors, "$.sections", "SECTIONS_REQUIRED", "sections must contain at least one section")
        return section_keys
    for index, section in enumerate(value):
        path = f"$.sections[{index}]"
        if not isinstance(section, dict):
            _error(errors, path, "OBJECT_REQUIRED", "section must be an object")
            continue
        semantic_key = _required_string(section, "semantic_key", path, errors)
        if semantic_key:
            if not SEMANTIC_KEY.fullmatch(semantic_key):
                _error(errors, f"{path}.semantic_key", "INVALID_SEMANTIC_KEY", "use a stable lowercase key")
            if semantic_key in section_keys:
                _error(errors, f"{path}.semantic_key", "DUPLICATE_SECTION_KEY", "section keys must be unique")
            section_keys.add(semantic_key)
        for key in ("title", "description", "pattern_key"):
            _required_string(section, key, path, errors)
        display_order = section.get("display_order")
        if isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 0:
            _error(errors, f"{path}.display_order", "INVALID_DISPLAY_ORDER", "display_order must be a non-negative integer")
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
    operation = _required_string(item, "operation", path, errors)
    if operation and operation not in OPERATIONS:
        _error(errors, f"{path}.operation", "INVALID_OPERATION", f"operation must be one of {sorted(OPERATIONS)}")
    item_id = item.get("item_id")
    item_revision_id = item.get("item_revision_id")
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
    if isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 0:
        _error(errors, f"{path}.display_order", "INVALID_DISPLAY_ORDER", "display_order must be a non-negative integer")

    for key in ("operation_reason", "title", "action", "expected_result", "why_it_matters"):
        _required_string(item, key, path, errors)
    _string_list(item.get("preconditions"), f"{path}.preconditions", errors)
    _string_list(item.get("source_references"), f"{path}.source_references", errors)
    if "test_data" in item:
        _string_list(item.get("test_data"), f"{path}.test_data", errors)
    anchors = set(_validate_anchor_list(item.get("coverage_anchors"), f"{path}.coverage_anchors", errors))
    if any(anchor.startswith("global:") for anchor in anchors):
        _required_string(item, "global_anchor_reason", path, errors)
    risk = _required_string(item, "risk", path, errors)
    if risk and risk not in RISKS:
        _error(errors, f"{path}.risk", "INVALID_RISK", f"risk must be one of {sorted(RISKS)}")
    if not isinstance(item.get("required"), bool):
        _error(errors, f"{path}.required", "BOOLEAN_REQUIRED", "required must be boolean")
    if not isinstance(item.get("production_impact"), bool):
        _error(errors, f"{path}.production_impact", "BOOLEAN_REQUIRED", "production_impact must be boolean")
    estimate = item.get("estimated_seconds")
    if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0):
        _error(errors, f"{path}.estimated_seconds", "INVALID_ESTIMATE", "estimated_seconds must be non-negative")
    if operation == "rename_key":
        previous = _required_string(item, "previous_semantic_key", path, errors)
        if previous and not SEMANTIC_KEY.fullmatch(previous):
            _error(errors, f"{path}.previous_semantic_key", "INVALID_SEMANTIC_KEY", "previous key is invalid")
    return anchors, bool(item.get("required"))


def _validate_limit(limit: Any, path: str, errors: list[dict[str, str]]) -> set[str]:
    if not isinstance(limit, dict):
        _error(errors, path, "OBJECT_REQUIRED", "known limit must be an object")
        return set()
    semantic_key = _required_string(limit, "semantic_key", path, errors)
    if semantic_key and not SEMANTIC_KEY.fullmatch(semantic_key):
        _error(errors, f"{path}.semantic_key", "INVALID_SEMANTIC_KEY", "use a stable lowercase key")
    for key in ("description", "reason", "mitigation"):
        _required_string(limit, key, path, errors)
    anchors = set(_validate_anchor_list(limit.get("affected_coverage_anchors"), f"{path}.affected_coverage_anchors", errors))
    severity = _required_string(limit, "severity", path, errors)
    if severity and severity not in RISKS:
        _error(errors, f"{path}.severity", "INVALID_SEVERITY", f"severity must be one of {sorted(RISKS)}")
    if not isinstance(limit.get("blocks_acceptance"), bool):
        _error(errors, f"{path}.blocks_acceptance", "BOOLEAN_REQUIRED", "blocks_acceptance must be boolean")
    status = _required_string(limit, "status", path, errors)
    if status and status not in LIMIT_STATUSES:
        _error(errors, f"{path}.status", "INVALID_STATUS", f"status must be one of {sorted(LIMIT_STATUSES)}")
    return anchors


def _validate_versions(value: Any, errors: list[dict[str, str]]) -> None:
    if not isinstance(value, dict):
        _error(errors, "$.versions", "OBJECT_REQUIRED", "versions must be an object")
        return
    _required_string(value, "integration_name", "$.versions", errors)
    for key in ("integration_version", "skill_version", "contract_version"):
        version = _required_string(value, key, "$.versions", errors)
        if version and not SEMANTIC_VERSION.fullmatch(version):
            _error(errors, f"$.versions.{key}", "INVALID_VERSION", "version must use semantic version syntax")
    if value.get("contract_version") not in {None, "1.0.0"}:
        _error(errors, "$.versions.contract_version", "CONTRACT_UNSUPPORTED", "contract_version must be 1.0.0")


def validate_payload(payload: Any) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return {"valid": False, "errors": [{"path": "$", "code": "OBJECT_REQUIRED", "message": "payload must be an object"}]}

    for finding in find_secret_paths(payload):
        _error(errors, finding["path"], "SECRET_REJECTED", f"possible {finding['kind']} must be removed or redacted")
    for key in sorted(set(payload) - ROOT_PROPERTIES):
        _error(errors, f"$.{key}", "ADDITIONAL_PROPERTY", "field is not part of reconciliation-request v1")

    feature_id = _required_string(payload, "feature_id", "$", errors)
    if feature_id and not FEATURE_ID.fullmatch(feature_id):
        _error(errors, "$.feature_id", "INVALID_FEATURE_ID", "feature_id must be feat_ followed by one ULID")
    base_revision = payload.get("base_checklist_revision")
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        _error(errors, "$.base_checklist_revision", "INVALID_REVISION", "base_checklist_revision must be non-negative")
    idempotency_key = _required_string(payload, "idempotency_key", "$", errors)
    if idempotency_key and not UUID_OR_ULID.fullmatch(idempotency_key):
        _error(errors, "$.idempotency_key", "INVALID_IDEMPOTENCY_KEY", "use one UUID or ULID per logical write")

    manifest_anchors = _validate_source(payload, errors)
    for key in ("implementation_change_summary", "intent_summary", "scope_summary", "expected_outcome"):
        _required_string(payload, key, "$", errors)
    _string_list(payload.get("preconditions"), "$.preconditions", errors)
    evidence = payload.get("automated_evidence")
    if not isinstance(evidence, list):
        _error(errors, "$.automated_evidence", "ARRAY_REQUIRED", "automated_evidence must be an array")
    else:
        evidence_keys: set[str] = set()
        for index, item in enumerate(evidence):
            path = f"$.automated_evidence[{index}]"
            if not isinstance(item, dict):
                _error(errors, path, "OBJECT_REQUIRED", "automated evidence must be an object")
                continue
            key = _required_string(item, "semantic_key", path, errors)
            if key:
                if key in evidence_keys:
                    _error(errors, f"{path}.semantic_key", "DUPLICATE_EVIDENCE_KEY", "evidence keys must be unique")
                evidence_keys.add(key)
            for required in ("kind", "name", "target", "executed_at", "outcome", "summary", "source_revision"):
                _required_string(item, required, path, errors)

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

    resolutions = _string_list(payload.get("addressed_resolution_ids"), "$.addressed_resolution_ids", errors)
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
