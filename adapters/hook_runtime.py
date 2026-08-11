#!/usr/bin/env python3
"""Shared deterministic runtime for Codex, Claude Code, and Gemini CLI adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_source_manifest import (  # noqa: E402
    ManifestError,
    capture_snapshot,
    compare_with_baseline,
    find_repository_root,
)
from validate_gate_response import sanitize_gate_text, validate_gate_response  # noqa: E402


SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _semantic_version_match(value: object) -> re.Match[str] | None:
    if not isinstance(value, str):
        return None
    match = SEMVER_PATTERN.fullmatch(value)
    if match is None:
        return None
    prerelease = match.group(4)
    if prerelease is not None and any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in prerelease.split(".")
    ):
        return None
    return match


def _package_versions() -> tuple[str, str, str]:
    manifest_path = SKILL_ROOT / "config" / "package-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        skill_version = manifest["skill"]["version"]
        integration_version = manifest["integration"]["version"]
        contract_version = manifest["contract"]["version"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("the bundled Acceptora package manifest is invalid") from error

    versions = (skill_version, integration_version, contract_version)
    if not all(_semantic_version_match(version) is not None for version in versions):
        raise RuntimeError("the bundled Acceptora package versions are invalid")
    return versions


SKILL_VERSION, INTEGRATION_VERSION, CONTRACT_VERSION = _package_versions()
SOURCE_ADAPTER_VERSION = "1.0.0"
CONFIG_RELATIVE_PATH = Path(".verification/config.json")
STATE_RELATIVE_PATH = Path(".verification/session-state")
MAX_GATE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RELEASE_MANIFEST_BYTES = 1024 * 1024
MAX_RELEASE_FILE_BYTES = 8 * 1024 * 1024
RELEASE_UPDATE_CACHE_TTL_SECONDS = 300
RELEASE_UPDATE_CACHE_FILENAME = "release-update.json"
SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
SOURCE_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40,64}$")
ARTIFACT_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._+-]+$")
TOKEN_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ACCEPTORA_TOKEN_PATTERN = re.compile(r"^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$")


class HookRuntimeError(RuntimeError):
    pass


class ReleaseUpdateUnavailable(HookRuntimeError):
    pass


class ReleaseUpdateRejected(HookRuntimeError):
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


@dataclass(frozen=True)
class GateDecision:
    outcome: str
    block: bool
    message: str | None = None


def read_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise HookRuntimeError(f"hook stdin is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise HookRuntimeError("hook stdin must be a JSON object")
    return value


def _safe_key(value: Any) -> str:
    text = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)[:120]


def _configured_token_value(config: dict[str, Any]) -> str | None:
    token_env = config.get("token_env", "ACCEPTORA_AGENT_TOKEN")
    if not isinstance(token_env, str) or TOKEN_ENV_PATTERN.fullmatch(token_env) is None:
        return None
    token = os.environ.get(token_env)
    return token if isinstance(token, str) else None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(body, encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _project_root(event: dict[str, Any]) -> Path:
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise HookRuntimeError("hook input does not contain cwd")
    return find_repository_root(cwd)


def _config_path(root: Path) -> Path:
    override = os.environ.get("ACCEPTORA_VERIFICATION_CONFIG")
    return Path(override).expanduser().resolve() if override else root / CONFIG_RELATIVE_PATH


def load_config(root: Path) -> dict[str, Any]:
    path = _config_path(root)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HookRuntimeError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise HookRuntimeError(f"{path} must contain a JSON object")
    return value


def _state_paths(root: Path, event: dict[str, Any]) -> tuple[Path, Path]:
    session = _safe_key(event.get("session_id"))
    directory = root / STATE_RELATIVE_PATH
    return directory / f"{session}.baseline.json", directory / f"{session}.loop.json"


def _pending_path(root: Path) -> Path:
    return root / STATE_RELATIVE_PATH / "pending-sync.json"


def _release_update_cache_path(root: Path) -> Path:
    return root / STATE_RELATIVE_PATH / RELEASE_UPDATE_CACHE_FILENAME


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _record_digest(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return _sha256_bytes(_canonical_json_bytes(payload))


def _semantic_version(value: object) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    match = _semantic_version_match(value)
    if match is None:
        raise ReleaseUpdateRejected("the published release version is invalid")
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))), prerelease


def _compare_semantic_versions(left: str, right: str) -> int:
    left_core, left_prerelease = _semantic_version(left)
    right_core, right_prerelease = _semantic_version(right)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left_prerelease is None or right_prerelease is None:
        if left_prerelease is right_prerelease:
            return 0
        return 1 if left_prerelease is None else -1
    for left_identifier, right_identifier in zip(left_prerelease, right_prerelease):
        if left_identifier == right_identifier:
            continue
        left_numeric = left_identifier.isdigit()
        right_numeric = right_identifier.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_identifier) > int(right_identifier) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_identifier > right_identifier else -1
    if len(left_prerelease) == len(right_prerelease):
        return 0
    return 1 if len(left_prerelease) > len(right_prerelease) else -1


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ReleaseUpdateRejected(f"the published {label} digest is invalid")
    return value


def _read_bounded_response(response: Any, maximum_bytes: int, label: str) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise ReleaseUpdateRejected(f"the published {label} has an invalid Content-Length")
            if parsed_content_length > maximum_bytes:
                raise ReleaseUpdateRejected(f"the published {label} exceeds the size limit")
        except ValueError as error:
            raise ReleaseUpdateRejected(f"the published {label} has an invalid Content-Length") from error
    body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ReleaseUpdateRejected(f"the published {label} exceeds the size limit")
    return body


def _fetch_release_manifest(url: str, timeout_seconds: float) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"verify-generated-work/{SKILL_VERSION}",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if getattr(response, "status", None) != 200:
                raise ReleaseUpdateRejected("the published release manifest returned an invalid status")
            content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ReleaseUpdateRejected("the published release manifest has an invalid content type")
            expected_digest = _required_sha256(
                response.headers.get("X-Acceptora-Artifact-SHA256"),
                "release manifest",
            )
            body = _read_bounded_response(response, MAX_RELEASE_MANIFEST_BYTES, "release manifest")
    except urllib.error.HTTPError as error:
        error.close()
        raise ReleaseUpdateUnavailable("the published release manifest is unavailable") from None
    except (urllib.error.URLError, TimeoutError):
        raise ReleaseUpdateUnavailable("the published release manifest is unavailable") from None

    if _sha256_bytes(body) != expected_digest:
        raise ReleaseUpdateRejected("the published release manifest failed integrity verification")
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseUpdateRejected("the published release manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise ReleaseUpdateRejected("the published release manifest is invalid")
    return manifest, expected_digest


def _validated_release_files(manifest: dict[str, Any]) -> str:
    files = manifest.get("files")
    if manifest.get("archive_prefix") != "verify-generated-work" or not isinstance(files, list) or not files:
        raise ReleaseUpdateRejected("the published release manifest has an invalid file inventory")

    expected_fields = {"path", "archive_path", "size", "mode", "sha256"}
    normalized_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    observed_paths: set[str] = set()
    previous_path: str | None = None
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ReleaseUpdateRejected("the published release manifest has an invalid file inventory")
        path = entry.get("path")
        archive_path = entry.get("archive_path")
        size = entry.get("size")
        mode = entry.get("mode")
        if not isinstance(path, str) or not path:
            raise ReleaseUpdateRejected("the published release manifest has an invalid file path")
        candidate = PurePosixPath(path)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or candidate.as_posix() != path
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or re.match(r"^[A-Za-z]:", path)
            or "\\" in path
            or re.search(r"[\x00-\x1f\x7f]", path)
        ):
            raise ReleaseUpdateRejected("the published release manifest has an invalid file path")
        folded_path = path.casefold()
        if folded_path in seen_paths or (previous_path is not None and path <= previous_path):
            raise ReleaseUpdateRejected("the published release manifest has a duplicate or unsorted file path")
        if archive_path != f"verify-generated-work/{path}":
            raise ReleaseUpdateRejected("the published release manifest has an invalid archive path")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_RELEASE_FILE_BYTES
            or mode != ("0755" if path.endswith(".py") else "0644")
        ):
            raise ReleaseUpdateRejected("the published release manifest has invalid file metadata")
        digest = _required_sha256(entry.get("sha256"), "release file")
        seen_paths.add(folded_path)
        observed_paths.add(path)
        previous_path = path
        normalized_files.append(
            {
                "path": path,
                "archive_path": archive_path,
                "size": size,
                "mode": mode,
                "sha256": digest,
            }
        )

    required_paths = {"SKILL.md", "config/package-manifest.json", "scripts/install.py"}
    if not required_paths.issubset(observed_paths):
        raise ReleaseUpdateRejected("the published release manifest is missing essential package files")

    source_tree_sha256 = _required_sha256(manifest.get("source_tree_sha256"), "source tree")
    if _sha256_bytes(_canonical_json_bytes(normalized_files)) != source_tree_sha256:
        raise ReleaseUpdateRejected("the published release manifest has an invalid source-tree digest")
    return source_tree_sha256


def _validated_release(manifest: dict[str, Any], manifest_sha256: str, client: str) -> dict[str, Any]:
    version = manifest.get("version")
    _semantic_version(version)
    source_commit = manifest.get("source_commit")
    supported_clients = manifest.get("supported_clients")
    artifacts = manifest.get("artifacts")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("name") != "verify-generated-work"
        or manifest.get("source_state") != "clean"
        or not isinstance(source_commit, str)
        or SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None
        or not isinstance(supported_clients, list)
        or any(not isinstance(value, str) for value in supported_clients)
        or client not in supported_clients
        or not isinstance(artifacts, list)
    ):
        raise ReleaseUpdateRejected("the published release manifest is not an eligible clean release")

    source_tree_sha256 = _validated_release_files(manifest)
    expected_zip_name = f"verify-generated-work-{version}.zip"
    artifact_names: set[str] = set()
    expected_zip: dict[str, Any] | None = None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseUpdateRejected("the published release manifest has an invalid artifact")
        filename = artifact.get("filename")
        size = artifact.get("size")
        digest = artifact.get("sha256")
        artifact_format = artifact.get("format")
        if (
            not isinstance(filename, str)
            or ARTIFACT_FILENAME_PATTERN.fullmatch(filename) is None
            or filename in artifact_names
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(artifact_format, str)
        ):
            raise ReleaseUpdateRejected("the published release manifest has an invalid artifact")
        artifact_names.add(filename)
        normalized_digest = _required_sha256(digest, "artifact")
        if filename == expected_zip_name and artifact_format == "zip":
            expected_zip = {
                "filename": filename,
                "format": artifact_format,
                "size": size,
                "sha256": normalized_digest,
            }
    if expected_zip is None:
        raise ReleaseUpdateRejected("the published release manifest is missing the expected ZIP artifact")

    return {
        "version": version,
        "source_commit": source_commit,
        "source_tree_sha256": source_tree_sha256,
        "manifest_sha256": manifest_sha256,
        "bundle": expected_zip,
    }


def _write_release_update_cache(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    complete = {**record, "record_sha256": _record_digest(record)}
    _atomic_json(path, complete)
    return complete


def _load_release_update_cache(
    path: Path,
    *,
    now: int,
    manifest_url: str,
    bundle_url: str,
    installed_source_tree_sha256: str,
    client: str,
) -> dict[str, Any] | None:
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("record_sha256") != _record_digest(record):
        return None
    checked_at = record.get("checked_at_unix")
    if (
        record.get("schema_version") != 1
        or record.get("kind") != "acceptora_release_update_check"
        or not isinstance(checked_at, int)
        or isinstance(checked_at, bool)
        or checked_at > now
        or now - checked_at > RELEASE_UPDATE_CACHE_TTL_SECONDS
        or record.get("release_manifest_url") != manifest_url
        or record.get("release_bundle_url") != bundle_url
        or record.get("client") != client
        or record.get("installed_version") != SKILL_VERSION
        or record.get("installed_source_tree_sha256") != installed_source_tree_sha256
        or record.get("setup_mutations_performed") != 0
        or record.get("cache_written") is not True
        or record.get("auto_apply") is not False
    ):
        return None
    status = record.get("status")
    published = record.get("published")
    if status in {"unavailable", "rejected"}:
        return record if published is None else None
    if status not in {"current", "update_available", "identity_conflict", "published_older"}:
        return None
    if not isinstance(published, dict):
        return None
    bundle = published.get("bundle")
    try:
        published_version = published.get("version")
        _semantic_version(published_version)
        published_source_tree = _required_sha256(published.get("source_tree_sha256"), "source tree")
        _required_sha256(published.get("manifest_sha256"), "release manifest")
        source_commit = published.get("source_commit")
        if not isinstance(source_commit, str) or SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
            return None
        if not isinstance(bundle, dict):
            return None
        bundle_filename = bundle.get("filename")
        bundle_size = bundle.get("size")
        if (
            bundle_filename != f"verify-generated-work-{published_version}.zip"
            or bundle.get("format") != "zip"
            or bundle.get("download_url") != bundle_url
            or not isinstance(bundle_size, int)
            or isinstance(bundle_size, bool)
            or bundle_size <= 0
        ):
            return None
        _required_sha256(bundle.get("sha256"), "artifact")
        comparison = _compare_semantic_versions(str(published_version), SKILL_VERSION)
    except ReleaseUpdateRejected:
        return None
    expected_status = (
        "update_available"
        if comparison > 0
        else "published_older"
        if comparison < 0
        else "identity_conflict"
        if published_source_tree != installed_source_tree_sha256
        else "current"
    )
    if status != expected_status:
        return None
    return record


def _release_update_message(record: dict[str, Any], cache_path: Path) -> str | None:
    status = record.get("status")
    if status in {"current", "unavailable"}:
        return None
    if status == "rejected":
        return (
            "Agent Verification update check warning: published release metadata failed integrity checks; "
            "no update was downloaded and no setup files were changed."
        )
    published = record.get("published")
    if not isinstance(published, dict):
        return "Agent Verification update check warning: published release identity could not be verified; no setup files were changed."
    bundle = published.get("bundle")
    if not isinstance(bundle, dict):
        return "Agent Verification update check warning: published release identity could not be verified; no setup files were changed."
    identity = (
        f"manifest {published.get('manifest_sha256')}; ZIP {bundle.get('sha256')} "
        f"({bundle.get('size')} bytes); source commit {published.get('source_commit')}"
    )
    review = f"Review {cache_path} (record {record.get('record_sha256')})."
    if status == "update_available":
        return (
            f"Acceptora Agent Skill update available: {record.get('installed_version')} -> {published.get('version')}. "
            f"Verified release metadata: {identity}. {review} No bundle was downloaded and no setup files were changed. "
            "Use the pinned URLs in that record to verify and download the new bundle, then follow the new bundle's "
            "root SETUP.md: accept rollback with the old trusted installer before creating a fresh accepted plan with the new installer."
        )
    if status == "identity_conflict":
        return (
            f"Agent Verification update check warning: published version {published.get('version')} reused a different "
            f"release identity ({identity}). {review} No update was downloaded or applied."
        )
    return (
        f"Agent Verification update check warning: published version {published.get('version')} is older than installed "
        f"version {record.get('installed_version')} ({identity}). {review} No update was downloaded or applied."
    )


def _check_release_update(
    config: dict[str, Any],
    cache_path: Path,
    *,
    now: int | None = None,
) -> str | None:
    checked_at = int(time.time()) if now is None else now
    manifest_url = _validate_endpoint(config.get("release_manifest_url"), "release_manifest_url")
    bundle_url = _validate_endpoint(config.get("release_bundle_url"), "release_bundle_url")
    manifest_origin = urlsplit(manifest_url)
    bundle_origin = urlsplit(bundle_url)
    if (manifest_origin.scheme, manifest_origin.netloc) != (bundle_origin.scheme, bundle_origin.netloc):
        raise ReleaseUpdateRejected("the pinned release endpoints do not share one origin")
    installed_source_tree_sha256 = _required_sha256(
        config.get("installed_source_tree_sha256"),
        "installed source tree",
    )
    client = config.get("client")
    if client not in {"codex", "claude-code", "gemini-cli"}:
        raise ReleaseUpdateRejected("the installed client identity is invalid")
    timeout_seconds = max(0.5, min(float(config.get("release_update_timeout_seconds", 3)), 5.0))
    cached = _load_release_update_cache(
        cache_path,
        now=checked_at,
        manifest_url=manifest_url,
        bundle_url=bundle_url,
        installed_source_tree_sha256=installed_source_tree_sha256,
        client=client,
    )
    if cached is not None:
        return _release_update_message(cached, cache_path)

    def cache_result(status: str, published: dict[str, Any] | None) -> str | None:
        record = _write_release_update_cache(
            cache_path,
            {
                "schema_version": 1,
                "kind": "acceptora_release_update_check",
                "checked_at_unix": checked_at,
                "release_manifest_url": manifest_url,
                "release_bundle_url": bundle_url,
                "client": client,
                "installed_version": SKILL_VERSION,
                "installed_source_tree_sha256": installed_source_tree_sha256,
                "status": status,
                "published": published,
                "setup_mutations_performed": 0,
                "cache_written": True,
                "auto_apply": False,
            },
        )
        return _release_update_message(record, cache_path)

    try:
        manifest, manifest_sha256 = _fetch_release_manifest(manifest_url, timeout_seconds)
        published = _validated_release(manifest, manifest_sha256, client)
    except ReleaseUpdateUnavailable:
        return cache_result("unavailable", None)
    except ReleaseUpdateRejected:
        return cache_result("rejected", None)

    published["bundle"] = {**published["bundle"], "download_url": bundle_url}
    comparison = _compare_semantic_versions(str(published["version"]), SKILL_VERSION)
    if comparison > 0:
        status = "update_available"
    elif comparison < 0:
        status = "published_older"
    elif published["source_tree_sha256"] != installed_source_tree_sha256:
        status = "identity_conflict"
    else:
        status = "current"
    return cache_result(status, published)


def check_for_skill_update(event: dict[str, Any]) -> str | None:
    event_name = str(event.get("hook_event_name") or event.get("event_name") or "")
    if event_name != "SessionStart":
        return None
    root = _project_root(event)
    config = load_config(root)
    expected_config_path = (SKILL_ROOT / "config" / "runtime-config.json").resolve()
    if (
        config.get("config_source") != "installer_owned_external_runtime"
        or os.path.normcase(str(_config_path(root).resolve())) != os.path.normcase(str(expected_config_path))
    ):
        return None
    try:
        return _check_release_update(config, _release_update_cache_path(root))
    except ReleaseUpdateRejected:
        return (
            "Agent Verification update check warning: published release metadata failed integrity checks; "
            "no update was downloaded and no setup files were changed."
        )
    except (OSError, TypeError, ValueError):
        return (
            "Agent Verification update check warning: the release check failed safely; "
            "no update was downloaded and no setup files were changed."
        )


def capture_task_baseline(event: dict[str, Any], integration: str) -> Path | None:
    root = _project_root(event)
    config = load_config(root)
    if config.get("enabled", True) is False:
        return None
    adapter = str(config.get("source_adapter", "auto"))
    if adapter not in {"auto", "git", "filesystem"}:
        raise HookRuntimeError("source_adapter must be auto, git, or filesystem")
    ignores = config.get("ignored_paths", [])
    if not isinstance(ignores, list) or any(not isinstance(value, str) for value in ignores):
        raise HookRuntimeError("ignored_paths must be a list of strings")
    baseline_path, loop_path = _state_paths(root, event)
    pending_path = _pending_path(root)
    event_name = str(event.get("hook_event_name") or event.get("event_name") or "")
    baseline_kind = "session" if event_name == "SessionStart" else "prompt"
    if pending_path.exists():
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            snapshot = pending.get("snapshot") if isinstance(pending, dict) else None
        except (OSError, json.JSONDecodeError) as error:
            raise HookRuntimeError(f"cannot read pending synchronization state: {error}") from error
        if not isinstance(snapshot, dict):
            raise HookRuntimeError("pending synchronization state has no baseline snapshot")
        baseline_kind = "prompt"
    elif baseline_path.exists():
        try:
            existing = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HookRuntimeError(f"cannot read existing task baseline: {error}") from error
        existing_kind = existing.get("baseline_kind") if isinstance(existing, dict) else None
        if existing_kind == "prompt" or existing_kind not in {"session", "prompt"}:
            return baseline_path
        if baseline_kind == "session":
            return baseline_path
    else:
        snapshot = capture_snapshot(root, adapter=adapter, extra_ignores=ignores)
    if baseline_path.exists() and baseline_kind == "prompt" and not pending_path.exists():
        snapshot = capture_snapshot(root, adapter=adapter, extra_ignores=ignores)
    _atomic_json(
        baseline_path,
        {
            "schema_version": "1.0",
            "integration": integration,
            "session_id": str(event.get("session_id", "unknown")),
            "turn_id": event.get("turn_id"),
            "baseline_kind": baseline_kind,
            "snapshot": snapshot,
        },
    )
    if loop_path.exists() and not pending_path.exists():
        loop_path.unlink()
    return baseline_path


def _load_baseline(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HookRuntimeError(f"cannot read baseline {path}: {error}") from error
    snapshot = value.get("snapshot") if isinstance(value, dict) else None
    if not isinstance(snapshot, dict):
        raise HookRuntimeError(f"baseline {path} has no snapshot object")
    return snapshot


def _validate_endpoint(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HookRuntimeError(f"{label} is not configured")

    endpoint = value.strip()

    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError as error:
        raise HookRuntimeError(f"{label} is not a valid absolute URL") from error

    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HookRuntimeError(f"{label} must not contain credentials, a query, or a fragment")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HookRuntimeError(f"{label} must be an absolute HTTP or HTTPS URL")
    if parsed.scheme == "http" and not _is_http_loopback_url(endpoint):
        raise HookRuntimeError(f"{label} must use HTTPS unless it targets local loopback")

    return endpoint


def _post_gate(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    gate_url = _validate_endpoint(config.get("completion_gate_url"), "completion_gate_url")
    token = _configured_token_value(config)
    if token is None or ACCEPTORA_TOKEN_PATTERN.fullmatch(token) is None:
        raise HookRuntimeError("configured Acceptora agent token is missing or malformed")

    timeout = float(config.get("timeout_seconds", 8))
    retries = max(1, min(int(config.get("retry_attempts", 2)), 4))
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        gate_url,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"verify-generated-work/{SKILL_VERSION}",
        },
        method="POST",
    )
    opener = _authenticated_opener(gate_url)
    last_failure: str | None = None
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_GATE_RESPONSE_BYTES:
                            raise HookRuntimeError("completion gate response exceeds the 4 MiB limit")
                    except ValueError as error:
                        raise HookRuntimeError("completion gate returned an invalid Content-Length") from error
                body = response.read(MAX_GATE_RESPONSE_BYTES + 1)
                if len(body) > MAX_GATE_RESPONSE_BYTES:
                    raise HookRuntimeError("completion gate response exceeds the 4 MiB limit")
                value = json.loads(body.decode("utf-8"))
                if not isinstance(value, dict):
                    raise HookRuntimeError("completion gate returned a non-object JSON response")
                return value
        except urllib.error.HTTPError as error:
            try:
                # Redirects and authentication, authorization, or contract errors
                # are never followed or retried with the bearer token.
                if error.code < 500 and error.code != 429:
                    raise HookRuntimeError(f"completion gate returned HTTP {error.code}") from error
                last_failure = f"HTTP {error.code}"
            finally:
                error.close()
        except json.JSONDecodeError:
            last_failure = "an invalid JSON response"
        except TimeoutError:
            last_failure = "a request timeout"
        except urllib.error.URLError:
            last_failure = "a network error"
        except OSError:
            last_failure = "a network I/O error"
        if attempt + 1 < retries:
            time.sleep(min(0.25 * (2**attempt), 1.0))
    raise HookRuntimeError(
        f"completion gate unavailable after {retries} attempt(s): {last_failure or 'request failed'}"
    )


def _source_digest(value: Any) -> str:
    digest = str(value or "")
    if re.fullmatch(r"[a-f0-9]{64}", digest):
        return f"sha256:{digest}"
    if re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        return digest
    raise HookRuntimeError("deterministic source adapter returned an invalid SHA-256 digest")


def _source_descriptor(snapshot: dict[str, Any], base_revision: str | None) -> dict[str, Any]:
    adapter = str(snapshot.get("adapter") or "")
    repository = str(snapshot.get("repository") or "")
    source_kind = "git" if adapter.startswith("git-") else "file_manifest"
    opaque_revision = str(snapshot.get("head") or snapshot.get("source_digest") or "")

    if not adapter or not repository or not opaque_revision:
        raise HookRuntimeError("deterministic source descriptor is incomplete")
    if len(adapter) > 100 or len(repository) > 500 or len(opaque_revision) > 500:
        raise HookRuntimeError("deterministic source descriptor exceeds the v1 contract limits")

    metadata = {
        "guarantee": str(snapshot.get("guarantee") or "unknown"),
        "branch": str(snapshot.get("branch") or ""),
    }

    return {
        "source_kind": source_kind,
        "source_locator": repository,
        "opaque_revision": opaque_revision,
        "base_revision": base_revision,
        "adapter_kind": adapter,
        "adapter_version": SOURCE_ADAPTER_VERSION,
        "metadata": metadata,
    }


def build_completion_gate_payload(
    config: dict[str, Any],
    manifest: dict[str, Any],
    event: dict[str, Any],
    integration: str,
) -> dict[str, Any]:
    """Map the deterministic helper output to check_completion_gate input v1."""

    project_id = config.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise HookRuntimeError("project_id is not configured")

    baseline = manifest.get("base")
    current = manifest.get("current")
    entries = manifest.get("entries")
    if not isinstance(baseline, dict) or not isinstance(current, dict) or not isinstance(entries, list):
        raise HookRuntimeError("deterministic changed-surface manifest is incomplete")

    baseline_digest = _source_digest(baseline.get("source_digest"))
    current_digest = _source_digest(current.get("source_digest"))
    baseline_descriptor = _source_descriptor(baseline, None)
    current_descriptor = _source_descriptor(current, baseline_descriptor["opaque_revision"])
    source_entries: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            raise HookRuntimeError("deterministic changed-surface entry is not an object")
        mapped: dict[str, Any] = {
            "anchor": str(entry.get("anchor") or ""),
            "change_kind": str(entry.get("change") or ""),
            "observed_by": "adapter",
            "content_digest": _source_digest(entry["current_sha256"])
            if entry.get("current_sha256") is not None
            else None,
            "metadata": {
                "path": str(entry.get("path") or ""),
                "current_size": int(entry.get("current_size") or 0),
            },
        }
        source_entries.append(mapped)

    source_kind = baseline_descriptor["source_kind"]
    repository = baseline_descriptor["source_locator"]

    return {
        "project_id": project_id,
        "source_identity": f"{source_kind}:{repository}",
        "adapter_kind": current_descriptor["adapter_kind"],
        "adapter_version": SOURCE_ADAPTER_VERSION,
        "baseline_source_descriptor": baseline_descriptor,
        "baseline_source_digest": baseline_digest,
        "current_source_descriptor": current_descriptor,
        "current_source_digest": current_digest,
        "source_manifest": {
            "schema_version": 1,
            "base_digest": baseline_digest,
            "current_digest": current_digest,
            "entries": source_entries,
            "ignored_entries": [],
        },
        "task_session_correlation_id": str(event.get("turn_id") or event.get("session_id") or "unknown"),
        "feature_id": config.get("feature_id"),
        "versions": {
            "integration_name": integration,
            "integration_version": INTEGRATION_VERSION,
            "skill_version": SKILL_VERSION,
            "contract_version": CONTRACT_VERSION,
        },
    }


def _register_loop_attempt(path: Path, manifest_digest: str, outcome: str) -> int:
    count = 0
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior.get("manifest_digest") == manifest_digest and prior.get("outcome") == outcome:
                count = int(prior.get("count", 0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            count = 0
    count += 1
    _atomic_json(path, {"manifest_digest": manifest_digest, "outcome": outcome, "count": count})
    return count


def _cleanup(paths: tuple[Path, ...]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def _cleanup_pending_state(root: Path, pending_path: Path) -> None:
    if not pending_path.exists():
        return
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        raw_origins = pending.get("origin_session_ids", []) if isinstance(pending, dict) else []
        if isinstance(pending, dict) and pending.get("origin_session_id"):
            raw_origins = [*raw_origins, pending["origin_session_id"]]
        origins = {_safe_key(value) for value in raw_origins if value}
    except (OSError, json.JSONDecodeError):
        origins = set()
    directory = root / STATE_RELATIVE_PATH
    for origin_session in origins:
        _cleanup((directory / f"{origin_session}.baseline.json", directory / f"{origin_session}.loop.json"))
    if pending_path.exists():
        pending_path.unlink()


def evaluate_completion_gate(event: dict[str, Any], integration: str) -> GateDecision:
    root = _project_root(event)
    config = load_config(root)
    if config.get("enabled", True) is False:
        return GateDecision("not_required", False)
    baseline_path, loop_path = _state_paths(root, event)
    pending_path = _pending_path(root)
    max_blocks = max(1, min(int(config.get("max_stop_blocks", 2)), 5))
    baseline_for_pending: dict[str, Any] | None = None

    try:
        if not baseline_path.exists():
            raise HookRuntimeError("task-start baseline is missing; verify the task-start hook is installed and trusted")
        baseline = _load_baseline(baseline_path)
        baseline_for_pending = baseline
        ignores = config.get("ignored_paths", [])
        manifest = compare_with_baseline(baseline, root, extra_ignores=ignores)
        if not manifest["entries"] and not pending_path.exists():
            confirmed = compare_with_baseline(baseline, root, extra_ignores=ignores)
            if confirmed["current"]["source_digest"] != manifest["current"]["source_digest"]:
                raise HookRuntimeError("source changed while confirming the completion boundary")
            _cleanup((baseline_path, loop_path))
            return GateDecision("not_required", False)

        payload = build_completion_gate_payload(config, manifest, event, integration)
        response = validate_gate_response(
            _post_gate(config, payload),
            payload,
            token=_configured_token_value(config),
            expected_feature_id=payload.get("feature_id"),
            allow_resolved_feature=payload.get("feature_id") is None,
        )
        outcome = response["outcome"]
        if outcome in {"pass", "not_required"}:
            confirmed = compare_with_baseline(baseline, root, extra_ignores=ignores)
            if confirmed["current"]["source_digest"] != manifest["current"]["source_digest"]:
                raise HookRuntimeError("source changed during the completion gate request")
            _cleanup((baseline_path, loop_path))
            _cleanup_pending_state(root, pending_path)
            return GateDecision(outcome, False)

        reason = response["reason"]
        recovery = response["recovery_instruction"] or "Use $verify-generated-work and run the completion gate again."
        feature = response.get("feature_id")
        feature_note = f" Feature: {feature}." if feature else ""
        message = f"Agent Verification gate: {outcome}. {reason}{feature_note} {recovery}"
        manifest_digest = str(manifest["changed_surface_digest"])
    except (HookRuntimeError, ManifestError, OSError, ValueError, TypeError) as error:
        outcome = "unavailable"
        manifest_digest = "unknown"
        safe_error = sanitize_gate_text(
            error,
            _configured_token_value(config),
            "Completion-gate failure details were redacted.",
        )
        message = (
            f"Agent Verification gate unavailable: {safe_error}. Retry synchronization, then write a secret-free recovery "
            f"record under {config.get('offline_outbox', '.verification/outbox')} and report the sync failure visibly."
        )

    if baseline_for_pending is not None:
        origins = {_safe_key(event.get("session_id"))}
        if pending_path.exists():
            try:
                prior_pending = json.loads(pending_path.read_text(encoding="utf-8"))
                origins.update(
                    _safe_key(value)
                    for value in prior_pending.get("origin_session_ids", [])
                    if value
                )
                if prior_pending.get("origin_session_id"):
                    origins.add(_safe_key(prior_pending["origin_session_id"]))
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        _atomic_json(
            pending_path,
            {
                "schema_version": "1.0",
                "origin_session_ids": sorted(origins),
                "manifest_digest": manifest_digest,
                "outcome": outcome,
                "snapshot": baseline_for_pending,
            },
        )
    message = sanitize_gate_text(
        message,
        _configured_token_value(config),
        "Agent Verification gate details were redacted; retry or use the offline outbox procedure.",
        maximum=4500,
    )
    count = _register_loop_attempt(loop_path, manifest_digest, outcome)
    if count <= max_blocks:
        return GateDecision(outcome, True, f"{message} Stop attempt {count} of {max_blocks}.")
    return GateDecision(
        outcome,
        False,
        f"{message} Loop protection allowed this turn to stop after {max_blocks} blocked attempt(s); do not claim normal synchronization.",
    )
