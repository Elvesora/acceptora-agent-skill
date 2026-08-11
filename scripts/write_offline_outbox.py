#!/usr/bin/env python3
"""Write a secret-free, idempotent Agent Verification offline outbox record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_checklist_payload import FEATURE_ID, UUID_OR_ULID, canonical_json, find_secret_paths


OPERATIONS = {"reconcile_checklist", "address_feedback", "record_verification_exception"}


class OutboxError(RuntimeError):
    pass


def _read_payload(path: str) -> dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    if not isinstance(value, dict):
        raise OutboxError("payload must be a JSON object")
    return value


def _read_existing(
    path: Path,
    operation: str,
    feature_id: str,
    idempotency_key: str,
    payload_hash: str,
    completion_gate_hash: str | None,
) -> dict[str, Any]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OutboxError(f"existing outbox record is unreadable: {error}") from error
    existing_payload = existing.get("payload")
    existing_gate = existing.get("completion_gate")
    existing_gate_payload = existing_gate.get("payload") if isinstance(existing_gate, dict) else None
    existing_gate_hash = (
        hashlib.sha256(canonical_json(existing_gate_payload).encode("utf-8")).hexdigest()
        if isinstance(existing_gate_payload, dict)
        else None
    )
    existing_payload_hash = (
        hashlib.sha256(canonical_json(existing_payload).encode("utf-8")).hexdigest()
        if isinstance(existing_payload, dict)
        else None
    )
    if (
        existing.get("operation") != operation
        or existing.get("feature_id") != feature_id
        or existing.get("idempotency_key") != idempotency_key
        or existing.get("status") != "pending"
        or existing_payload_hash != payload_hash
        or existing_gate_hash != completion_gate_hash
    ):
        raise OutboxError("idempotency key already exists with different operation or payload")
    return existing


def write_outbox(
    payload: dict[str, Any],
    output_dir: str | os.PathLike[str],
    operation: str,
    feature_id: str,
    idempotency_key: str,
    source_digest: str | None,
    completion_gate_payload: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], bool]:
    if operation not in OPERATIONS:
        raise OutboxError(f"operation must be one of {sorted(OPERATIONS)}")
    if not FEATURE_ID.match(feature_id):
        raise OutboxError("feature_id must be feat_ followed by one ULID")
    if not UUID_OR_ULID.match(idempotency_key):
        raise OutboxError("idempotency_key must be a UUID or ULID")
    if payload.get("feature_id") != feature_id:
        raise OutboxError("feature_id must exactly match payload.feature_id")
    if payload.get("idempotency_key") != idempotency_key:
        raise OutboxError("idempotency_key must exactly match payload.idempotency_key")

    findings = find_secret_paths(payload)
    if completion_gate_payload is not None:
        findings.extend(find_secret_paths(completion_gate_payload, "$.completion_gate_payload"))
    if findings:
        locations = ", ".join(finding["path"] for finding in findings[:5])
        raise OutboxError(f"SECRET_REJECTED: possible credential at {locations}")

    payload_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    completion_gate_hash = (
        hashlib.sha256(canonical_json(completion_gate_payload).encode("utf-8")).hexdigest()
        if completion_gate_payload is not None
        else None
    )
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{operation}-{idempotency_key}.json"
    if destination.exists():
        return destination, _read_existing(
            destination,
            operation,
            feature_id,
            idempotency_key,
            payload_hash,
            completion_gate_hash,
        ), True

    envelope: dict[str, Any] = {
        "schema_version": "1.1",
        "status": "pending",
        "operation": operation,
        "feature_id": feature_id,
        "idempotency_key": idempotency_key,
        "source_digest": source_digest,
        "canonical_payload_sha256": payload_hash,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "attempt_count": 0,
        "last_error_code": None,
        "last_error_message": None,
        "payload": payload,
    }
    if completion_gate_payload is not None:
        envelope["completion_gate"] = {
            "canonical_payload_sha256": completion_gate_hash,
            "payload": completion_gate_payload,
        }
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    lock_path = destination.with_suffix(destination.suffix + ".lock")
    try:
        with lock_path.open("x", encoding="utf-8") as lock:
            lock.write(f"pid={os.getpid()}\n")
    except FileExistsError:
        if destination.exists():
            return destination, _read_existing(
                destination,
                operation,
                feature_id,
                idempotency_key,
                payload_hash,
                completion_gate_hash,
            ), True
        raise OutboxError(f"outbox record is currently being written: {destination.name}")

    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
        if lock_path.exists():
            lock_path.unlink()
    return destination, envelope, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", nargs="?", default="-", help="JSON request file or - for stdin")
    parser.add_argument("--output-dir", default=".verification/outbox")
    parser.add_argument("--operation", required=True, choices=sorted(OPERATIONS))
    parser.add_argument("--feature-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--source-digest")
    parser.add_argument(
        "--completion-gate-payload",
        help="optional exact completion-gate JSON payload to verify after replaying the MCP write",
    )
    arguments = parser.parse_args(argv)
    try:
        payload = _read_payload(arguments.payload)
        completion_gate_payload = (
            _read_payload(arguments.completion_gate_payload)
            if arguments.completion_gate_payload
            else None
        )
        destination, envelope, replayed = write_outbox(
            payload,
            arguments.output_dir,
            arguments.operation,
            arguments.feature_id,
            arguments.idempotency_key,
            arguments.source_digest or payload.get("source_digest"),
            completion_gate_payload,
        )
        result = {
            "written": not replayed,
            "replayed": replayed,
            "path": str(destination),
            "canonical_payload_sha256": envelope["canonical_payload_sha256"],
        }
        sys.stdout.write(canonical_json(result) + "\n")
        return 0
    except (OutboxError, OSError, json.JSONDecodeError) as error:
        sys.stderr.write(canonical_json({"error": "OUTBOX_WRITE_FAILED", "message": str(error)}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
