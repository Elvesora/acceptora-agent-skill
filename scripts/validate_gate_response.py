#!/usr/bin/env python3
"""Validate and sanitize the Acceptora v1 completion-gate response."""

from __future__ import annotations

import re
from typing import Any

from validate_checklist_payload import DIGEST, FEATURE_ID, find_secret_paths


ALLOWED_OUTCOMES = {"pass", "continue_sync", "not_required", "ambiguous", "unavailable"}
SUCCESS_OUTCOMES = {"pass", "not_required"}
REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]+$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
REQUIRED_FIELDS = {
    "outcome",
    "feature_id",
    "reason_code",
    "reason",
    "last_synchronized_digest",
    "last_synchronized_checklist_revision",
    "recovery_instruction",
    "correlation_id",
}


class GateResponseError(ValueError):
    """Raised when a completion-gate response cannot be trusted."""


def _request_identity(payload: object) -> tuple[str | None, str]:
    if not isinstance(payload, dict):
        raise GateResponseError("Completion-gate request identity is invalid.")

    feature_id = payload.get("feature_id")
    current_digest = payload.get("current_source_digest")
    baseline_digest = payload.get("baseline_source_digest")
    source_manifest = payload.get("source_manifest")
    if (
        (feature_id is not None and (not isinstance(feature_id, str) or FEATURE_ID.fullmatch(feature_id) is None))
        or not isinstance(current_digest, str)
        or DIGEST.fullmatch(current_digest) is None
        or not isinstance(baseline_digest, str)
        or DIGEST.fullmatch(baseline_digest) is None
        or not isinstance(source_manifest, dict)
        or source_manifest.get("base_digest") != baseline_digest
        or source_manifest.get("current_digest") != current_digest
    ):
        raise GateResponseError("Completion-gate request identity is invalid.")
    return feature_id, current_digest


def sanitize_gate_text(value: object, token: str | None, fallback: str, maximum: int = 2000) -> str:
    candidate = str(value)
    if token:
        candidate = candidate.replace(token, "[REDACTED]")
    candidate = CONTROL_CHARACTERS.sub(" ", candidate).strip()[:maximum]
    if not candidate or find_secret_paths({"value": candidate}):
        return fallback
    return candidate


def validate_gate_response(
    response: object,
    request_payload: object,
    *,
    token: str | None,
    expected_feature_id: str | None = None,
    allow_resolved_feature: bool = False,
    expected_checklist_revision: int | None = None,
) -> dict[str, Any]:
    """Return a schema-valid, identity-bound response safe for local output."""

    requested_feature_id, current_digest = _request_identity(request_payload)
    if expected_feature_id is not None and FEATURE_ID.fullmatch(expected_feature_id) is None:
        raise GateResponseError("Expected completion-gate feature identity is invalid.")
    if (
        expected_checklist_revision is not None
        and (
            isinstance(expected_checklist_revision, bool)
            or not isinstance(expected_checklist_revision, int)
            or expected_checklist_revision < 1
        )
    ):
        raise GateResponseError("Expected completion-gate checklist revision is invalid.")

    if not isinstance(response, dict) or set(response) != REQUIRED_FIELDS:
        raise GateResponseError("Completion gate returned an invalid v1 response shape.")

    outcome = response.get("outcome")
    feature_id = response.get("feature_id")
    reason_code = response.get("reason_code")
    reason = response.get("reason")
    synchronized_digest = response.get("last_synchronized_digest")
    synchronized_revision = response.get("last_synchronized_checklist_revision")
    recovery = response.get("recovery_instruction")
    correlation_id = response.get("correlation_id")

    if not isinstance(outcome, str) or outcome not in ALLOWED_OUTCOMES:
        raise GateResponseError("Completion gate returned an invalid outcome.")
    if feature_id is not None and (not isinstance(feature_id, str) or FEATURE_ID.fullmatch(feature_id) is None):
        raise GateResponseError("Completion gate returned an invalid feature identity.")
    if (
        not isinstance(reason_code, str)
        or len(reason_code) > 100
        or REASON_CODE.fullmatch(reason_code) is None
    ):
        raise GateResponseError("Completion gate returned an invalid reason code.")
    if not isinstance(reason, str) or not reason or len(reason) > 2000:
        raise GateResponseError("Completion gate returned an invalid reason.")
    if synchronized_digest is not None and (
        not isinstance(synchronized_digest, str) or DIGEST.fullmatch(synchronized_digest) is None
    ):
        raise GateResponseError("Completion gate returned an invalid synchronized digest.")
    if synchronized_revision is not None and (
        isinstance(synchronized_revision, bool)
        or not isinstance(synchronized_revision, int)
        or synchronized_revision < 1
    ):
        raise GateResponseError("Completion gate returned an invalid synchronized checklist revision.")
    if recovery is not None and (not isinstance(recovery, str) or len(recovery) > 2000):
        raise GateResponseError("Completion gate returned an invalid recovery instruction.")
    if not isinstance(correlation_id, str) or not correlation_id or len(correlation_id) > 200:
        raise GateResponseError("Completion gate returned an invalid correlation identifier.")

    bound_feature_id = expected_feature_id if expected_feature_id is not None else requested_feature_id
    if bound_feature_id is not None:
        if feature_id != bound_feature_id:
            raise GateResponseError("Completion gate returned a different feature identity.")
    elif not allow_resolved_feature and feature_id is not None:
        raise GateResponseError("Completion gate returned an unexpected feature identity.")

    if outcome in SUCCESS_OUTCOMES:
        if synchronized_digest != current_digest:
            raise GateResponseError("Completion gate did not acknowledge the current source digest.")
        if synchronized_revision is None:
            raise GateResponseError("Completion gate omitted the synchronized checklist revision.")
        if expected_checklist_revision is not None and synchronized_revision != expected_checklist_revision:
            raise GateResponseError("Completion gate acknowledged a different checklist revision.")
        if outcome == "pass" and feature_id is None:
            raise GateResponseError("Completion gate pass omitted the synchronized feature identity.")

    sanitized = dict(response)
    sanitized["reason"] = sanitize_gate_text(reason, token, "Completion-gate details were redacted.")
    if recovery is not None:
        sanitized["recovery_instruction"] = sanitize_gate_text(
            recovery,
            token,
            "Retry the completion gate using trusted server output.",
        )
    sanitized["correlation_id"] = sanitize_gate_text(correlation_id, token, "redacted", maximum=200)
    return sanitized
