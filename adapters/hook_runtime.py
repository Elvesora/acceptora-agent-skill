#!/usr/bin/env python3
"""Shared deterministic runtime for Codex, Claude Code, and Gemini CLI adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
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


def _client_profiles() -> dict[str, dict[str, Any]]:
    registry_path = SKILL_ROOT / "config" / "client-profiles.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        reviewed_on = registry["capabilities_reviewed_on"]
        profiles = registry["clients"]
        if registry.get("schema_version") != 1:
            raise ValueError
        if not isinstance(reviewed_on, str) or date.fromisoformat(reviewed_on).isoformat() != reviewed_on:
            raise ValueError
        if not isinstance(profiles, list) or not profiles:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("the bundled Acceptora client provider registry is invalid") from error

    clients: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        try:
            client = profile["id"]
            lifecycle = profile["lifecycle"]
            update_event = lifecycle["update_check_event"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("the bundled Acceptora client provider registry is invalid") from error
        if (
            not isinstance(client, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", client) is None
            or client in clients
            or not isinstance(update_event, str)
            or not update_event
        ):
            raise RuntimeError("the bundled Acceptora client provider registry is invalid")
        clients[client] = profile
    return clients


SKILL_VERSION, INTEGRATION_VERSION, CONTRACT_VERSION = _package_versions()
CLIENT_PROFILES = _client_profiles()
SOURCE_ADAPTER_VERSION = "1.0.0"
CONFIG_RELATIVE_PATH = Path(".verification/config.json")
STATE_RELATIVE_PATH = Path(".verification/session-state")
MAX_GATE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GIT_LS_REMOTE_BYTES = 4096
SKILL_UPDATE_CACHE_TTL_SECONDS = 300
SKILL_UPDATE_CACHE_FILENAME = "skill-update.json"
GIT_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40,64}$")
CANONICAL_SKILL_REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
PRODUCTION_SKILL_BRANCH = "main"
TOKEN_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ACCEPTORA_TOKEN_PATTERN = re.compile(r"^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$")


class HookRuntimeError(RuntimeError):
    pass


class SkillUpdateUnavailable(HookRuntimeError):
    pass


class SkillUpdateRejected(HookRuntimeError):
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


def _skill_update_cache_path(root: Path) -> Path:
    return root / STATE_RELATIVE_PATH / SKILL_UPDATE_CACHE_FILENAME


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _record_digest(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return _sha256_bytes(_canonical_json_bytes(payload))


def _write_skill_update_cache(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    complete = {**record, "record_sha256": _record_digest(record)}
    _atomic_json(path, complete)
    return complete


def _load_skill_update_cache(
    path: Path,
    *,
    now: int,
    repository_url: str,
    branch: str,
    installed_commit_sha: str,
    git_executable: str,
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
        or record.get("kind") != "acceptora_git_main_update_check"
        or not isinstance(checked_at, int)
        or isinstance(checked_at, bool)
        or checked_at > now
        or now - checked_at > SKILL_UPDATE_CACHE_TTL_SECONDS
        or record.get("repository_url") != repository_url
        or record.get("branch") != branch
        or record.get("git_executable") != git_executable
        or record.get("client") != client
        or record.get("installed_commit_sha") != installed_commit_sha
        or record.get("setup_mutations_performed") != 0
        or record.get("cache_written") is not True
        or record.get("auto_apply") is not False
    ):
        return None
    status = record.get("status")
    current_commit_sha = record.get("current_commit_sha")
    if status in {"unavailable", "rejected"}:
        return record if current_commit_sha is None else None
    if status not in {"current", "update_available"}:
        return None
    if not isinstance(current_commit_sha, str) or GIT_COMMIT_PATTERN.fullmatch(current_commit_sha) is None:
        return None
    expected_status = "current" if current_commit_sha == installed_commit_sha else "update_available"
    if status != expected_status:
        return None
    return record


def _skill_update_message(record: dict[str, Any], cache_path: Path) -> str | None:
    status = record.get("status")
    if status in {"current", "unavailable"}:
        return None
    if status == "rejected":
        return (
            "Agent Verification update check warning: the production branch response was invalid; "
            "no skill source was fetched and no setup files were changed."
        )
    installed_commit = str(record.get("installed_commit_sha"))
    current_commit = str(record.get("current_commit_sha"))
    return (
        f"Acceptora Agent Skill update available: installed commit {installed_commit[:12]}, "
        f"production main commit {current_commit[:12]}. Ask your coding agent to clone a fresh main checkout from "
        f"{record.get('repository_url')} outside the target repository, read SETUP.md completely from that checkout, "
        'and follow its "Coding-agent install or update" procedure for an update. The printed cache path identifies '
        "the installed runtime needed by that procedure. "
        f"Review {cache_path} (record {record.get('record_sha256')}). No source was fetched and no update was applied."
    )


def _skill_update_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    for key in ("ACCEPTORA_AGENT_TOKEN", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
        environment.pop(key, None)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    return environment


def _remote_main_commit(
    git_executable: str,
    repository_url: str,
    branch: str,
    timeout_seconds: float,
) -> str:
    reference = f"refs/heads/{branch}"
    command = [
        git_executable,
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "http.followRedirects=false",
        "ls-remote",
        "--exit-code",
        "--heads",
        repository_url,
        reference,
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="acceptora-skill-update-") as isolated_worktree:
            environment = _skill_update_environment()
            environment["GIT_CEILING_DIRECTORIES"] = str(Path(isolated_worktree).resolve())
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
                env=environment,
                cwd=isolated_worktree,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SkillUpdateUnavailable("the production branch is unavailable") from error
    if process.returncode != 0:
        raise SkillUpdateUnavailable("the production branch is unavailable")
    if len(process.stdout) > MAX_GIT_LS_REMOTE_BYTES or len(process.stderr) > MAX_GIT_LS_REMOTE_BYTES:
        raise SkillUpdateRejected("the production branch response is too large")
    try:
        output = process.stdout.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise SkillUpdateRejected("the production branch response is invalid") from error
    lines = output.splitlines()
    if len(lines) != 1:
        raise SkillUpdateRejected("the production branch response is invalid")
    commit_sha, separator, observed_reference = lines[0].partition("\t")
    commit_sha = commit_sha.lower()
    if separator != "\t" or observed_reference != reference or GIT_COMMIT_PATTERN.fullmatch(commit_sha) is None:
        raise SkillUpdateRejected("the production branch response is invalid")
    return commit_sha


def _check_skill_update(
    config: dict[str, Any],
    cache_path: Path,
    *,
    now: int | None = None,
) -> str | None:
    checked_at = int(time.time()) if now is None else now
    repository_url = config.get("skill_repository_url")
    branch = config.get("skill_repository_branch")
    installed_commit_sha = config.get("installed_commit_sha")
    git_executable = config.get("git_executable")
    if repository_url != CANONICAL_SKILL_REPOSITORY_URL or branch != PRODUCTION_SKILL_BRANCH:
        raise SkillUpdateRejected("the installed production source is invalid")
    if not isinstance(installed_commit_sha, str) or GIT_COMMIT_PATTERN.fullmatch(installed_commit_sha) is None:
        raise SkillUpdateRejected("the installed commit is invalid")
    if not isinstance(git_executable, str) or not Path(git_executable).is_absolute():
        raise SkillUpdateRejected("the installed Git executable is invalid")
    client = config.get("client")
    if client not in CLIENT_PROFILES:
        raise SkillUpdateRejected("the installed client identity is invalid")
    timeout_seconds = max(0.5, min(float(config.get("skill_update_timeout_seconds", 3)), 5.0))
    cached = _load_skill_update_cache(
        cache_path,
        now=checked_at,
        repository_url=repository_url,
        branch=branch,
        installed_commit_sha=installed_commit_sha,
        git_executable=git_executable,
        client=client,
    )
    if cached is not None:
        return _skill_update_message(cached, cache_path)

    def cache_result(status: str, current_commit_sha: str | None) -> str | None:
        record = _write_skill_update_cache(
            cache_path,
            {
                "schema_version": 1,
                "kind": "acceptora_git_main_update_check",
                "checked_at_unix": checked_at,
                "repository_url": repository_url,
                "branch": branch,
                "git_executable": git_executable,
                "client": client,
                "installed_commit_sha": installed_commit_sha,
                "status": status,
                "current_commit_sha": current_commit_sha,
                "setup_mutations_performed": 0,
                "cache_written": True,
                "auto_apply": False,
            },
        )
        return _skill_update_message(record, cache_path)

    try:
        current_commit_sha = _remote_main_commit(git_executable, repository_url, branch, timeout_seconds)
    except SkillUpdateUnavailable:
        return cache_result("unavailable", None)
    except SkillUpdateRejected:
        return cache_result("rejected", None)
    status = "current" if current_commit_sha == installed_commit_sha else "update_available"
    return cache_result(status, current_commit_sha)


def check_for_skill_update(event: dict[str, Any]) -> str | None:
    event_name = str(event.get("hook_event_name") or event.get("event_name") or "")
    if event_name not in {
        profile["lifecycle"]["update_check_event"]
        for profile in CLIENT_PROFILES.values()
    }:
        return None
    root = _project_root(event)
    config = load_config(root)
    client = config.get("client")
    profile = CLIENT_PROFILES.get(client) if isinstance(client, str) else None
    if profile is None or event_name != profile["lifecycle"]["update_check_event"]:
        return None
    expected_config_path = (SKILL_ROOT / "config" / "runtime-config.json").resolve()
    if (
        config.get("config_source") != "installer_owned_external_runtime"
        or os.path.normcase(str(_config_path(root).resolve())) != os.path.normcase(str(expected_config_path))
    ):
        return None
    try:
        return _check_skill_update(config, _skill_update_cache_path(root))
    except SkillUpdateRejected:
        return (
            "Agent Verification update check warning: the installed Git update configuration is invalid; "
            "no skill source was fetched and no setup files were changed."
        )
    except (OSError, TypeError, ValueError):
        return (
            "Agent Verification update check warning: the Git check failed safely; "
            "no skill source was fetched and no setup files were changed."
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
