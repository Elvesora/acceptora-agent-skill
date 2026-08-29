#!/usr/bin/env python3
"""Validate and print one installer-owned Acceptora instruction snapshot."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
SNAPSHOT_KIND = "acceptora_verification_instruction_snapshot"
INSTRUCTION_FIELDS = (
    "analysis_guidance",
    "manual_verification_guidance",
    "test_data_guidance",
)
INSTRUCTION_SOURCES = {"default", "account", "project"}
PROJECT_ID_PATTERN = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
MAX_INSTRUCTION_CHARACTERS = 12_000
MAX_SNAPSHOT_BYTES = 1_048_576
MAX_REVISION = 9_223_372_036_854_775_807
MAX_CLOCK_SKEW_SECONDS = 60
SNAPSHOT_PROPERTIES = {
    "schema_version",
    "kind",
    "project_id",
    "fetched_at",
    "fetched_at_unix",
    "account_revision",
    "project_revision",
    "effective_digest",
    "configured",
    "instructions",
    "sources",
    "record_sha256",
}


class InstructionSnapshotError(RuntimeError):
    """A safe validation error that never includes instruction bodies."""


def canonical_json_bytes(value: Any) -> bytes:
    """Match the server's sorted, compact, UTF-8 canonical JSON encoding."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # PHP keeps these two JavaScript line terminators escaped unless the
    # JSON_UNESCAPED_LINE_TERMINATORS flag is explicitly enabled.
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return encoded.encode("utf-8")


def sha256_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _normalize_instruction(value: str) -> str | None:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip(" \t\n\r\0\x0b")
    return normalized or None


def _revision(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_REVISION:
        raise InstructionSnapshotError(f"{label} must be a non-negative integer")
    return value


def validate_effective_instructions(value: Any) -> dict[str, Any]:
    """Validate one server-resolved instruction envelope and its digest."""

    expected_properties = {
        "schema_version",
        "account_revision",
        "project_revision",
        "effective_digest",
        "configured",
        "instructions",
        "sources",
    }
    if not isinstance(value, dict) or set(value) != expected_properties:
        raise InstructionSnapshotError("verification_instructions has an invalid field set")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InstructionSnapshotError("verification_instructions uses an unsupported schema version")

    account_revision = _revision(value.get("account_revision"), "account_revision")
    project_revision = _revision(value.get("project_revision"), "project_revision")
    configured = value.get("configured")
    if not isinstance(configured, bool):
        raise InstructionSnapshotError("configured must be boolean")

    instructions = value.get("instructions")
    sources = value.get("sources")
    if not isinstance(instructions, dict) or set(instructions) != set(INSTRUCTION_FIELDS):
        raise InstructionSnapshotError("instructions has an invalid field set")
    if not isinstance(sources, dict) or set(sources) != set(INSTRUCTION_FIELDS):
        raise InstructionSnapshotError("sources has an invalid field set")

    normalized_instructions: dict[str, str | None] = {}
    normalized_sources: dict[str, str] = {}
    for field in INSTRUCTION_FIELDS:
        instruction = instructions[field]
        source = sources[field]
        if instruction is not None:
            if not isinstance(instruction, str):
                raise InstructionSnapshotError(f"instructions.{field} must be a string or null")
            if len(instruction) > MAX_INSTRUCTION_CHARACTERS:
                raise InstructionSnapshotError(f"instructions.{field} exceeds the character limit")
            if _normalize_instruction(instruction) != instruction:
                raise InstructionSnapshotError(f"instructions.{field} is not canonically normalized")
        if not isinstance(source, str) or source not in INSTRUCTION_SOURCES:
            raise InstructionSnapshotError(f"sources.{field} is invalid")
        if (instruction is None) != (source == "default"):
            raise InstructionSnapshotError(f"instructions.{field} and sources.{field} are inconsistent")
        normalized_instructions[field] = instruction
        normalized_sources[field] = source

    if configured is not any(value is not None for value in normalized_instructions.values()):
        raise InstructionSnapshotError("configured does not match the effective instruction values")

    digest_payload = {
        "schema_version": SCHEMA_VERSION,
        "account_revision": account_revision,
        "project_revision": project_revision,
        "instructions": normalized_instructions,
        "sources": normalized_sources,
    }
    expected_digest = sha256_digest(digest_payload)
    effective_digest = value.get("effective_digest")
    if not isinstance(effective_digest, str) or DIGEST_PATTERN.fullmatch(effective_digest) is None:
        raise InstructionSnapshotError("effective_digest is invalid")
    if not hmac.compare_digest(effective_digest, expected_digest):
        raise InstructionSnapshotError("effective_digest does not match the canonical instruction context")

    return {
        **digest_payload,
        "effective_digest": effective_digest,
        "configured": configured,
    }


def build_snapshot_record(
    project_id: str,
    value: Any,
    *,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a checksummed immutable record from a validated server envelope."""

    if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise InstructionSnapshotError("project_id is invalid")
    context = validate_effective_instructions(value)
    observed_at = fetched_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise InstructionSnapshotError("fetched_at must include a timezone")
    observed_at = observed_at.astimezone(timezone.utc)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": SNAPSHOT_KIND,
        "project_id": project_id,
        "fetched_at": observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "fetched_at_unix": int(observed_at.timestamp()),
        "account_revision": context["account_revision"],
        "project_revision": context["project_revision"],
        "effective_digest": context["effective_digest"],
        "configured": context["configured"],
        "instructions": context["instructions"],
        "sources": context["sources"],
    }
    record["record_sha256"] = sha256_digest(record)
    return record


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InstructionSnapshotError("fetched_at must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise InstructionSnapshotError("fetched_at must be a UTC RFC 3339 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise InstructionSnapshotError("fetched_at must be a UTC RFC 3339 timestamp")
    return parsed


def validate_snapshot_record(
    value: Any,
    *,
    expected_project_id: str,
    expected_account_revision: int,
    expected_project_revision: int,
    expected_effective_digest: str,
    max_age_seconds: int,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate record integrity, freshness, identity, and expected revision binding."""

    if not isinstance(value, dict) or set(value) != SNAPSHOT_PROPERTIES:
        raise InstructionSnapshotError("the instruction snapshot has an invalid field set")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != SNAPSHOT_KIND:
        raise InstructionSnapshotError("the instruction snapshot has an unsupported record type")
    if PROJECT_ID_PATTERN.fullmatch(expected_project_id) is None or value.get("project_id") != expected_project_id:
        raise InstructionSnapshotError("the instruction snapshot project does not match")

    claimed_record_digest = value.get("record_sha256")
    unsigned = {key: child for key, child in value.items() if key != "record_sha256"}
    if (
        not isinstance(claimed_record_digest, str)
        or DIGEST_PATTERN.fullmatch(claimed_record_digest) is None
        or not hmac.compare_digest(claimed_record_digest, sha256_digest(unsigned))
    ):
        raise InstructionSnapshotError("the instruction snapshot checksum does not match")

    context = validate_effective_instructions(
        {
            "schema_version": value["schema_version"],
            "account_revision": value["account_revision"],
            "project_revision": value["project_revision"],
            "effective_digest": value["effective_digest"],
            "configured": value["configured"],
            "instructions": value["instructions"],
            "sources": value["sources"],
        }
    )
    expected_account_revision = _revision(expected_account_revision, "expected account revision")
    expected_project_revision = _revision(expected_project_revision, "expected project revision")
    if context["account_revision"] != expected_account_revision:
        raise InstructionSnapshotError("the instruction snapshot account revision is stale")
    if context["project_revision"] != expected_project_revision:
        raise InstructionSnapshotError("the instruction snapshot project revision is stale")
    if (
        DIGEST_PATTERN.fullmatch(expected_effective_digest) is None
        or not hmac.compare_digest(context["effective_digest"], expected_effective_digest)
    ):
        raise InstructionSnapshotError("the instruction snapshot effective digest is stale")

    fetched_at = _parse_timestamp(value.get("fetched_at"))
    fetched_at_unix = value.get("fetched_at_unix")
    if isinstance(fetched_at_unix, bool) or not isinstance(fetched_at_unix, int):
        raise InstructionSnapshotError("fetched_at_unix must be an integer")
    if int(fetched_at.timestamp()) != fetched_at_unix:
        raise InstructionSnapshotError("the instruction snapshot timestamps are inconsistent")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or not 1 <= max_age_seconds <= 3600:
        raise InstructionSnapshotError("max_age_seconds must be from 1 through 3600")
    current_time = int(time.time()) if now is None else now
    if fetched_at_unix > current_time + MAX_CLOCK_SKEW_SECONDS:
        raise InstructionSnapshotError("the instruction snapshot timestamp is in the future")
    if current_time - fetched_at_unix > max_age_seconds:
        raise InstructionSnapshotError("the instruction snapshot is stale")

    return {
        "schema_version": context["schema_version"],
        "project_id": expected_project_id,
        "fetched_at": value["fetched_at"],
        "account_revision": context["account_revision"],
        "project_revision": context["project_revision"],
        "effective_digest": context["effective_digest"],
        "configured": context["configured"],
        "instructions": context["instructions"],
        "sources": context["sources"],
        "authority": "untrusted_owner_guidance",
    }


def _is_linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _stable_snapshot_read(path: Path) -> bytes:
    if not path.is_absolute():
        raise InstructionSnapshotError("the instruction snapshot path must be absolute")
    runtime_root = Path(__file__).resolve().parents[1]
    state_root = runtime_root / "state"
    if _is_linklike(state_root) or not state_root.is_dir():
        raise InstructionSnapshotError("the installer-owned instruction state directory is invalid")
    if _is_linklike(path):
        raise InstructionSnapshotError("the instruction snapshot must not be a symlink or junction")
    try:
        expected_parent = state_root.resolve(strict=True)
        observed_parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise InstructionSnapshotError("the instruction snapshot path is unavailable") from error
    if os.path.normcase(str(observed_parent)) != os.path.normcase(str(expected_parent)):
        raise InstructionSnapshotError("the instruction snapshot is outside installer-owned state")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstructionSnapshotError("the instruction snapshot is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SNAPSHOT_BYTES:
            raise InstructionSnapshotError("the instruction snapshot is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_SNAPSHOT_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SNAPSHOT_BYTES:
                raise InstructionSnapshotError("the instruction snapshot exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise InstructionSnapshotError("the instruction snapshot changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_snapshot(
    path: Path,
    *,
    expected_project_id: str,
    expected_account_revision: int,
    expected_project_revision: int,
    expected_effective_digest: str,
    max_age_seconds: int,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(_stable_snapshot_read(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InstructionSnapshotError("the instruction snapshot is not valid UTF-8 JSON") from None
    return validate_snapshot_record(
        value,
        expected_project_id=expected_project_id,
        expected_account_revision=expected_account_revision,
        expected_project_revision=expected_project_revision,
        expected_effective_digest=expected_effective_digest,
        max_age_seconds=max_age_seconds,
        now=now,
    )


def _non_negative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from None
    if parsed < 0 or parsed > MAX_REVISION:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--account-revision", required=True, type=_non_negative_integer)
    parser.add_argument("--project-revision", required=True, type=_non_negative_integer)
    parser.add_argument("--effective-digest", required=True)
    parser.add_argument("--max-age-seconds", type=_non_negative_integer, default=300)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_args(argv)
        result = read_snapshot(
            Path(arguments.snapshot),
            expected_project_id=arguments.project_id,
            expected_account_revision=arguments.account_revision,
            expected_project_revision=arguments.project_revision,
            expected_effective_digest=arguments.effective_digest,
            max_age_seconds=arguments.max_age_seconds,
        )
        output: dict[str, Any] = {"valid": True, **result}
        exit_code = 0
    except InstructionSnapshotError as error:
        output = {
            "valid": False,
            "error": {
                "code": "INSTRUCTION_SNAPSHOT_INVALID",
                "message": str(error),
            },
        }
        exit_code = 1
    sys.stdout.write(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2 if arguments.pretty else None) + "\n"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
