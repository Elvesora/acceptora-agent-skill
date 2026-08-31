#!/usr/bin/env python3
"""Plan, apply, inspect, or roll back a verified agent-skill installation."""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANAGED_START = "<!-- agent-verification:start -->"
MANAGED_END = "<!-- agent-verification:end -->"
PROJECT_ID_PATTERN = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PLATFORMS = ("auto", "windows", "posix")
CLIENT_REGISTRY_PATH = "config/client-profiles.json"
CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CLIENT_ENV_SIGNALS = {
    "codex": ("CODEX_HOME", "CODEX_THREAD_ID"),
    "claude-code": ("CLAUDECODE", "CLAUDE_CODE"),
    "gemini-cli": ("GEMINI_CLI",),
    "antigravity-cli": (),
}
CLIENT_MARKER_DIRECTORIES = {
    "codex": (".agents", ".codex"),
    "claude-code": (".claude",),
    "gemini-cli": (".gemini",),
    "antigravity-cli": (),
}
FALSEY_ENV_VALUES = {"", "0", "false", "no", "off"}
EPHEMERAL_PARTS = {".git", ".github", ".pytest_cache", ".verification", "__pycache__", "dist", "tests"}
EPHEMERAL_SUFFIXES = {".pyc", ".pyo", ".deferred"}
RELEASE_IDENTITY_EXCLUDED_FILES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "scripts/build_release.py",
    "scripts/preview_install.py",
}
INSTALL_EXCLUDED_FILES = RELEASE_IDENTITY_EXCLUDED_FILES | {
    "CHANGELOG.md",
    "SETUP.md",
    "SETUP-CODEX.md",
    "SETUP-CLAUDE-CODE.md",
    "SETUP-GEMINI-CLI.md",
    "GETTING-STARTED.md",
}
SKILL_FILES = {
    "LICENSE",
    "SKILL.md",
    "scripts/build_source_manifest.py",
    "scripts/read_instruction_snapshot.py",
    "scripts/store_project_credential.py",
    "scripts/validate_checklist_payload.py",
    "scripts/write_offline_outbox.py",
}
SKILL_DIRECTORIES = {"agents", "references"}
LEGACY_TOKEN_ENV = "ACCEPTORA_AGENT_TOKEN"
PROJECT_TOKEN_ENV_PREFIX = "ACCEPTORA_AGENT_TOKEN_"
WINDOWS_TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
WINDOWS_CODEX_SANDBOX_USERS_NAME = "CodexSandboxUsers"
WINDOWS_READ_EXECUTE_SYNCHRONIZE_RIGHTS = 0x001200A9
ACCEPTORA_TOKEN_PATTERN = r"^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$"
PROJECT_ENV_TEMPLATE_PARTS = {
    "default",
    "defaults",
    "dist",
    "example",
    "examples",
    "sample",
    "samples",
    "template",
    "templates",
}
MANAGED_HOOK_ID_PATTERN = re.compile(r"acceptora-target:([a-f0-9]{32})")
PACKAGE_IDENTITY = "acceptora"
RUNTIME_NAMESPACE = "acceptora/verify-generated-work/runtimes"
RUNTIME_RECEIPT = "install-receipt.json"
RUNTIME_PACKAGE_DIRECTORY = "package"
MAX_SOURCE_FILE_SIZE = 8 * 1024 * 1024
CANONICAL_SKILL_REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
PRODUCTION_SKILL_BRANCH = "main"
EMBEDDED_PROVENANCE_FILENAME = "acceptora-agent-skill-provenance.json"
GIT_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40,64}$")
SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_UNSPECIFIED = object()
MINIMUM_PYTHON = (3, 11)
_WINDOWS_CURRENT_SID: str | None = None
_WINDOWS_CODEX_SANDBOX_USERS_SID: str | None | object = _UNSPECIFIED


class InstallError(RuntimeError):
    """A safe, user-actionable installer failure."""


def _assert_running_python_version() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        raise InstallError("acceptora requires Python 3.11 or newer.")


# Kept as a compatibility alias for callers of preview_install.py.
PreviewError = InstallError


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _credential_free_environment(
    *additional_blocked_prefixes: str,
    blocked_names: set[str] | None = None,
) -> dict[str, str]:
    excluded_names = {LEGACY_TOKEN_ENV, *(name.upper() for name in (blocked_names or set()))}
    excluded_prefixes = (PROJECT_TOKEN_ENV_PREFIX, *(prefix.upper() for prefix in additional_blocked_prefixes))
    environment: dict[str, str] = {}
    for key in os.environ:
        normalized = key.upper()
        if normalized in excluded_names or normalized.startswith(excluded_prefixes):
            continue
        environment[key] = os.environ[key]
    return environment


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
            and left == right
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_equal(left_item, right_item) for left_item, right_item in zip(left, right, strict=True))
        )
    return type(left) is type(right) and left == right


def _sha256_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _file_hash(path: Path) -> str | None:
    if not path.exists() and not _is_linklike(path):
        return None
    if _is_linklike(path) or not path.is_file():
        raise InstallError(f"Managed destination is not a regular file: {path}")
    return _sha256_file(path)


def _normal_path(path: Path) -> str:
    return path.as_posix()


def _validate_relative(relative: str) -> PurePosixPath:
    value = relative.replace("\\", "/")
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise InstallError(f"Unsafe managed path: {relative}")
    if re.match(r"^[A-Za-z]:", value):
        raise InstallError(f"Unsafe managed path: {relative}")
    return candidate


def _assert_no_case_collision(parent: Path, name: str) -> None:
    if not parent.exists() or not parent.is_dir():
        return
    folded = name.casefold()
    for child in parent.iterdir():
        if child.name.casefold() == folded and child.name != name:
            raise InstallError(f"Case-colliding destination exists: {child} conflicts with {name}")


def _safe_target(root: Path, relative: str) -> Path:
    parts = _validate_relative(relative).parts
    current = root
    for part in parts:
        if current.exists() and _is_linklike(current):
            raise InstallError(f"Managed destination crosses a symlink or junction: {current}")
        _assert_no_case_collision(current, part)
        current = current / part
    if _is_linklike(current):
        raise InstallError(f"Managed destination is a symlink or junction: {current}")
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InstallError("A managed destination would leave the target repository.") from exc
    return current


def _validated_root(value: str) -> Path:
    requested = Path(value).expanduser().absolute()
    if any(path.exists() and _is_linklike(path) for path in (requested, *requested.parents)):
        raise InstallError("The target repository path must not cross a symlink or junction.")
    root = requested.resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise InstallError("The target repository must be an existing directory.")
    return root


def _default_runtime_base() -> Path:
    if os.name == "nt":
        local_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_data) if local_data else Path.home() / "AppData" / "Local"
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base.expanduser().absolute() / Path(RUNTIME_NAMESPACE)


def _validate_external_path(path: Path, label: str) -> Path:
    requested = path.expanduser().absolute()
    if any(candidate.exists() and _is_linklike(candidate) for candidate in (requested, *requested.parents)):
        raise InstallError(f"{label} must not cross a symlink or junction.")
    resolved = requested.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise InstallError(f"{label} must be a directory.")
    return resolved


def _windows_system_tool(relative: str) -> Path:
    if os.name != "nt":
        raise InstallError("Windows ACL tooling is unavailable on this platform.")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise InstallError("The trusted Windows system directory could not be resolved.")
    tool = (Path(buffer.value) / Path(relative)).resolve(strict=False)
    if not tool.is_file() or _is_linklike(tool):
        raise InstallError(f"Required trusted Windows ACL tool is unavailable: {relative}")
    return tool


def _windows_acl_infos(paths: list[Path]) -> dict[str, dict[str, Any]]:
    if not paths:
        return {}
    powershell = _windows_system_tool("System32/WindowsPowerShell/v1.0/powershell.exe")
    encoded_paths = base64.b64encode(
        json.dumps([str(path) for path in paths], ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script = (
        f"$ps=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_paths}'))|ConvertFrom-Json);"
        "$current=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
        "@($ps|ForEach-Object{$p=[string]$_;$a=Get-Acl -LiteralPath $p;"
        "[pscustomobject]@{path=$p;current=$current;"
        "owner=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;"
        "rules=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])|ForEach-Object {"
        "[pscustomobject]@{sid=$_.IdentityReference.Value;type=$_.AccessControlType.ToString();"
        "rights=[int64]$_.FileSystemRights;inherited=$_.IsInherited;"
        "inheritance=$_.InheritanceFlags.ToString();propagation=$_.PropagationFlags.ToString()}})}})|"
        "ConvertTo-Json -Compress -Depth 5"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = _credential_free_environment(
        blocked_names={"PSMODULEPATH", "POWERSHELL_TELEMETRY_OPTOUT"}
    )
    command = [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script]
    process: subprocess.CompletedProcess[str] | None = None
    timeout_error: subprocess.TimeoutExpired | None = None
    for _attempt in range(2):
        try:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                timeout=15,
                check=False,
            )
            break
        except subprocess.TimeoutExpired as error:
            timeout_error = error
        except OSError as error:
            raise InstallError("The external path ACLs could not be inspected.") from error
    if process is None:
        raise InstallError("The external path ACLs could not be inspected.") from timeout_error
    if process.returncode != 0:
        raise InstallError("The external path ACLs could not be inspected.")
    try:
        value = json.loads(process.stdout.strip())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InstallError("The external path ACL inspection returned invalid data.") from error
    records = [value] if isinstance(value, dict) else value
    if not isinstance(records, list) or len(records) != len(paths):
        raise InstallError("The external path ACL inspection returned invalid data.")
    mapped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise InstallError("The external path ACL inspection returned invalid data.")
        mapped[record["path"]] = record
    if set(mapped) != {str(path) for path in paths}:
        raise InstallError("The external path ACL inspection returned unexpected paths.")
    current_sids = {record.get("current") for record in mapped.values()}
    if len(current_sids) != 1:
        raise InstallError("The external path ACL inspection returned inconsistent user identities.")
    current_sid = next(iter(current_sids))
    if not isinstance(current_sid, str) or re.fullmatch(r"S-[0-9-]+", current_sid) is None:
        raise InstallError("The external path ACL inspection returned an invalid user identity.")
    global _WINDOWS_CURRENT_SID
    if _WINDOWS_CURRENT_SID is not None and _WINDOWS_CURRENT_SID != current_sid:
        raise InstallError("The current Windows user identity changed during installer execution.")
    _WINDOWS_CURRENT_SID = current_sid
    return mapped


def _windows_acl_info(path: Path) -> dict[str, Any]:
    return _windows_acl_infos([path])[str(path)]


def _windows_current_sid() -> str:
    global _WINDOWS_CURRENT_SID
    if _WINDOWS_CURRENT_SID is not None:
        return _WINDOWS_CURRENT_SID
    powershell = _windows_system_tool("System32/WindowsPowerShell/v1.0/powershell.exe")
    environment = _credential_free_environment(
        blocked_names={"PSMODULEPATH", "POWERSHELL_TELEMETRY_OPTOUT"}
    )
    try:
        process = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The current Windows user SID could not be resolved.") from error
    sid = process.stdout.strip()
    if process.returncode != 0 or re.fullmatch(r"S-[0-9-]+", sid) is None:
        raise InstallError("The current Windows user SID could not be resolved.")
    _WINDOWS_CURRENT_SID = sid
    return sid


def _windows_codex_sandbox_users_sid() -> str | None:
    global _WINDOWS_CODEX_SANDBOX_USERS_SID
    if _WINDOWS_CODEX_SANDBOX_USERS_SID is not _UNSPECIFIED:
        return (
            _WINDOWS_CODEX_SANDBOX_USERS_SID
            if isinstance(_WINDOWS_CODEX_SANDBOX_USERS_SID, str)
            else None
        )
    powershell = _windows_system_tool("System32/WindowsPowerShell/v1.0/powershell.exe")
    environment = _credential_free_environment(
        blocked_names={"PSMODULEPATH", "POWERSHELL_TELEMETRY_OPTOUT"}
    )
    script = (
        "try{"
        "$account=[System.Security.Principal.NTAccount]::new("
        f"[Environment]::MachineName,'{WINDOWS_CODEX_SANDBOX_USERS_NAME}');"
        "$account.Translate([System.Security.Principal.SecurityIdentifier]).Value"
        "}catch [System.Security.Principal.IdentityNotMappedException]{exit 3}"
    )
    try:
        process = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _WINDOWS_CODEX_SANDBOX_USERS_SID = None
        return None
    sid = process.stdout.strip()
    if process.returncode == 0 and re.fullmatch(r"S-[0-9-]+", sid) is not None:
        _WINDOWS_CODEX_SANDBOX_USERS_SID = sid
        return sid
    _WINDOWS_CODEX_SANDBOX_USERS_SID = None
    return None


def _windows_private_directory_allow_rule_is_supported(
    rule: dict[str, Any],
    current_sid: str,
    allowed_read_execute_sid: str | None,
) -> bool:
    if rule.get("type") != "Allow":
        return False
    if rule.get("sid") in {current_sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}:
        return rule.get("inherited") is False
    rights = rule.get("rights")
    return (
        isinstance(allowed_read_execute_sid, str)
        and rule.get("sid") == allowed_read_execute_sid
        and type(rights) is int
        and rights & 0xFFFFFFFF == WINDOWS_READ_EXECUTE_SYNCHRONIZE_RIGHTS
        and rule.get("inherited") is False
    )


def _assert_private_directory(
    path: Path,
    label: str,
    *,
    allowed_read_execute_sid: str | None = None,
) -> None:
    if not path.is_dir() or _is_linklike(path):
        raise InstallError(f"{label} must be a regular private directory.")
    if os.name == "nt":
        acl = _windows_acl_info(path)
        current = acl.get("current")
        rules = acl.get("rules")
        if isinstance(rules, dict):
            rules = [rules]
        if acl.get("owner") != current or not isinstance(current, str) or not isinstance(rules, list):
            raise InstallError(f"{label} must be owned by the current Windows user.")
        allowed = [rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "Allow"]
        if (
            not allowed
            or any(
                not _windows_private_directory_allow_rule_is_supported(
                    rule,
                    current,
                    allowed_read_execute_sid,
                )
                for rule in allowed
            )
            or not any(
                rule.get("sid") in {current, "S-1-3-4"}
                and int(rule.get("rights", 0)) & 0x1F01FF == 0x1F01FF
                for rule in allowed
            )
        ):
            raise InstallError(f"{label} ACL must grant access only to the current Windows user.")
        return
    metadata = path.stat()
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise InstallError(f"{label} must be owned by the current user.")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise InstallError(f"{label} must not be accessible by group or other users.")


def _set_windows_owner_only_acl(path: Path, *, directory: bool) -> str:
    sid = _windows_current_sid()
    icacls = _windows_system_tool("System32/icacls.exe")
    permission = f"*{sid}:{'(OI)(CI)' if directory else ''}F"
    command = [str(icacls), str(path), "/inheritance:r", "/grant:r", permission, "/Q"]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_credential_free_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The managed external path ACL could not be made owner-only.") from error
    if result.returncode != 0:
        raise InstallError("The managed external path ACL could not be made owner-only.")
    return sid


def _grant_windows_directory_read_execute_acl(path: Path, sid: str) -> None:
    if re.fullmatch(r"S-[0-9-]+", sid) is None:
        raise InstallError("The managed external path read-execute SID is invalid.")
    icacls = _windows_system_tool("System32/icacls.exe")
    command = [str(icacls), str(path), "/grant:r", f"*{sid}:(OI)(CI)RX", "/Q"]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_credential_free_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The managed external path ACL could not grant read-execute access.") from error
    if result.returncode != 0:
        raise InstallError("The managed external path ACL could not grant read-execute access.")


def _secure_private_directory(
    path: Path,
    *,
    allowed_read_execute_sid: str | None = None,
) -> None:
    if os.name == "nt":
        _set_windows_owner_only_acl(path, directory=True)
        if allowed_read_execute_sid is not None:
            _grant_windows_directory_read_execute_acl(path, allowed_read_execute_sid)
    else:
        os.chmod(path, 0o700)
    _assert_private_directory(
        path,
        "External runtime directory",
        allowed_read_execute_sid=allowed_read_execute_sid,
    )


def _secure_private_file(path: Path) -> None:
    if _is_linklike(path) or not path.is_file():
        raise InstallError("A private client configuration target must be a regular file.")
    if os.name == "nt":
        _set_windows_owner_only_acl(path, directory=False)
        _assert_private_file(path)
        return
    os.chmod(path, 0o600)
    _assert_private_file(path)


def _assert_private_file(path: Path) -> None:
    if _is_linklike(path) or not path.is_file():
        raise InstallError("A private client configuration target must be a regular file.")
    if os.name == "nt":
        acl = _windows_acl_info(path)
        sid = acl.get("current")
        rules = acl.get("rules")
        if isinstance(rules, dict):
            rules = [rules]
        allowed = [rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "Allow"] if isinstance(rules, list) else []
        if (
            not isinstance(sid, str)
            or acl.get("owner") != sid
            or not allowed
            or any(
                rule.get("sid") not in {sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
                for rule in allowed
            )
            or not any(
                rule.get("sid") == sid
                and int(rule.get("rights", 0)) & 0x1F01FF == 0x1F01FF
                for rule in allowed
            )
        ):
            raise InstallError("The client configuration file ACL is not owner-only.")
        return
    metadata = path.stat()
    if (hasattr(os, "getuid") and metadata.st_uid != os.getuid()) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise InstallError("The client configuration file is not owner-only.")


def _assert_safe_user_path_ancestor_chain(base: Path, label: str) -> None:
    home = Path.home().resolve(strict=True)
    try:
        base.relative_to(home)
    except ValueError as error:
        raise InstallError(f"{label} must be inside the current user's verified home directory.") from error

    current = base if base.exists() else base.parent
    while not current.exists():
        current = current.parent
    candidates: list[Path] = []
    while True:
        candidates.append(current)
        if current == home:
            break
        if current == current.parent:
            raise InstallError(f"{label} ancestor chain does not reach the current user's home directory.")
        current = current.parent

    if os.name == "nt":
        trusted_owners = {"S-1-5-18", "S-1-5-32-544"}
        trusted_sids = {*trusted_owners, "S-1-3-4"}
        dangerous = (
            0x00000002
            | 0x00000004
            | 0x00000010
            | 0x00000040
            | 0x00000100
            | 0x00010000
            | 0x00040000
            | 0x00080000
            | 0x40000000
            | 0x10000000
        )
        acl_by_path = _windows_acl_infos(candidates)
        for candidate in candidates:
            acl = acl_by_path[str(candidate)]
            current_sid = acl.get("current")
            rules = acl.get("rules")
            if isinstance(rules, dict):
                rules = [rules]
            if not isinstance(current_sid, str) or not isinstance(rules, list):
                raise InstallError(f"{label} ancestor ACL inspection returned invalid data.")
            if acl.get("owner") not in {*trusted_owners, current_sid}:
                raise InstallError(f"{label} ancestor is owned by an untrusted Windows principal: {candidate}")
            for rule in rules:
                if not isinstance(rule, dict) or rule.get("type") != "Allow":
                    continue
                if "InheritOnly" in str(rule.get("propagation", "")):
                    continue
                sid = rule.get("sid")
                rights = int(rule.get("rights", 0)) & 0xFFFFFFFF
                if sid not in {*trusted_sids, current_sid} and rights & dangerous:
                    raise InstallError(f"{label} ancestor permits replacement by another Windows principal: {candidate}")
        return

    for candidate in candidates:
        metadata = candidate.stat()
        if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
            raise InstallError(f"{label} ancestor is owned by another user: {candidate}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise InstallError(f"{label} ancestor is group/world-writable without sticky protection: {candidate}")


def _assert_safe_executable_ancestor_chain(path: Path, label: str) -> None:
    candidates = [path, *path.parents]
    if os.name == "nt":
        trusted_owners = {"S-1-5-18", "S-1-5-32-544"}
        trusted_sids = {*trusted_owners, "S-1-3-4"}
        parent_dangerous = 0x00000040 | 0x00010000 | 0x00040000 | 0x00080000 | 0x10000000
        leaf_dangerous = (
            parent_dangerous
            | 0x00000002
            | 0x00000004
            | 0x00000010
            | 0x00000100
            | 0x40000000
        )
        acl_by_path = _windows_acl_infos(candidates)
        for index, candidate in enumerate(candidates):
            acl = acl_by_path[str(candidate)]
            current_sid = acl.get("current")
            owner = acl.get("owner")
            rules = acl.get("rules")
            if isinstance(rules, dict):
                rules = [rules]
            owner_is_trusted_service = owner == WINDOWS_TRUSTED_INSTALLER_SID
            if (
                not isinstance(current_sid, str)
                or not isinstance(rules, list)
                or (owner not in {*trusted_owners, current_sid} and not owner_is_trusted_service)
            ):
                raise InstallError(f"{label} has an untrusted owner or unreadable ancestor ACL: {candidate}")
            for rule in rules:
                if not isinstance(rule, dict) or rule.get("type") != "Allow":
                    continue
                if "InheritOnly" in str(rule.get("propagation", "")):
                    continue
                sid = rule.get("sid")
                rights = int(rule.get("rights", 0)) & 0xFFFFFFFF
                dangerous = leaf_dangerous if index == 0 else parent_dangerous
                sid_is_trusted_service = sid == WINDOWS_TRUSTED_INSTALLER_SID
                if sid not in {*trusted_sids, current_sid} and not sid_is_trusted_service and rights & dangerous:
                    raise InstallError(f"{label} can be replaced by another Windows principal: {candidate}")
        return

    for candidate in candidates:
        metadata = candidate.lstat()
        if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
            raise InstallError(f"{label} has an ancestor owned by another user: {candidate}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022 and not (candidate.is_dir() and mode & stat.S_ISVTX):
            raise InstallError(f"{label} has a replaceable group/world-writable ancestor: {candidate}")


def _runtime_identity(root: Path, client: str) -> str:
    root_identity = str(root).casefold() if os.name == "nt" else root.as_posix()
    return hashlib.sha256(f"{root_identity}\0{client}".encode("utf-8")).hexdigest()[:32]


def _runtime_root(root: Path, client: str, runtime_base: str | None) -> Path:
    base = _validate_external_path(Path(runtime_base) if runtime_base else _default_runtime_base(), "Runtime base")
    _assert_safe_user_path_ancestor_chain(base, "Runtime base")
    sandbox_sid = _windows_codex_sandbox_users_sid() if os.name == "nt" else None
    if base.exists():
        if os.name == "nt":
            _assert_private_directory(
                base,
                "Runtime base",
                allowed_read_execute_sid=sandbox_sid,
            )
        else:
            _assert_private_directory(base, "Runtime base")
    runtime = _validate_external_path(base / _runtime_identity(root, client), "Runtime directory")
    if runtime.exists():
        _assert_private_directory(
            runtime,
            "Runtime directory",
            allowed_read_execute_sid=sandbox_sid if client == "codex" else None,
        )
    try:
        runtime.relative_to(root)
    except ValueError:
        pass
    else:
        raise InstallError("The executable runtime must be outside the target repository.")
    return runtime


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


def _enclosing_worktree_root(root: Path) -> Path:
    for candidate in (root, *root.parents):
        marker = candidate / ".git"
        if marker.exists() or _is_linklike(marker):
            return candidate
    return root


def _assert_worktree_boundary(root: Path) -> None:
    worktree = _enclosing_worktree_root(root)
    if worktree != root:
        raise InstallError("The target repository must be the enclosing Git worktree root, not a subdirectory.")
    marker = root / ".git"
    if _is_linklike(marker):
        raise InstallError("The target repository .git marker must not be a symlink or junction.")


def _validated_executable(value: str, label: str, root: Path) -> Path:
    requested = Path(value).expanduser()
    if not requested.is_absolute():
        raise InstallError(f"{label} must be an absolute executable path.")
    requested = requested.absolute()
    if any(candidate.exists() and _is_linklike(candidate) for candidate in (requested, *requested.parents)):
        raise InstallError(f"{label} must not cross a symlink or junction.")
    resolved = requested.resolve(strict=False)
    if any(character in resolved.as_posix() for character in {'"', "\r", "\n", "\x00", "$", "`", "%", "!", "\\"}):
        raise InstallError(f"{label} contains characters that cannot be safely embedded in a hook command.")
    if not resolved.is_file() or _is_linklike(resolved):
        raise InstallError(f"{label} must name an existing regular executable file.")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise InstallError(f"{label} is not executable.")
    untrusted_root = _enclosing_worktree_root(root)
    try:
        resolved.relative_to(untrusted_root)
    except ValueError:
        _assert_safe_executable_ancestor_chain(resolved, label)
        return resolved
    raise InstallError(f"{label} must be outside the enclosing repository worktree.")


def _validated_historical_executable(value: str, label: str, root: Path) -> Path:
    requested = Path(value).expanduser()
    if not requested.is_absolute():
        raise InstallError(f"{label} must be an absolute executable path.")
    requested = requested.absolute()
    if any(candidate.exists() and _is_linklike(candidate) for candidate in (requested, *requested.parents)):
        raise InstallError(f"{label} must not cross a symlink or junction.")
    resolved = requested.resolve(strict=False)
    if any(character in resolved.as_posix() for character in {'"', "\r", "\n", "\x00", "$", "`", "%", "!", "\\"}):
        raise InstallError(f"{label} contains characters that cannot be safely embedded in a hook command.")
    untrusted_root = _enclosing_worktree_root(root)
    try:
        resolved.relative_to(untrusted_root)
    except ValueError:
        pass
    else:
        raise InstallError(f"{label} must be outside the enclosing repository worktree.")
    existing = requested
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.exists():
        raise InstallError(f"{label} has no verifiable existing ancestor.")
    if requested.exists() and (not requested.is_file() or _is_linklike(requested)):
        raise InstallError(f"{label} must name a regular file when it exists.")
    _assert_safe_executable_ancestor_chain(existing, label)
    return resolved


def _resolved_python_executable(root: Path, override: str | None) -> Path:
    candidate = override or str(Path(sys.executable).resolve(strict=True))
    executable = _validated_executable(candidate, "Python executable", root)
    environment = _credential_free_environment("PYTHON")
    try:
        process = subprocess.run(
            [
                str(executable),
                "-I",
                "-c",
                (
                    "import json,os,sys;"
                    "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor],"
                    "'executable':os.path.realpath(sys.executable)}))"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=10,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The selected Python executable could not be version-checked safely.") from error
    try:
        probe = json.loads(process.stdout) if process.returncode == 0 else None
        version = tuple(probe["version"]) if isinstance(probe, dict) else ()
        identity = Path(probe["executable"]).resolve(strict=True) if isinstance(probe, dict) else None
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        version = ()
        identity = None
    if (
        len(version) != 2
        or any(not isinstance(part, int) for part in version)
        or version < MINIMUM_PYTHON
        or identity is None
        or (str(identity).casefold() if os.name == "nt" else str(identity))
        != (str(executable).casefold() if os.name == "nt" else str(executable))
    ):
        raise InstallError("The selected hook interpreter must be Python 3.11 or newer.")
    return executable


def _resolved_git_executable(root: Path, override: str | None) -> Path:
    candidate = override or shutil.which("git")
    if not candidate:
        raise InstallError("Git executable was not found; pass --git-executable with a trusted absolute path.")
    return _validated_executable(candidate, "Git executable", root)


def _isolated_git_environment() -> dict[str, str]:
    environment = _credential_free_environment("GIT_")
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _package_git_value(git_executable: Path, *arguments: str) -> str:
    try:
        process = subprocess.run(
            [
                str(git_executable),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(PACKAGE_ROOT),
                *arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            env=_isolated_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The skill package Git identity could not be inspected.") from error
    if process.returncode != 0 or process.stderr.strip():
        raise InstallError("The skill package Git identity could not be inspected.")
    try:
        value = process.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise InstallError("The skill package Git identity is not valid UTF-8.") from error
    if not value or "\n" in value or "\r" in value:
        raise InstallError("The skill package Git identity is invalid.")
    return value


def _package_git_worktree_root(git_executable: Path) -> Path | None:
    try:
        process = subprocess.run(
            [
                str(git_executable),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(PACKAGE_ROOT),
                "rev-parse",
                "--show-toplevel",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            env=_isolated_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The skill package Git identity could not be inspected.") from error
    if process.returncode != 0:
        if (PACKAGE_ROOT / ".git").exists() or _is_linklike(PACKAGE_ROOT / ".git"):
            raise InstallError("The skill package Git identity could not be inspected.")
        return None
    if process.stderr.strip():
        raise InstallError("The skill package Git identity could not be inspected.")
    try:
        value = process.stdout.decode("utf-8", errors="strict").strip()
        root = Path(value).resolve(strict=True)
    except (UnicodeDecodeError, OSError, ValueError) as error:
        raise InstallError("The skill package Git identity is invalid.") from error
    if not value or "\n" in value or "\r" in value or not root.is_dir():
        raise InstallError("The skill package Git identity is invalid.")
    return root


def _embedded_package_source_identity(args: argparse.Namespace) -> dict[str, str]:
    if PACKAGE_ROOT.name != PACKAGE_IDENTITY:
        raise InstallError("The extracted skill package directory has an unexpected name.")
    provenance_path = PACKAGE_ROOT.parent / EMBEDDED_PROVENANCE_FILENAME
    if not provenance_path.exists() and not _is_linklike(provenance_path):
        raise InstallError("The extracted skill package is missing its embedded provenance record.")
    if _is_linklike(provenance_path) or not provenance_path.is_file():
        raise InstallError("The embedded skill provenance record is not a regular file.")
    if provenance_path.stat().st_size > 16 * 1024:
        raise InstallError("The embedded skill provenance record is unexpectedly large.")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError("The embedded skill provenance record is malformed.") from error
    expected_keys = {
        "branch",
        "commit_sha",
        "repository_url",
        "schema_version",
        "source_tree_sha256",
    }
    if not isinstance(provenance, dict) or set(provenance) != expected_keys or provenance.get("schema_version") != 1:
        raise InstallError("The embedded skill provenance record is malformed.")
    repository_url = provenance.get("repository_url")
    branch = provenance.get("branch")
    commit_sha = provenance.get("commit_sha")
    source_tree_sha256 = provenance.get("source_tree_sha256")
    if repository_url != CANONICAL_SKILL_REPOSITORY_URL:
        raise InstallError("The embedded skill provenance does not name the canonical Acceptora repository.")
    if branch != PRODUCTION_SKILL_BRANCH:
        raise InstallError("The embedded skill provenance does not name the production main branch.")
    if not isinstance(commit_sha, str) or GIT_COMMIT_PATTERN.fullmatch(commit_sha) is None:
        raise InstallError("The embedded skill provenance commit is invalid.")
    if not isinstance(source_tree_sha256, str) or SHA256_PATTERN.fullmatch(source_tree_sha256) is None:
        raise InstallError("The embedded skill provenance tree digest is invalid.")
    observed_tree_sha256 = _package_source_tree_sha256(_iter_release_identity_files())
    if observed_tree_sha256 != source_tree_sha256:
        raise InstallError("The extracted skill package does not match its embedded source-tree digest.")
    requested_commit = getattr(args, "installed_commit_sha", None)
    if requested_commit is not None and requested_commit != commit_sha:
        raise InstallError("The skill package commit changed after the installation plan was created.")
    return {
        "repository_url": repository_url,
        "branch": branch,
        "commit_sha": commit_sha,
    }


def _assert_clean_package_checkout(git_executable: Path) -> None:
    try:
        process = subprocess.run(
            [
                str(git_executable),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(PACKAGE_ROOT),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            env=_isolated_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The skill package Git state could not be inspected.") from error
    if process.returncode != 0 or process.stderr.strip():
        raise InstallError("The skill package Git state could not be inspected.")
    if process.stdout.strip():
        raise InstallError("The skill package must be a clean checkout of the production main branch.")


def _normalized_repository_url(value: str) -> str:
    normalized = value.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _package_source_identity(
    args: argparse.Namespace,
    git_executable: Path,
    *,
    historical: bool,
) -> dict[str, str]:
    if historical:
        repository_url = getattr(args, "skill_repository_url", None)
        branch = getattr(args, "skill_repository_branch", None)
        commit_sha = getattr(args, "installed_commit_sha", None)
    else:
        package_root = _package_git_worktree_root(git_executable)
        if package_root is None:
            return _embedded_package_source_identity(args)
        if os.path.normcase(str(package_root)) != os.path.normcase(str(PACKAGE_ROOT)):
            if (PACKAGE_ROOT / ".git").exists() or _is_linklike(PACKAGE_ROOT / ".git"):
                raise InstallError("The skill package must be run from its canonical Git worktree root.")
            return _embedded_package_source_identity(args)
        observed_repository_url = _package_git_value(git_executable, "remote", "get-url", "origin")
        if _normalized_repository_url(observed_repository_url) != CANONICAL_SKILL_REPOSITORY_URL:
            raise InstallError("The skill package origin is not the canonical Acceptora repository.")
        repository_url = CANONICAL_SKILL_REPOSITORY_URL
        branch = _package_git_value(git_executable, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != PRODUCTION_SKILL_BRANCH:
            raise InstallError("The skill package must be checked out on the production main branch.")
        commit_sha = _package_git_value(git_executable, "rev-parse", "--verify", "HEAD^{commit}").lower()
        branch_commit_sha = _package_git_value(
            git_executable,
            "rev-parse",
            "--verify",
            f"refs/heads/{PRODUCTION_SKILL_BRANCH}^{{commit}}",
        ).lower()
        if branch_commit_sha != commit_sha:
            raise InstallError("The skill package HEAD does not match the production main branch.")
        _assert_clean_package_checkout(git_executable)
        requested_commit = getattr(args, "installed_commit_sha", None)
        if requested_commit is not None and requested_commit != commit_sha:
            raise InstallError("The skill package commit changed after the installation plan was created.")

    if repository_url != CANONICAL_SKILL_REPOSITORY_URL or branch != PRODUCTION_SKILL_BRANCH:
        raise InstallError("The skill package source is not the canonical production branch.")
    if not isinstance(commit_sha, str) or GIT_COMMIT_PATTERN.fullmatch(commit_sha) is None:
        raise InstallError("The skill package commit is invalid.")
    return {
        "repository_url": repository_url,
        "branch": branch,
        "commit_sha": commit_sha,
    }


def _assert_actual_git_worktree_root(root: Path, git_executable: Path) -> None:
    environment = _credential_free_environment("GIT_")
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    try:
        process = subprocess.run(
            [str(git_executable), "-c", "core.fsmonitor=false", "-C", str(root), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("The target repository could not be verified with the pinned Git executable.") from error
    if process.returncode != 0:
        raise InstallError("The target repository must be an actual Git worktree root.")
    try:
        observed = Path(process.stdout.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise InstallError("The pinned Git executable returned an invalid worktree root.") from error
    if os.path.normcase(str(observed)) != os.path.normcase(str(root)):
        raise InstallError("The target repository must be the actual Git worktree root, not a subdirectory.")


def _assert_supported_git_index(root: Path, git_executable: Path) -> None:
    environment = _credential_free_environment("GIT_")
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )

    def run(*arguments: str) -> bytes:
        try:
            process = subprocess.run(
                [str(git_executable), "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallError("The target Git index could not be inspected with the pinned executable.") from error
        if process.returncode != 0:
            raise InstallError("The target Git index is unavailable or corrupt.")
        return process.stdout

    for record in (value for value in run("ls-files", "-s", "-z").split(b"\0") if value):
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3 or fields[2] != b"0":
            raise InstallError("The strict Git source adapter does not support unresolved index stages.")
        if fields[0] == b"160000":
            raise InstallError("The strict Git source adapter does not support Git submodules.")
        try:
            relative = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise InstallError("The strict Git source adapter requires UTF-8 repository paths.") from error
        if (
            "\\" in relative
            or relative.startswith("/")
            or re.match(r"^[A-Za-z]:", relative)
            or any(ord(character) < 32 or ord(character) == 127 for character in relative)
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise InstallError("The strict Git source adapter found an unsafe or non-portable repository path.")

    for record in (value for value in run("ls-files", "-v", "-z").split(b"\0") if value):
        tag, separator = record[:1], record[1:2]
        if separator != b" ":
            raise InstallError("The pinned Git executable returned invalid index flags.")
        if tag == b"S" or tag.islower():
            raise InstallError("The strict Git source adapter does not support assume-unchanged or skip-worktree paths.")


def _is_project_environment_filename(name: str) -> bool:
    normalized = name.casefold()
    parts = {part for part in re.split(r"[._-]+", normalized) if part}
    if parts & PROJECT_ENV_TEMPLATE_PARTS:
        return False
    return (
        normalized in {".env", ".envrc", ".dev.vars"}
        or normalized.startswith(".env.")
        or normalized.startswith(".dev.vars.")
        or normalized.endswith(".env")
    )


def _assert_project_environment_storage(
    root: Path,
    git_executable: Path,
    token_env: str,
) -> None:
    if token_env in os.environ:
        return

    discovered: dict[str, tuple[int, int, int, int, int]] = {}
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError as error:
        raise InstallError("Project environment-file storage could not be inspected safely.") from error
    for path in entries:
        if not _is_project_environment_filename(path.name):
            continue
        if _is_linklike(path):
            raise InstallError(
                f"Project environment-file storage is not a regular non-link file: {path.name}"
            )
        try:
            metadata = path.lstat()
        except OSError as error:
            raise InstallError(
                f"Project environment-file storage changed during inspection: {path.name}"
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise InstallError(
                f"Project environment-file storage is not a regular non-link file: {path.name}"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in path.name):
            raise InstallError("Project environment-file storage has an unsafe filename.")
        try:
            path.name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise InstallError("Project environment-file storage has a non-UTF-8 filename.") from error
        discovered[path.name] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    if not discovered:
        return

    try:
        process = subprocess.run(
            [
                str(git_executable),
                "--literal-pathspecs",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                *discovered,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_isolated_git_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("Project environment-file Git state could not be inspected safely.") from error
    if process.returncode != 0 or process.stderr:
        raise InstallError("Project environment-file Git state could not be inspected safely.")
    if process.stdout and not process.stdout.endswith(b"\0"):
        raise InstallError("Project environment-file Git state is invalid.")

    records = process.stdout[:-1].split(b"\0") if process.stdout else []
    try:
        safe_paths = [record.decode("utf-8", errors="strict") for record in records]
    except UnicodeDecodeError as error:
        raise InstallError("Project environment-file Git state is not valid UTF-8.") from error
    if len(safe_paths) != len(set(safe_paths)) or any(path not in discovered for path in safe_paths):
        raise InstallError("Project environment-file Git state is invalid.")

    for name, expected_identity in discovered.items():
        path = root / name
        if _is_linklike(path):
            raise InstallError(f"Project environment-file storage changed during inspection: {name}")
        try:
            metadata = path.lstat()
        except OSError as error:
            raise InstallError(
                f"Project environment-file storage changed during inspection: {name}"
            ) from error
        observed_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        if observed_identity != expected_identity or not stat.S_ISREG(metadata.st_mode):
            raise InstallError(f"Project environment-file storage changed during inspection: {name}")

    unsafe_paths = sorted(set(discovered) - set(safe_paths), key=str.casefold)
    if unsafe_paths:
        joined = ", ".join(unsafe_paths)
        raise InstallError(
            f"Project environment-file storage is not both Git-untracked and ignored: {joined}"
        )
    safe_paths.sort(key=str.casefold)
    if len(safe_paths) > 1:
        raise InstallError(
            "Multiple Git-ignored project environment files require an explicit user choice: "
            + ", ".join(safe_paths)
        )
    raise InstallError(
        "A Git-ignored project environment file is available; stop installation, validate the key "
        f"with the documented validation-only flow, then ask the user to store {token_env} in {safe_paths[0]}"
    )


def _validated_base_url(value: str) -> SplitResult:
    raw = value.strip()
    if any(
        character in {'"', "\\", "\u2028", "\u2029"}
        or character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in raw
    ):
        raise InstallError("The API base URL contains characters that cannot be represented safely in every client config.")
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError as error:
        raise InstallError("The API base URL contains a malformed host or port.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InstallError("The API base URL must be an absolute HTTP or HTTPS URL.")
    if parsed.netloc.endswith(":") or (parsed_port is not None and not 1 <= parsed_port <= 65535):
        raise InstallError("The API base URL contains a malformed host or port.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InstallError("The API base URL must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise InstallError("The API base URL must be an HTTPS origin without a path.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise InstallError("Use HTTPS unless the API host is local loopback.")
    return parsed._replace(path="")


def _endpoint(base: SplitResult, suffix: str) -> str:
    prefix = base.path.rstrip("/")
    return urlunsplit((base.scheme, base.netloc, f"{prefix}/{suffix.lstrip('/')}", "", ""))


def _read_source(relative: str) -> bytes:
    source_relative = _validate_relative(relative)
    path = PACKAGE_ROOT.joinpath(*source_relative.parts)
    try:
        path.resolve(strict=False).relative_to(PACKAGE_ROOT)
    except ValueError as exc:
        raise InstallError("A package source would leave the skill package.") from exc
    if _is_linklike(path) or not path.is_file():
        raise InstallError(f"Required package source is unavailable: {relative}")
    if path.stat().st_size > MAX_SOURCE_FILE_SIZE:
        raise InstallError(f"Package source is unexpectedly large: {relative}")
    return path.read_bytes()


def _read_text(relative: str) -> str:
    try:
        return _read_source(relative).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"Required package source is not UTF-8: {relative}") from exc


def _package_manifest() -> dict[str, Any]:
    try:
        value = json.loads(_read_text("config/package-manifest.json"))
    except json.JSONDecodeError as exc:
        raise InstallError("The package manifest is invalid JSON.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("skill"), dict):
        raise InstallError("The package manifest is missing skill metadata.")
    return value


def _client_registry() -> dict[str, Any]:
    try:
        value = json.loads(_read_text(CLIENT_REGISTRY_PATH))
    except json.JSONDecodeError as exc:
        raise InstallError("The client provider registry is invalid JSON.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise InstallError("The client provider registry has an unsupported schema.")
    reviewed_on = value.get("capabilities_reviewed_on")
    try:
        if not isinstance(reviewed_on, str) or date.fromisoformat(reviewed_on).isoformat() != reviewed_on:
            raise ValueError
    except ValueError as exc:
        raise InstallError("The client provider registry has an invalid capability review date.") from exc
    clients = value.get("clients")
    if not isinstance(clients, list) or not clients:
        raise InstallError("The client provider registry does not contain client profiles.")

    identifiers: set[str] = set()
    for profile in clients:
        if not isinstance(profile, dict):
            raise InstallError("The client provider registry contains an invalid profile.")
        client = profile.get("id")
        if not isinstance(client, str) or CLIENT_ID_PATTERN.fullmatch(client) is None or client in identifiers:
            raise InstallError("The client provider registry contains an invalid or duplicate client ID.")
        identifiers.add(client)
        if not isinstance(profile.get("display_name"), str) or not profile["display_name"].strip():
            raise InstallError(f"The client profile has no display name: {client}")
        if not isinstance(profile.get("reference_build"), str) or not profile["reference_build"].strip():
            raise InstallError(f"The client profile has no reference build: {client}")
        minimum_build = profile.get("minimum_build")
        if minimum_build is not None and (not isinstance(minimum_build, str) or not minimum_build.strip()):
            raise InstallError(f"The client profile has an invalid minimum build: {client}")
        install_supported = profile.get("install_supported")
        unsupported_reason = profile.get("unsupported_reason")
        if type(install_supported) is not bool:
            raise InstallError(f"The client profile has no explicit installation support status: {client}")
        if install_supported and unsupported_reason is not None:
            raise InstallError(f"The supported client profile has an unsupported reason: {client}")
        if not install_supported and (
            not isinstance(unsupported_reason, str) or not unsupported_reason.strip()
        ):
            raise InstallError(f"The unsupported client profile has no reason: {client}")

        project = profile.get("project_layout")
        if not isinstance(project, dict):
            raise InstallError(f"The client profile has no project layout: {client}")
        for field in ("skill_directory", "instruction_file", "instruction_source"):
            relative = project.get(field)
            if not isinstance(relative, str):
                raise InstallError(f"The client project layout is missing {field}: {client}")
            _validate_relative(relative)
        _read_source(project["instruction_source"])

        user_config = profile.get("user_config")
        if not isinstance(user_config, dict):
            raise InstallError(f"The client profile has no user configuration layout: {client}")
        default_directory = user_config.get("default_directory")
        if not isinstance(default_directory, str):
            raise InstallError(f"The client profile has no default configuration directory: {client}")
        _validate_relative(default_directory)
        for field in ("settings", "mcp"):
            target = user_config.get(field)
            if not isinstance(target, dict) or target.get("base") not in {"client_config", "client_config_parent"}:
                raise InstallError(f"The client profile has an invalid {field} configuration target: {client}")
            relative = target.get("path")
            if not isinstance(relative, str):
                raise InstallError(f"The client profile has no {field} configuration path: {client}")
            _validate_relative(relative)

        templates = profile.get("templates")
        hooks = templates.get("hooks") if isinstance(templates, dict) else None
        mcp = templates.get("mcp") if isinstance(templates, dict) else None
        if not isinstance(hooks, dict) or not isinstance(hooks.get("default"), str):
            raise InstallError(f"The client profile has no default hook template: {client}")
        hook_templates = [hooks["default"]]
        platform_overrides = hooks.get("platform_overrides", {})
        if not isinstance(platform_overrides, dict) or any(
            platform not in {"windows", "posix"} or not isinstance(relative, str)
            for platform, relative in platform_overrides.items()
        ):
            raise InstallError(f"The client profile has invalid hook template overrides: {client}")
        hook_templates.extend(platform_overrides.values())
        for relative in hook_templates:
            _validate_relative(relative)
            _read_source(relative)
        if (
            not isinstance(mcp, dict)
            or not isinstance(mcp.get("path"), str)
            or mcp.get("renderer")
            not in {"codex_toml", "claude_json", "gemini_json", "antigravity_stdio_json"}
        ):
            raise InstallError(f"The client profile has an invalid MCP template: {client}")
        _validate_relative(mcp["path"])
        _read_source(mcp["path"])

        runtime_adapters = profile.get("runtime_adapters")
        if not isinstance(runtime_adapters, list) or not runtime_adapters or any(
            not isinstance(relative, str) for relative in runtime_adapters
        ):
            raise InstallError(f"The client profile has invalid runtime adapters: {client}")
        for relative in runtime_adapters:
            _validate_relative(relative)
            _read_source(relative)

        lifecycle = profile.get("lifecycle")
        if not isinstance(lifecycle, dict):
            raise InstallError(f"The client profile has no lifecycle: {client}")
        baseline_events = lifecycle.get("baseline_events")
        if not isinstance(baseline_events, list) or not baseline_events or any(
            not isinstance(event, str) or not event for event in baseline_events
        ):
            raise InstallError(f"The client profile has invalid baseline events: {client}")
        for field in ("completion_event", "update_check_event"):
            if not isinstance(lifecycle.get(field), str) or not lifecycle[field]:
                raise InstallError(f"The client profile has no {field}: {client}")

        discovery_checks = profile.get("discovery_checks")
        if not isinstance(discovery_checks, list) or not discovery_checks or any(
            not isinstance(check, str) or not check for check in discovery_checks
        ):
            raise InstallError(f"The client profile has invalid discovery checks: {client}")

        official_docs = profile.get("official_docs")
        if not isinstance(official_docs, dict) or set(official_docs) != {"skills", "hooks", "mcp", "configuration"}:
            raise InstallError(f"The client profile has incomplete official documentation links: {client}")
        if any(
            not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).netloc
            for url in official_docs.values()
        ):
            raise InstallError(f"The client profile has an invalid official documentation URL: {client}")
    return value


def _client_profiles() -> dict[str, dict[str, Any]]:
    return {profile["id"]: profile for profile in _client_registry()["clients"]}


def _client_names() -> tuple[str, ...]:
    return tuple(_client_profiles())


def _client_profile(client: str) -> dict[str, Any]:
    try:
        return _client_profiles()[client]
    except KeyError as exc:
        raise InstallError(f"Unsupported client: {client}") from exc


def _assert_client_install_supported(client: str) -> None:
    profile = _client_profile(client)
    if profile["install_supported"] is True:
        return
    reason = profile["unsupported_reason"]
    raise InstallError(
        f"{profile['display_name']} is not supported for new preview, plan, apply, reconnect, or upgrade. "
        f"{reason} Existing installations remain available only to status, rollback-plan, and rollback."
    )


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    value = environ.get(name)
    if value is None:
        return False
    return str(value).strip().casefold() not in FALSEY_ENV_VALUES


def _is_client_marker(path: Path) -> bool:
    return path.exists() and path.is_dir() and not _is_linklike(path)


def _detect_client(
    *,
    explicit: str | None,
    target_root: Path | None,
    environ: Mapping[str, str],
) -> str:
    supported = ", ".join(_client_names())
    if explicit:
        if explicit not in _client_names():
            raise InstallError(f"Unsupported client: {explicit}. Pass --client with one of: {supported}.")
        return explicit

    env_hits = [
        client
        for client, names in CLIENT_ENV_SIGNALS.items()
        if any(_env_flag(environ, name) for name in names)
    ]
    if len(env_hits) == 1:
        return env_hits[0]
    if len(env_hits) > 1:
        raise InstallError(
            "The agent environment identifies multiple clients "
            f"({', '.join(env_hits)}). Pass --client with exactly one of: {supported}."
        )

    marker_hits: list[str] = []
    if target_root is not None:
        marker_hits = [
            client
            for client, names in CLIENT_MARKER_DIRECTORIES.items()
            if any(_is_client_marker(target_root / name) for name in names)
        ]
    if len(marker_hits) == 1:
        return marker_hits[0]
    if len(marker_hits) > 1:
        raise InstallError(
            "The target worktree has markers for multiple clients "
            f"({', '.join(marker_hits)}). Pass --client with exactly one of: {supported}."
        )
    raise InstallError(
        "Unable to detect the coding-agent client. Pass --client with one of: " + supported + "."
    )


def _source_mode(relative: str) -> int:
    return 0o755 if relative.endswith(".py") else 0o644


def _iter_source_files(excluded_files: set[str]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    for source in sorted(PACKAGE_ROOT.rglob("*"), key=lambda item: item.relative_to(PACKAGE_ROOT).as_posix()):
        relative = source.relative_to(PACKAGE_ROOT).as_posix()
        parts = PurePosixPath(relative).parts
        if any(part in EPHEMERAL_PARTS for part in parts):
            continue
        if source.suffix in EPHEMERAL_SUFFIXES or relative in excluded_files:
            continue
        if _is_linklike(source):
            raise InstallError(f"The skill package contains an unsupported symlink: {relative}")
        if not source.is_file():
            continue
        folded = relative.casefold()
        if folded in casefolded and casefolded[folded] != relative:
            raise InstallError(f"The skill package contains case-colliding paths: {casefolded[folded]} and {relative}")
        casefolded[folded] = relative
        if source.stat().st_size > MAX_SOURCE_FILE_SIZE:
            raise InstallError(f"Package source is unexpectedly large: {relative}")
        files.append(
            {
                "source": relative,
                "sha256": _sha256_file(source),
                "mode": format(_source_mode(relative), "04o"),
            }
        )
    if not files:
        raise InstallError("The skill package does not contain installable files.")
    return files


def _iter_release_identity_files() -> list[dict[str, Any]]:
    return _iter_source_files(RELEASE_IDENTITY_EXCLUDED_FILES)


def _iter_package_files() -> list[dict[str, Any]]:
    return _iter_source_files(INSTALL_EXCLUDED_FILES)


def _iter_skill_files() -> list[dict[str, Any]]:
    files = [
        entry
        for entry in _iter_package_files()
        if entry["source"] in SKILL_FILES
        or PurePosixPath(entry["source"]).parts[0] in SKILL_DIRECTORIES
    ]
    if not any(entry["source"] == "SKILL.md" for entry in files):
        raise InstallError("The installable skill payload is missing SKILL.md.")
    return files


def _package_source_tree_sha256(files: list[dict[str, Any]]) -> str:
    public_files: list[dict[str, Any]] = []
    for entry in files:
        body = _read_source(entry["source"])
        if _sha256_bytes(body) != entry["sha256"]:
            raise InstallError("The skill package changed while its release identity was being calculated.")
        public_files.append(
            {
                "path": entry["source"],
                "archive_path": f"{PACKAGE_IDENTITY}/{entry['source']}",
                "size": len(body),
                "mode": entry["mode"],
                "sha256": entry["sha256"],
            }
        )
    return _sha256_bytes(_canonical_json_bytes(public_files))


def _client_layout(client: str) -> dict[str, str]:
    project = _client_profile(client)["project_layout"]
    return {
        "skill": project["skill_directory"],
        "instruction": project["instruction_file"],
        "instruction_source": project["instruction_source"],
    }


def _client_user_layout(root: Path, client: str, override: str | None) -> dict[str, Path]:
    profile = _client_profile(client)
    user_config = profile["user_config"]
    default_parts = _validate_relative(user_config["default_directory"]).parts
    config_directory = _validate_external_path(
        Path(override) if override else Path.home().joinpath(*default_parts),
        "Client configuration directory",
    )
    _assert_safe_user_path_ancestor_chain(config_directory, "Client configuration directory")
    untrusted_root = _enclosing_worktree_root(root)
    try:
        config_directory.relative_to(untrusted_root)
    except ValueError:
        pass
    else:
        raise InstallError("The client configuration directory must be outside the enclosing repository worktree.")
    def resolve_target(target: dict[str, str]) -> Path:
        base = config_directory if target["base"] == "client_config" else config_directory.parent
        return base.joinpath(*_validate_relative(target["path"]).parts)

    settings = resolve_target(user_config["settings"])
    mcp = resolve_target(user_config["mcp"])
    for path in {settings, mcp}:
        if _is_linklike(path) or (path.exists() and not path.is_file()):
            raise InstallError(f"Client configuration target is not a regular file: {path}")
        if path.exists():
            _assert_safe_executable_ancestor_chain(path, "Client configuration file")
    return {"directory": config_directory, "settings": settings, "mcp": mcp}


def _resolved_platform(value: str) -> str:
    if value == "auto":
        return "windows" if os.name == "nt" else "posix"
    if value not in {"windows", "posix"}:
        raise InstallError(f"Unsupported platform: {value}")
    return value


def _replace_json_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        return value
    if isinstance(value, list):
        return [_replace_json_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            _replace_json_strings(key, replacements): _replace_json_strings(child, replacements)
            for key, child in value.items()
        }
    return value


def _load_json_source(relative: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(relative))
    except json.JSONDecodeError as exc:
        raise InstallError(f"The package template is invalid JSON: {relative}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"The package template must contain a JSON object: {relative}")
    return value


def _antigravity_windows_hook_command(command: str) -> str:
    powershell_path = _windows_system_tool(
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    ).as_posix()
    if re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9._/-]+", powershell_path) is None:
        raise InstallError(
            "The trusted Windows PowerShell path cannot be safely embedded in an Antigravity hook command."
        )
    payload = (
        f"& {command}\n"
        "$acceptoraExitCode = $LASTEXITCODE\n"
        "if ($null -eq $acceptoraExitCode) { exit 1 }\n"
        "exit $acceptoraExitCode"
    )
    encoded_payload = base64.b64encode(payload.encode("utf-16le")).decode("ascii")
    return (
        f"{powershell_path} -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -EncodedCommand {encoded_payload}"
    )


def _encode_antigravity_windows_hooks(value: Any, runtime_path: str) -> int:
    if isinstance(value, list):
        return sum(_encode_antigravity_windows_hooks(child, runtime_path) for child in value)
    if not isinstance(value, dict):
        return 0
    encoded = 0
    if value.get("type") == "command":
        command = value.get("command")
        if not isinstance(command, str) or runtime_path not in command:
            raise InstallError("The Antigravity hook template contains an unmanaged command.")
        value["command"] = _antigravity_windows_hook_command(command)
        encoded += 1
    for child in value.values():
        encoded += _encode_antigravity_windows_hooks(child, runtime_path)
    return encoded


def _render_hooks(
    client: str,
    platform: str,
    runtime_root: Path,
    python_executable: Path,
) -> tuple[str, dict[str, Any]]:
    runtime_path = runtime_root.as_posix()
    python_command = f'"{python_executable.as_posix()}" -B -I'
    if client == "codex" and platform == "windows":
        python_command = f'& "{python_executable.as_posix()}" -B -I'
    forbidden = {'"', "\r", "\n", "\x00", "$", "`", "%", "!", "\\"}
    if any(character in runtime_path for character in forbidden):
        raise InstallError("The target path contains characters that cannot be safely embedded in a client hook command.")
    managed_hook_marker = f"acceptora-target:{runtime_root.name}"
    common_replacements = {
        "{{PYTHON_COMMAND}}": python_command,
        "{{RUNTIME_ROOT}}": runtime_path,
        "{{RUNTIME_ID}}": runtime_root.name,
    }
    hooks = _client_profile(client)["templates"]["hooks"]
    relative = hooks.get("platform_overrides", {}).get(platform, hooks["default"])
    template = _load_json_source(relative)
    replacements = common_replacements
    rendered = _replace_json_strings(template, replacements)

    def mark_managed_hooks(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                mark_managed_hooks(child)
            return
        if not isinstance(value, dict):
            return
        command = value.get("command")
        if value.get("type") == "command" and isinstance(command, str) and runtime_path in command:
            field = "statusMessage" if isinstance(value.get("statusMessage"), str) else "description"
            current = value.get(field)
            if not isinstance(current, str) or not current:
                raise InstallError(f"The hook template has no supported managed marker field: {relative}")
            value[field] = f"{current} [{managed_hook_marker}]"
        for child in value.values():
            mark_managed_hooks(child)

    if client == "antigravity-cli":
        if _managed_hook_identities(rendered) != {runtime_root.name}:
            raise InstallError(f"The Antigravity hook template has no unique managed identity: {relative}")
    else:
        mark_managed_hooks(rendered)
    serialized = json.dumps(rendered, ensure_ascii=False)
    if re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", serialized):
        raise InstallError(f"The hook template contains an unresolved installer placeholder: {relative}")
    if re.search(r"(?:^|[\\s\"'])python3(?:[\\s\"']|$)|(?:^|[\\s\"'])py -3(?:[\\s\"']|$)", serialized):
        raise InstallError(f"The hook template contains an unpinned Python command: {relative}")
    if client == "antigravity-cli" and platform == "windows":
        if _encode_antigravity_windows_hooks(rendered, runtime_path) == 0:
            raise InstallError(f"The Antigravity hook template has no managed commands: {relative}")
    return relative, rendered


def _render_project_config(
    project_id: str,
) -> dict[str, Any]:
    hints = _load_json_source("config/project.example.json")
    allowed = {
        "version",
        "feature_id",
        "source_adapter",
        "ignored_paths",
        "offline_outbox",
        "processed_outbox",
    }
    project = {key: copy.deepcopy(value) for key, value in hints.items() if key in allowed}
    project.update({"config_role": "non_authoritative_project_hints", "project_id": project_id})
    return project


def _toml_managed_markers(server_alias: str) -> tuple[str, str]:
    if re.fullmatch(r"acceptora-[a-f0-9]{12}", server_alias) is None:
        raise InstallError("The Codex MCP server alias is invalid.")
    return (
        f"# acceptora-verification:{server_alias}:start",
        f"# acceptora-verification:{server_alias}:end",
    )


def _pinned_hook_runtime(token_env: str) -> str:
    source = _read_text("adapters/hook_runtime.py").rstrip() + "\n"
    repository_config_loader = '''def _config_path(root: Path) -> Path:
    override = os.environ.get("ACCEPTORA_VERIFICATION_CONFIG")
    return Path(override).expanduser().resolve() if override else root / CONFIG_RELATIVE_PATH
'''
    pinned_config_loader = '''def _config_path(root: Path) -> Path:
    return SKILL_ROOT / "config" / "runtime-config.json"
'''
    if source.count(repository_config_loader) != 1:
        raise InstallError("The hook runtime repository-config loader cannot be pinned safely.")
    source = source.replace(repository_config_loader, pinned_config_loader, 1)
    repository_token_loader = '''def _configured_token_value(config: dict[str, Any]) -> str | None:
    token_env = config.get("token_env")
    if not isinstance(token_env, str) or TOKEN_ENV_PATTERN.fullmatch(token_env) is None:
        return None
    token = os.environ.get(token_env)
    return token if isinstance(token, str) else None
'''
    pinned_token_loader = f'''def _configured_token_value(config: dict[str, Any]) -> str | None:
    token_env = config.get("token_env")
    if token_env != "{token_env}":
        return None
    token = os.environ.get("{token_env}", "")
    return token if re.fullmatch(r"{ACCEPTORA_TOKEN_PATTERN}", token) is not None else None
'''
    if source.count(repository_token_loader) != 1:
        raise InstallError("The hook runtime credential loader cannot be pinned safely.")
    source = source.replace(repository_token_loader, pinned_token_loader, 1)
    override = r'''

# Installed runtime policy. This copy lives outside the target repository and
# intentionally ignores repository-controlled configuration.
_PINNED_CONFIG_PATH = SKILL_ROOT / "config" / "runtime-config.json"
_PINNED_STATE_ROOT = SKILL_ROOT / "state"


def _config_path(root: Path) -> Path:
    return _PINNED_CONFIG_PATH


def _project_root(event: dict[str, Any]) -> Path:
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise HookRuntimeError("hook input does not contain cwd")
    # Load installer-owned policy before invoking Git or examining repository
    # configuration. The supplied root argument is intentionally ignored by
    # load_config in this pinned runtime.
    config = load_config(Path(cwd))
    target_value = config.get("target_root")
    if not isinstance(target_value, str) or not target_value:
        raise HookRuntimeError("pinned runtime target_root is invalid")
    expected = Path(target_value).resolve()
    observed_cwd = Path(cwd).resolve()
    try:
        observed_cwd.relative_to(expected)
    except ValueError as error:
        raise HookRuntimeError("hook cwd does not match the pinned target repository") from error
    observed = find_repository_root(expected).resolve()
    if os.path.normcase(str(observed)) != os.path.normcase(str(expected)):
        raise HookRuntimeError("hook cwd does not match the pinned target repository")
    return observed


def _state_paths(root: Path, event: dict[str, Any]) -> tuple[Path, Path]:
    session = _safe_key(event.get("session_id"))
    return _PINNED_STATE_ROOT / f"{session}.baseline.json", _PINNED_STATE_ROOT / f"{session}.loop.json"


def _pending_path(root: Path) -> Path:
    return _PINNED_STATE_ROOT / "pending-sync.json"


def _skill_update_cache_path(root: Path) -> Path:
    return _PINNED_STATE_ROOT / SKILL_UPDATE_CACHE_FILENAME


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
    for origin_session in origins:
        _cleanup(
            (
                _PINNED_STATE_ROOT / f"{origin_session}.baseline.json",
                _PINNED_STATE_ROOT / f"{origin_session}.loop.json",
            )
        )
    if pending_path.exists():
        pending_path.unlink()
'''
    return source + override.lstrip("\n")


def _pinned_manifest_builder(git_executable: Path) -> str:
    source = _read_text("scripts/build_source_manifest.py")
    marker = 'SCHEMA_VERSION = "1.0"'
    invocation = '["git", "-c", "core.fsmonitor=false", "-C", str(root), *args]'
    if source.count(marker) != 1 or source.count(invocation) != 1:
        raise InstallError("The source-manifest helper cannot be pinned to the reviewed Git executable.")
    source = source.replace(marker, f'{marker}\nPINNED_GIT_EXECUTABLE = {json.dumps(git_executable.as_posix())}', 1)
    return source.replace(
        invocation,
        '[PINNED_GIT_EXECUTABLE, "-c", "core.fsmonitor=false", "-C", str(root), *args]',
        1,
    )


def _assert_strict_source_capture(root: Path, git_executable: Path) -> None:
    module_path = PACKAGE_ROOT / "scripts" / "build_source_manifest.py"
    namespace: dict[str, Any] = {
        "__file__": str(module_path),
        "__name__": "acceptora_installer_strict_source_preflight",
    }
    try:
        exec(compile(_pinned_manifest_builder(git_executable), str(module_path), "exec"), namespace)
        namespace["capture_snapshot"](root, "git")
    except Exception as error:
        raise InstallError(f"The repository is not eligible for strict Git source capture: {error}") from error


def _hook_dispatcher(adapter_relative: str) -> str:
    adapter = json.dumps(adapter_relative)
    return f'''#!/usr/bin/env python3
"""Installer-owned global hook dispatcher; no-op outside its pinned target."""

from __future__ import annotations

import io
import json
import os
import runpy
import sys
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = RUNTIME_ROOT / "config" / "runtime-config.json"
ADAPTER_RELATIVE = {adapter}


def _neutral() -> int:
    sys.stdout.write("{{}}\\n")
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cwd_value = event.get("cwd") if isinstance(event, dict) else None
        target_value = config.get("target_root") if isinstance(config, dict) else None
        if not isinstance(cwd_value, str) or not isinstance(target_value, str):
            return _neutral()
        cwd = Path(cwd_value).resolve()
        target = Path(target_value).resolve()
        try:
            cwd.relative_to(target)
        except ValueError:
            return _neutral()
        sys.stdin = io.StringIO(json.dumps(event, ensure_ascii=False))
        adapter_path = RUNTIME_ROOT / "trusted_adapters" / ADAPTER_RELATIVE
        runpy.run_path(str(adapter_path), run_name="__main__")
        return 0
    except SystemExit as error:
        return int(error.code or 0)
    except Exception as error:
        sys.stdout.write(json.dumps({{"systemMessage": f"Agent Verification dispatcher warning: {{error}}"}}) + "\\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _generated_file(destination: str, content: str, mode: int = 0o644) -> dict[str, Any]:
    body = content.encode("utf-8")
    return {
        "destination": destination,
        "content": content,
        "sha256": _sha256_bytes(body),
        "mode": format(mode, "04o"),
    }


def _source_file(source: str, destination: str | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "destination": destination or source,
        "sha256": _sha256_bytes(_read_source(source)),
        "mode": format(_source_mode(destination or source), "04o"),
    }


def _runtime_files(client: str, config: dict[str, Any], git_executable: Path) -> list[dict[str, Any]]:
    adapter_files = tuple(_client_profile(client)["runtime_adapters"])
    files_by_destination = {
        f"{RUNTIME_PACKAGE_DIRECTORY}/{entry['source']}": {
            **entry,
            "destination": f"{RUNTIME_PACKAGE_DIRECTORY}/{entry['source']}",
        }
        for entry in _iter_release_identity_files()
    }
    generated = [
        _generated_file("trusted_adapters/hook_runtime.py", _pinned_hook_runtime(config["token_env"]), 0o755),
        _generated_file("scripts/build_source_manifest.py", _pinned_manifest_builder(git_executable), 0o755),
        _source_file("scripts/read_instruction_snapshot.py"),
        _source_file("scripts/validate_checklist_payload.py"),
        _source_file("scripts/validate_gate_response.py"),
        _source_file("config/package-manifest.json"),
        _source_file(CLIENT_REGISTRY_PATH),
        _generated_file("config/runtime-config.json", _json_text(config)),
        *(
            _source_file(path, f"trusted_adapters/{path.removeprefix('adapters/')}")
            for path in adapter_files
        ),
        *(
            _generated_file(path, _hook_dispatcher(path.removeprefix("adapters/")), 0o755)
            for path in adapter_files
        ),
    ]
    for entry in generated:
        files_by_destination[entry["destination"]] = entry
    files = list(files_by_destination.values())
    for entry in files:
        entry["mode"] = "0700" if int(entry["mode"], 8) & 0o111 else "0600"
    destinations = [entry["destination"] for entry in files]
    if len(destinations) != len(set(destinations)):
        raise InstallError("The external runtime contains duplicate destinations.")
    return sorted(files, key=lambda entry: entry["destination"])


def _render_mcp_config(
    client: str,
    token_env: str,
    mcp_url: str,
    server_alias: str,
    runtime_root: Path | None = None,
    python_executable: Path | None = None,
) -> tuple[str, str | dict[str, Any]]:
    mcp_profile = _client_profile(client)["templates"]["mcp"]
    relative = mcp_profile["path"]
    renderer = mcp_profile["renderer"]
    if renderer == "codex_toml":
        rendered = _read_text(relative)
        rendered = rendered.replace('"https://verify.example.test/mcp"', json.dumps(mcp_url, ensure_ascii=False))
        rendered = rendered.replace("ACCEPTORA_AGENT_TOKEN_PROJ_REPLACE_WITH_PROJECT_ULID", token_env)
        rendered = rendered.replace("[mcp_servers.acceptora]", f'[mcp_servers."{server_alias}"]')
        return relative, rendered.rstrip() + "\n"

    config = _load_json_source(relative)
    if renderer == "antigravity_stdio_json":
        if runtime_root is None or python_executable is None:
            raise InstallError("Antigravity MCP rendering requires the pinned runtime and Python executable.")
        config = _replace_json_strings(
            config,
            {
                "{{PYTHON_EXECUTABLE}}": _normal_path(python_executable),
                "{{RUNTIME_ROOT}}": runtime_root.as_posix(),
                "ACCEPTORA_AGENT_TOKEN_PROJ_REPLACE_WITH_PROJECT_ULID": token_env,
                "https://verify.example.test/mcp": mcp_url,
            },
        )
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get("acceptora"), dict):
        raise InstallError(f"The MCP template is missing mcpServers.acceptora: {relative}")
    server = servers["acceptora"]
    if renderer == "antigravity_stdio_json":
        server = copy.deepcopy(server)
    elif renderer == "gemini_json":
        server.pop("type", None)
        server.pop("url", None)
        server["httpUrl"] = mcp_url
        server["trust"] = False
    elif renderer == "claude_json":
        server.pop("httpUrl", None)
        server.pop("trust", None)
        server["url"] = mcp_url
    else:
        raise InstallError(f"Unsupported MCP renderer: {renderer}")
    if renderer != "antigravity_stdio_json":
        headers = server.setdefault("headers", {})
        if not isinstance(headers, dict):
            raise InstallError(f"The MCP template has invalid headers: {relative}")
        headers["Authorization"] = f"Bearer ${{{token_env}}}"
    return relative, {"mcpServers": {server_alias: server}}


def _contains_managed_reference(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    adapter_markers = tuple(
        f"/{relative.lower()}"
        for profile in _client_profiles().values()
        for relative in profile["runtime_adapters"]
    )
    return any(
        marker in text
        for marker in (
            "acceptora",
            "verify-generated-work",
            "capturing verification baseline",
            "manual-verification synchronization",
            *adapter_markers,
        )
    )


def _managed_hook_identities(value: Any) -> set[str]:
    return set(MANAGED_HOOK_ID_PATTERN.findall(json.dumps(value, ensure_ascii=False, sort_keys=True).lower()))


def _is_managed_hook_group_path(path: tuple[str, ...] | list[str]) -> bool:
    return (
        len(path) == 1
        and MANAGED_HOOK_ID_PATTERN.fullmatch(path[0].casefold()) is not None
    )


def _is_mcp_server_path(path: tuple[str, ...] | list[str]) -> bool:
    return len(path) == 2 and path[0] == "mcpServers"


def _merge_json(base: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged = copy.deepcopy(base)
    inverse: list[dict[str, Any]] = []

    def merge(target: dict[str, Any], desired: dict[str, Any], path: list[str]) -> None:
        for key in sorted(desired):
            value = desired[key]
            child_path = [*path, key]
            if key not in target:
                target[key] = copy.deepcopy(value)
                inverse.append({"kind": "remove_key", "path": child_path, "value": copy.deepcopy(value)})
                continue
            current = target[key]
            if _is_managed_hook_group_path(child_path):
                if not _json_equal(current, value):
                    raise InstallError(
                        f"Existing JSON setting has an ambiguous managed hook reference at {'.'.join(child_path)}"
                    )
                continue
            if _is_mcp_server_path(child_path):
                if not _json_equal(current, value):
                    raise InstallError(
                        f"Existing JSON setting conflicts with the managed MCP server at {'.'.join(child_path)}"
                    )
                continue
            if isinstance(current, dict) and isinstance(value, dict):
                merge(current, value, child_path)
                continue
            if isinstance(current, list) and isinstance(value, list):
                desired_managed = [item for item in value if _contains_managed_reference(item)]
                existing_managed = [item for item in current if _contains_managed_reference(item)]
                desired_identities = set().union(*(_managed_hook_identities(item) for item in desired_managed)) if desired_managed else set()
                if desired_managed and len(desired_identities) != 1:
                    raise InstallError(f"Managed hook identity is missing or ambiguous at {'.'.join(child_path)}")
                matching_existing = []
                has_legacy_managed = False
                for item in existing_managed:
                    identities = _managed_hook_identities(item)
                    if not identities:
                        has_legacy_managed = True
                    elif identities.intersection(desired_identities):
                        matching_existing.append(item)
                if desired_managed and (has_legacy_managed or matching_existing) and (
                    has_legacy_managed
                    or len(matching_existing) != len(desired_managed)
                    or any(
                        not any(_json_equal(item, desired_item) for desired_item in desired_managed)
                        for item in matching_existing
                    )
                ):
                    raise InstallError(
                        f"Existing JSON setting has an ambiguous managed hook reference at {'.'.join(child_path)}"
                    )
                for item in value:
                    if any(_json_equal(item, current_item) for current_item in current):
                        continue
                    current.append(copy.deepcopy(item))
                    inverse.append(
                        {"kind": "remove_list_value", "path": child_path, "value": copy.deepcopy(item)}
                    )
                continue
            if not _json_equal(current, value):
                raise InstallError(f"Existing JSON setting conflicts at {'.'.join(child_path)}")

    merge(merged, patch, [])
    return merged, inverse


def _external_json_rollback_changes(operation: dict[str, Any]) -> list[dict[str, Any]]:
    action = operation.get("action")
    if action == "no_change":
        return []
    if action == "merge":
        changes = operation.get("rollback_changes")
        if not isinstance(changes, list) or not changes:
            raise InstallError("The external JSON operation has no canonical rollback changes.")
        return copy.deepcopy(changes)
    if action != "create":
        raise InstallError("The external JSON operation has an unsupported rollback action.")
    desired = _external_json_owned_desired(operation.get("desired"))
    changes: list[dict[str, Any]] = []

    def collect(value: Any, path: list[str]) -> None:
        if _is_managed_hook_group_path(path) or _is_mcp_server_path(path):
            changes.append({"kind": "remove_key", "path": path, "value": copy.deepcopy(value)})
            return
        if isinstance(value, dict):
            for key in sorted(value):
                collect(value[key], [*path, key])
            return
        if isinstance(value, list):
            changes.extend(
                {"kind": "remove_list_value", "path": path, "value": copy.deepcopy(item)}
                for item in value
            )
            return
        changes.append({"kind": "remove_key", "path": path, "value": copy.deepcopy(value)})

    collect(desired, [])
    if not changes:
        raise InstallError("The external JSON operation has no canonical owned values.")
    return changes


def _target_state(path: Path) -> str:
    if not path.exists() and not _is_linklike(path):
        return "missing"
    if path.is_file() and not _is_linklike(path):
        return "file"
    return "non_file"


def _managed_block_action(path: Path) -> str:
    state = _target_state(path)
    if state == "missing":
        return "create_with_managed_block"
    if state != "file":
        return "manual_review_non_file"
    try:
        existing = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "manual_review_non_utf8"
    starts = existing.count(MANAGED_START)
    ends = existing.count(MANAGED_END)
    if starts == 0 and ends == 0:
        return "append_managed_block"
    if starts == 1 and ends == 1 and existing.index(MANAGED_START) < existing.index(MANAGED_END):
        return "replace_managed_block"
    return "manual_review_marker_mismatch"


def _gitignore_action(path: Path, block: str) -> str:
    state = _target_state(path)
    if state == "missing":
        return "create_with_lines"
    if state != "file":
        return "manual_review_non_file"
    try:
        existing_lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    except UnicodeDecodeError:
        return "manual_review_non_utf8"
    required = {line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    return "no_change" if required.issubset(existing_lines) else "append_missing_lines"


def _legacy_package_files(target_skill_root: Path) -> list[dict[str, str]]:
    return [
        {
            "source": entry["source"],
            "destination": _normal_path(target_skill_root / Path(entry["source"])),
            "sha256": entry["sha256"],
        }
        for entry in _iter_skill_files()
    ]


def build_preview(args: argparse.Namespace) -> dict[str, Any]:
    """Build the legacy, non-mutating preview document."""
    _assert_client_install_supported(args.client)
    root = _validated_root(args.target_root)
    _assert_worktree_boundary(root)
    platform = _resolved_platform(args.platform)
    token_env = _validate_inputs(args.project_id, args.token_env)
    base = _validated_base_url(args.api_base_url)
    mcp_url = _endpoint(base, "/mcp")
    contract_version_url = _endpoint(base, "/api/contract-version")
    completion_gate_url = _endpoint(base, "/api/v1/integrations/completion-gate")
    rest_base_url = _endpoint(base, "/api/v1/integrations")
    openapi_url = _endpoint(base, "/api/v1/integrations/openapi.json")
    layout = _client_layout(args.client)
    user_layout = _client_user_layout(root, args.client, getattr(args, "client_config_dir", None))
    target_skill_root = _safe_target(root, layout["skill"])
    runtime_root = _runtime_root(root, args.client, getattr(args, "runtime_base", None))
    python_executable = _resolved_python_executable(root, getattr(args, "python_executable", None))
    instruction_target = _safe_target(root, layout["instruction"])
    gitignore_target = _safe_target(root, ".gitignore")
    project_config_target = _safe_target(root, ".verification/config.json")
    settings_target = user_layout["settings"]
    mcp_target = user_layout["mcp"]
    instruction_block = _read_text(layout["instruction_source"])
    gitignore_block = _gitignore_content()
    hook_source, hooks = _render_hooks(args.client, platform, runtime_root, python_executable)
    server_alias = f"acceptora-{_runtime_identity(root, args.client)[:12]}"
    mcp_source, mcp = _render_mcp_config(
        args.client,
        token_env,
        mcp_url,
        server_alias,
        runtime_root,
        python_executable,
    )
    project = _render_project_config(args.project_id)
    hook_content = _json_text(hooks)
    mcp_content = mcp if isinstance(mcp, str) else _json_text(mcp)
    manual_merges = [
        {
            "target": _normal_path(project_config_target),
            "source": "config/project.example.json",
            "action": "manual_merge_required" if project_config_target.exists() else "manual_create",
            "content": _json_text(project),
        }
    ]
    if settings_target == mcp_target:
        combined, _ = _merge_json(hooks, mcp if isinstance(mcp, dict) else {})
        manual_merges.append(
            {
                "target": _normal_path(settings_target),
                "source": f"{hook_source} + {mcp_source}",
                "action": "manual_merge_and_trust_required" if settings_target.exists() else "manual_create_and_trust_required",
                "content": _json_text(combined),
            }
        )
    else:
        manual_merges.extend(
            [
                {
                    "target": _normal_path(mcp_target),
                    "source": mcp_source,
                    "action": "manual_merge_required" if mcp_target.exists() else "manual_create",
                    "content": mcp_content,
                },
                {
                    "target": _normal_path(settings_target),
                    "source": hook_source,
                    "action": "manual_merge_and_trust_required" if settings_target.exists() else "manual_create_and_trust_required",
                    "content": hook_content,
                },
            ]
        )
    return {
        "schema_version": 1,
        "preview_only": True,
        "mutations_performed": 0,
        "client": args.client,
        "platform": platform,
        "target_root": _normal_path(root),
        "token_env": token_env,
        "external_runtime": {
            "action": "manual_external_copy_review_required" if runtime_root.exists() else "manual_external_copy_create",
            "destination": _normal_path(runtime_root),
            "receipt": _normal_path(runtime_root / RUNTIME_RECEIPT),
            "trusted_installer": _normal_path(runtime_root / RUNTIME_PACKAGE_DIRECTORY / "scripts" / "install.py"),
        },
        "skill_copy": {
            "action": "manual_copy_review_required" if target_skill_root.exists() else "manual_copy_create",
            "destination": _normal_path(target_skill_root),
            "files": _legacy_package_files(target_skill_root),
        },
        "managed_blocks": [
            {
                "target": _normal_path(instruction_target),
                "source": layout["instruction_source"],
                "action": _managed_block_action(instruction_target),
                "start_marker": MANAGED_START,
                "end_marker": MANAGED_END,
                "content": instruction_block,
            },
            {
                "target": _normal_path(gitignore_target),
                "source": "snippets/gitignore.block",
                "action": _gitignore_action(gitignore_target, gitignore_block),
                "content": gitignore_block,
            },
        ],
        "manual_merges": manual_merges,
        "warnings": [
            "This command is preview-only and never creates, edits, copies, installs, or trusts files.",
            "Review and apply every listed copy, block, and merge manually.",
            "Executable hooks, pinned policy, state, and the receipt must remain in the external runtime.",
            f"Credential values must remain in {token_env} and must not be pasted into managed files.",
        ],
    }


def _text_preview(preview: dict[str, Any]) -> str:
    lines = [
        "INSTALLER PREVIEW ONLY - 0 mutations performed",
        f"Client: {preview['client']}",
        f"Platform: {preview['platform']}",
        f"Target: {preview['target_root']}",
        f"External runtime: {preview['external_runtime']['destination']}",
        f"Credential environment variable: {preview['token_env']}",
        "",
        f"Skill copy: {preview['skill_copy']['action']} -> {preview['skill_copy']['destination']}",
    ]
    for block in preview["managed_blocks"]:
        lines.extend(["", f"Managed block: {block['action']} -> {block['target']}", block["content"].rstrip()])
    for merge in preview["manual_merges"]:
        lines.extend(["", f"Manual merge: {merge['action']} -> {merge['target']}", merge["content"].rstrip()])
    lines.extend(["", "No files were changed."])
    return "\n".join(lines) + "\n"


def _project_token_env(project_id: str) -> str:
    if project_id != "proj_REPLACE_WITH_PROJECT_ULID" and not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise InstallError("The project ID must use the public proj_<ULID> form.")
    token_env = f"{PROJECT_TOKEN_ENV_PREFIX}{project_id.upper()}"
    if ENV_NAME_PATTERN.fullmatch(token_env) is None:
        raise InstallError("The project-derived token environment-variable name is invalid.")
    return token_env


def _validate_inputs(project_id: str, token_env: str | None) -> str:
    expected = _project_token_env(project_id)
    if token_env is not None and token_env != expected:
        raise InstallError(f"The hook runtime token environment variable is pinned to {expected}.")
    return expected


def _gitignore_content() -> str:
    source = _read_text("snippets/gitignore.block")
    lines = [line for line in source.splitlines() if line.strip()]
    return "\n".join(lines) + "\n"


def _append_block(existing: bytes, block: str) -> tuple[bytes, str]:
    if not existing:
        suffix = block if block.endswith("\n") else block + "\n"
    else:
        separator = "" if existing.endswith(b"\n") else "\n"
        suffix = separator + "\n" + block.strip() + "\n"
    return existing + suffix.encode("utf-8"), suffix


def _append_missing_lines(existing: bytes, content: str) -> tuple[bytes, str]:
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("A managed text destination is not UTF-8.") from exc
    present = {line.strip() for line in text.splitlines()}
    missing = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and line.strip() not in present
    ]
    if not missing:
        return existing, ""
    prefix = "" if not text or text.endswith("\n") else "\n"
    suffix = prefix + "\n".join(missing) + "\n"
    return existing + suffix.encode("utf-8"), suffix


def _plan_copy_tree(root: Path, destination: str, files: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    target = _safe_target(root, destination)
    operation = {
        "id": "skill-copy",
        "kind": "copy_tree",
        "target": destination,
        "files": files,
    }
    if not target.exists():
        operation["action"] = "create"
        return operation, None
    if _is_linklike(target) or not target.is_dir():
        operation["action"] = "conflict"
        return operation, "The skill destination already exists and is not a regular directory."
    expected = {entry["source"]: entry["sha256"] for entry in files}
    actual: dict[str, str] = {}
    for path in sorted(target.rglob("*")):
        if _is_linklike(path):
            operation["action"] = "conflict"
            return operation, "The existing skill destination contains a symlink."
        if path.is_file():
            actual[path.relative_to(target).as_posix()] = _sha256_file(path)
    if actual == expected:
        operation["action"] = "no_change"
        return operation, None
    operation["action"] = "conflict"
    return operation, "The skill destination already contains unmanaged or modified files; roll it back or choose a clean target."


def _planned_file_body(entry: dict[str, Any]) -> bytes:
    source = entry.get("source")
    content = entry.get("content")
    if isinstance(source, str) and content is None:
        return _read_source(source)
    if isinstance(content, str) and source is None:
        return content.encode("utf-8")
    raise InstallError("A planned runtime file must have exactly one canonical source.")


def _plan_external_runtime(runtime_root: Path, files: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    operation: dict[str, Any] = {
        "id": "external-runtime",
        "kind": "external_runtime",
        "target": _normal_path(runtime_root),
        "files": files,
    }
    if not runtime_root.exists():
        operation["action"] = "create"
        return operation, None
    if _is_linklike(runtime_root) or not runtime_root.is_dir():
        operation["action"] = "conflict"
        return operation, "The external runtime destination is not a regular directory."
    expected = {entry["destination"]: entry["sha256"] for entry in files}
    actual: dict[str, str] = {}
    for path in sorted(runtime_root.rglob("*")):
        if _is_linklike(path):
            operation["action"] = "conflict"
            return operation, "The external runtime destination contains a symlink."
        if path.is_file():
            actual[path.relative_to(runtime_root).as_posix()] = _sha256_file(path)
    if actual == expected:
        operation["action"] = "no_change"
        return operation, None
    operation["action"] = "conflict"
    return operation, "The external runtime already contains unmanaged or modified files."


def _plan_managed_text(
    root: Path,
    operation_id: str,
    target_relative: str,
    content: str,
    start_marker: str,
    end_marker: str,
    section_probe: str | None = None,
    *,
    target_path: Path | None = None,
    operation_kind: str = "managed_text",
) -> tuple[dict[str, Any], str | None]:
    target = target_path if target_path is not None else _safe_target(root, target_relative)
    target_label = _normal_path(target) if target_path is not None else target_relative
    before_hash = _file_hash(target)
    operation: dict[str, Any] = {
        "id": operation_id,
        "kind": operation_kind,
        "target": target_label,
        "content": content,
        "start_marker": start_marker,
        "end_marker": end_marker,
        "expected_before_sha256": before_hash,
    }
    if before_hash is None:
        after = content.encode("utf-8")
        operation.update({"action": "create", "expected_after_sha256": _sha256_bytes(after)})
        return operation, None
    try:
        existing = target.read_bytes()
        text = existing.decode("utf-8")
    except UnicodeDecodeError:
        operation["action"] = "conflict"
        return operation, "The managed text destination is not UTF-8."
    starts = text.count(start_marker)
    ends = text.count(end_marker)
    if starts == 0 and ends == 0:
        if section_probe and section_probe in text:
            operation["action"] = "conflict"
            return operation, f"An unmanaged {section_probe} section already exists."
        after, suffix = _append_block(existing, content)
        operation.update(
            {
                "action": "append",
                "append_text": suffix,
                "expected_after_sha256": _sha256_bytes(after),
            }
        )
        return operation, None
    if starts != 1 or ends != 1 or text.index(start_marker) > text.index(end_marker):
        operation["action"] = "conflict"
        return operation, "Managed markers are missing, duplicated, or out of order."
    beginning = text.index(start_marker)
    finish = text.index(end_marker, beginning) + len(end_marker)
    existing_block = text[beginning:finish].strip()
    if existing_block != content.strip():
        operation["action"] = "conflict"
        return operation, "A different managed block already exists; roll back that installation before replacing it."
    operation.update({"action": "no_change", "expected_after_sha256": before_hash})
    return operation, None


def _plan_codex_mcp(
    root: Path,
    target: Path,
    content: str,
    server_alias: str,
) -> tuple[dict[str, Any], str | None]:
    start_marker, end_marker = _toml_managed_markers(server_alias)
    state = _target_state(target)
    if state == "missing":
        return _plan_managed_text(
            root,
            "mcp-config",
            _normal_path(target),
            content,
            start_marker,
            end_marker,
            target_path=target,
            operation_kind="external_managed_text",
        )
    if state != "file":
        operation = {
            "id": "mcp-config",
            "kind": "external_managed_text",
            "target": _normal_path(target),
            "content": content,
            "start_marker": start_marker,
            "end_marker": end_marker,
            "expected_before_sha256": None,
            "action": "conflict",
        }
        return operation, "The Codex user configuration is not a regular file."
    try:
        existing_text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        operation, _ = _plan_managed_text(
            root,
            "mcp-config",
            _normal_path(target),
            content,
            start_marker,
            end_marker,
            target_path=target,
            operation_kind="external_managed_text",
        )
        operation["action"] = "conflict"
        return operation, "The Codex user configuration is not UTF-8."
    if start_marker in existing_text or end_marker in existing_text:
        return _plan_managed_text(
            root,
            "mcp-config",
            _normal_path(target),
            content,
            start_marker,
            end_marker,
            target_path=target,
            operation_kind="external_managed_text",
        )
    try:
        document = tomllib.loads(existing_text)
        desired_document = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        operation, _ = _plan_managed_text(
            root,
            "mcp-config",
            _normal_path(target),
            content,
            start_marker,
            end_marker,
            target_path=target,
            operation_kind="external_managed_text",
        )
        operation["action"] = "conflict"
        return operation, "The Codex user configuration is not valid TOML."
    servers = document.get("mcp_servers", {})
    desired_servers = desired_document.get("mcp_servers", {})
    if not isinstance(servers, dict) or not isinstance(desired_servers, dict):
        existing_server = object()
        desired_server = None
    else:
        existing_server = servers.get(server_alias)
        desired_server = desired_servers.get(server_alias)
    if existing_server is not None:
        before_hash = _sha256_file(target)
        operation = {
            "id": "mcp-config",
            "kind": "external_managed_text",
            "target": _normal_path(target),
            "content": content,
            "start_marker": start_marker,
            "end_marker": end_marker,
            "expected_before_sha256": before_hash,
            "expected_after_sha256": before_hash,
            "action": "no_change" if _json_equal(existing_server, desired_server) else "conflict",
        }
        if _json_equal(existing_server, desired_server):
            return operation, None
        return operation, "An existing semantic Codex MCP server definition conflicts with this installation."
    return _plan_managed_text(
        root,
        "mcp-config",
        _normal_path(target),
        content,
        start_marker,
        end_marker,
        target_path=target,
        operation_kind="external_managed_text",
    )


def _plan_gitignore(root: Path, content: str) -> tuple[dict[str, Any], str | None]:
    target_relative = ".gitignore"
    target = _safe_target(root, target_relative)
    before_hash = _file_hash(target)
    operation: dict[str, Any] = {
        "id": "verification-gitignore",
        "kind": "append_lines",
        "target": target_relative,
        "content": content,
        "expected_before_sha256": before_hash,
    }
    existing = b"" if before_hash is None else target.read_bytes()
    try:
        after, suffix = _append_missing_lines(existing, content)
    except InstallError as exc:
        operation["action"] = "conflict"
        return operation, str(exc)
    if after == existing:
        operation.update({"action": "no_change", "expected_after_sha256": before_hash})
    else:
        operation.update(
            {
                "action": "create" if before_hash is None else "append",
                "append_text": suffix,
                "expected_after_sha256": _sha256_bytes(after),
            }
        )
    return operation, None


def _plan_json_merge(
    root: Path,
    operation_id: str,
    target_relative: str,
    desired: dict[str, Any],
    *,
    target_path: Path | None = None,
    operation_kind: str = "json_merge",
) -> tuple[dict[str, Any], str | None]:
    target = target_path if target_path is not None else _safe_target(root, target_relative)
    target_label = _normal_path(target) if target_path is not None else target_relative
    before_hash = _file_hash(target)
    operation: dict[str, Any] = {
        "id": operation_id,
        "kind": operation_kind,
        "target": target_label,
        "desired": desired,
        "expected_before_sha256": before_hash,
    }
    if before_hash is None:
        after = _json_text(desired).encode("utf-8")
        operation.update({"action": "create", "expected_after_sha256": _sha256_bytes(after)})
        return operation, None
    try:
        existing_body = target.read_bytes()
        existing = (
            {}
            if operation_kind == "external_json_merge" and existing_body == b""
            else json.loads(existing_body.decode("utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        operation["action"] = "conflict"
        return operation, "The managed JSON destination is not a valid UTF-8 JSON document."
    if not isinstance(existing, dict):
        operation["action"] = "conflict"
        return operation, "The managed JSON destination must contain an object."
    try:
        merged, inverse = _merge_json(existing, desired)
    except InstallError as exc:
        operation["action"] = "conflict"
        return operation, str(exc)
    after = _json_text(merged).encode("utf-8")
    if not inverse:
        operation.update({"action": "no_change", "expected_after_sha256": before_hash})
    else:
        operation.update(
            {
                "action": "merge",
                "expected_after_sha256": _sha256_bytes(after),
                "rollback_changes": inverse,
            }
        )
    return operation, None


def _build_plan(
    args: argparse.Namespace,
    *,
    historical: bool = False,
) -> dict[str, Any]:
    root = _validated_root(args.target_root)
    _assert_worktree_boundary(root)
    platform = _resolved_platform(args.platform)
    token_env = _validate_inputs(args.project_id, args.token_env)
    base = _validated_base_url(args.api_base_url)
    api_base_url = urlunsplit((base.scheme, base.netloc, base.path.rstrip("/"), "", ""))
    mcp_url = _endpoint(base, "/mcp")
    contract_version_url = _endpoint(base, "/api/contract-version")
    completion_gate_url = _endpoint(base, "/api/v1/integrations/completion-gate")
    rest_base_url = _endpoint(base, "/api/v1/integrations")
    openapi_url = _endpoint(base, "/api/v1/integrations/openapi.json")
    layout = _client_layout(args.client)
    user_layout = _client_user_layout(root, args.client, getattr(args, "client_config_dir", None))
    runtime_root = _runtime_root(root, args.client, getattr(args, "runtime_base", None))
    if historical:
        python_executable = _validated_historical_executable(args.python_executable, "Python executable", root)
        git_executable = _validated_historical_executable(args.git_executable, "Git executable", root)
    else:
        python_executable = _resolved_python_executable(root, getattr(args, "python_executable", None))
        git_executable = _resolved_git_executable(root, getattr(args, "git_executable", None))
        _assert_actual_git_worktree_root(root, git_executable)
        _assert_project_environment_storage(root, git_executable, token_env)
        _assert_strict_source_capture(root, git_executable)
    source_identity = _package_source_identity(args, git_executable, historical=historical)
    server_alias = f"acceptora-{_runtime_identity(root, args.client)[:12]}"
    trusted_installer = runtime_root / RUNTIME_PACKAGE_DIRECTORY / "scripts" / "install.py"
    installed_source_tree_sha256 = _package_source_tree_sha256(_iter_release_identity_files())
    files = _iter_skill_files()
    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []

    def add(result: tuple[dict[str, Any], str | None]) -> None:
        operation, conflict = result
        operations.append(operation)
        if conflict:
            conflicts.append({"operation": operation["id"], "message": conflict})

    project = _render_project_config(args.project_id)
    pinned_runtime_config = copy.deepcopy(project)
    pinned_runtime_config.update(
        {
            "enabled": True,
            "source_adapter": "git",
            "target_root": _normal_path(root),
            "client": args.client,
            "token_env": token_env,
            "mcp_url": mcp_url,
            "contract_version_url": contract_version_url,
            "completion_gate_url": completion_gate_url,
            "rest_base_url": rest_base_url,
            "openapi_url": openapi_url,
            "skill_repository_url": source_identity["repository_url"],
            "skill_repository_branch": source_identity["branch"],
            "installed_commit_sha": source_identity["commit_sha"],
            "skill_update_timeout_seconds": 3,
            "installed_source_tree_sha256": installed_source_tree_sha256,
            "tls_ca_file": None,
            "timeout_seconds": 8,
            "retry_attempts": 3,
            "retry_base_delay_seconds": 0.5,
            "max_retry_delay_seconds": 30,
            "max_stop_blocks": 2,
            "python_executable": _normal_path(python_executable),
            "git_executable": _normal_path(git_executable),
            "config_source": "installer_owned_external_runtime",
            "mcp_server_alias": server_alias,
            "client_config_directory": _normal_path(user_layout["directory"]),
        }
    )
    add(_plan_external_runtime(runtime_root, _runtime_files(args.client, pinned_runtime_config, git_executable)))
    add(_plan_copy_tree(root, layout["skill"], files))
    add(
        _plan_managed_text(
            root,
            "agent-instructions",
            layout["instruction"],
            _read_text(layout["instruction_source"]),
            MANAGED_START,
            MANAGED_END,
        )
    )
    add(_plan_gitignore(root, _gitignore_content()))
    add(_plan_json_merge(root, "project-config", ".verification/config.json", project))
    hook_source, hooks = _render_hooks(args.client, platform, runtime_root, python_executable)
    mcp_source, mcp = _render_mcp_config(
        args.client,
        token_env,
        mcp_url,
        server_alias,
        runtime_root,
        python_executable,
    )
    if isinstance(mcp, str):
        start_marker, end_marker = _toml_managed_markers(server_alias)
        block = f"{start_marker}\n{mcp.rstrip()}\n{end_marker}\n"
        add(_plan_codex_mcp(root, user_layout["mcp"], block, server_alias))
        add(
            _plan_json_merge(
                root,
                "client-hooks",
                _normal_path(user_layout["settings"]),
                hooks,
                target_path=user_layout["settings"],
                operation_kind="external_json_merge",
            )
        )
    elif user_layout["settings"] == user_layout["mcp"]:
        combined, _ = _merge_json(hooks, mcp)
        add(
            _plan_json_merge(
                root,
                "client-settings",
                _normal_path(user_layout["settings"]),
                combined,
                target_path=user_layout["settings"],
                operation_kind="external_json_merge",
            )
        )
    else:
        add(
            _plan_json_merge(
                root,
                "mcp-config",
                _normal_path(user_layout["mcp"]),
                mcp,
                target_path=user_layout["mcp"],
                operation_kind="external_json_merge",
            )
        )
        add(
            _plan_json_merge(
                root,
                "client-hooks",
                _normal_path(user_layout["settings"]),
                hooks,
                target_path=user_layout["settings"],
                operation_kind="external_json_merge",
            )
        )

    manifest = _package_manifest()
    plan: dict[str, Any] = {
        "schema_version": 1,
        "command": "install",
        "preview_only": True,
        "mutations_performed": 0,
        "client": args.client,
        "platform": platform,
        "target_root": _normal_path(root),
        "runtime_root": _normal_path(runtime_root),
        "trusted_installer": _normal_path(trusted_installer),
        "client_config_directory": _normal_path(user_layout["directory"]),
        "mcp_server_alias": server_alias,
        "receipt": _normal_path(runtime_root / RUNTIME_RECEIPT),
        "inputs": {
            "client": args.client,
            "platform": platform,
            "target_root": _normal_path(root),
            "runtime_base": _normal_path(runtime_root.parent),
            "client_config_dir": _normal_path(user_layout["directory"]),
            "python_executable": _normal_path(python_executable),
            "git_executable": _normal_path(git_executable),
            "project_id": args.project_id,
            "api_base_url": api_base_url,
            "token_env": token_env,
            "skill_repository_url": source_identity["repository_url"],
            "skill_repository_branch": source_identity["branch"],
            "installed_commit_sha": source_identity["commit_sha"],
        },
        "package": {
            "name": manifest["skill"].get("name"),
            "version": manifest["skill"].get("version"),
            "manifest_sha256": _sha256_bytes(_read_source("config/package-manifest.json")),
            "source_tree_sha256": installed_source_tree_sha256,
            "source": source_identity,
        },
        "sources": {"hooks": hook_source, "mcp": mcp_source},
        "operations": operations,
        "conflicts": conflicts,
        "warnings": [
            "No files were changed. Review this complete plan before apply.",
            "Apply requires this plan's exact SHA-256 and rechecks every target and package source.",
            "Executable hooks, pinned runtime policy, session state, and the receipt live outside the target repository.",
            "Hooks and MCP endpoint configuration are merged into the selected user-scope client configuration.",
            f"Future apply, status, rollback-plan, and rollback commands must use the trusted installer at {_normal_path(trusted_installer)}.",
            f"The runtime token environment variable is fixed to {token_env} for this Acceptora project and its value is never read by the installer.",
            "Client trust and tool approvals remain user-controlled.",
        ],
    }
    plan["plan_sha256"] = _plan_digest(plan)
    return plan


def _plan_digest(plan: dict[str, Any]) -> str:
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha256_bytes(_canonical_json_bytes(payload))


def _load_json_file(path_value: str) -> dict[str, Any]:
    try:
        if path_value == "-":
            value = json.load(sys.stdin)
        else:
            with Path(path_value).open("r", encoding="utf-8") as handle:
                value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"Unable to read JSON input: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError("JSON input must contain an object.")
    return value


def _assert_no_link_chain(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() and _is_linklike(candidate):
            raise InstallError(f"Managed path crosses a symlink or junction: {candidate}")


def _atomic_write(
    path: Path,
    body: bytes,
    mode: int = 0o644,
    *,
    create_only: bool = False,
    private: bool = False,
    expected_before: str | None | object = _UNSPECIFIED,
) -> None:
    _assert_no_link_chain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_chain(path.parent)
    parent_identity = path.parent.stat()
    staging: Path | None = None
    temporary: Path | None = None
    descriptor: int | None = None
    descriptor_open = False
    published = False
    try:
        temporary_parent = path.parent
        if private and os.name == "nt":
            staging = Path(tempfile.mkdtemp(prefix=f".{path.name}.stage-", dir=path.parent))
            _secure_private_directory(staging)
            if any(staging.iterdir()):
                raise InstallError("The private atomic-write staging directory was modified before use.")
            temporary_parent = staging
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=temporary_parent,
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        if private:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            _assert_private_file(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor_open = False
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600 if private else mode)
        _assert_no_link_chain(path.parent)
        current_parent = path.parent.stat()
        if (current_parent.st_dev, current_parent.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
            raise InstallError("Managed destination parent changed during an atomic write.")
        if expected_before is not _UNSPECIFIED and _file_hash(path) != expected_before:
            raise InstallError(f"Managed destination changed during an atomic write: {path}")
        if create_only:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise InstallError(f"Managed create destination appeared during apply: {path}") from exc
            published = True
        else:
            os.replace(temporary, path)
            published = True
        _assert_no_link_chain(path)
        if _sha256_file(path) != _sha256_bytes(body):
            raise InstallError("Managed destination changed during an atomic write.")
        if private:
            _assert_private_file(path)
    finally:
        active_error = sys.exc_info()[0] is not None
        if descriptor_open:
            assert descriptor is not None
            os.close(descriptor)
        cleanup_errors: list[OSError] = []
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError as error:
                cleanup_errors.append(error)
        if staging is not None:
            try:
                staging.rmdir()
            except OSError as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            if published or active_error:
                for error in cleanup_errors:
                    sys.stderr.write(f"Installer warning: atomic-write cleanup was deferred: {error}\n")
            else:
                raise InstallError("The atomic-write temporary path could not be removed.") from cleanup_errors[0]


class _Transaction:
    def __init__(self, *, allowed_runtime_read_execute_sid: str | None = None) -> None:
        self._before: dict[Path, tuple[bytes, int] | None] = {}
        self._after: dict[Path, str | None] = {}
        self._created_directories: set[Path] = set()
        self._private_paths: set[Path] = set()
        self._allowed_runtime_read_execute_sid = allowed_runtime_read_execute_sid

    def ensure_directory(self, path: Path, *, private: bool = False, secure_boundary: bool = False) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        if _is_linklike(current):
            raise InstallError(f"Cannot create a directory through a symlink or junction: {current}")
        for directory in reversed(missing):
            directory.mkdir(mode=0o700 if private and os.name != "nt" else 0o777)
            self._created_directories.add(directory)
            if private and (os.name != "nt" or secure_boundary):
                _secure_private_directory(
                    directory,
                    allowed_read_execute_sid=(
                        self._allowed_runtime_read_execute_sid if secure_boundary else None
                    ),
                )
        if private and path.exists() and secure_boundary:
            _assert_private_directory(
                path,
                "External runtime directory",
                allowed_read_execute_sid=self._allowed_runtime_read_execute_sid,
            )

    def write(
        self,
        path: Path,
        body: bytes,
        mode: int | None = None,
        *,
        expected_before: str | None | object = _UNSPECIFIED,
        private_parents: bool = False,
        private_output: bool = False,
    ) -> None:
        existing_mode: int | None = None
        current_hash = _file_hash(path)
        if expected_before is not _UNSPECIFIED and current_hash != expected_before:
            raise InstallError(f"Managed destination changed immediately before write: {path}")
        if path not in self._before:
            if path.exists():
                if _is_linklike(path) or not path.is_file():
                    raise InstallError(f"Cannot atomically replace non-file destination: {path}")
                existing_mode = stat.S_IMODE(path.stat().st_mode)
                self._before[path] = (path.read_bytes(), existing_mode)
            else:
                self._before[path] = None
        elif path.exists():
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        self.ensure_directory(path.parent, private=private_parents)
        if private_output:
            self._private_paths.add(path)
        self._after[path] = _sha256_bytes(body)
        _atomic_write(
            path,
            body,
            0o600 if private_output else (mode if mode is not None else (existing_mode or 0o644)),
            create_only=expected_before is None,
            private=private_output,
            expected_before=expected_before,
        )

    def remove(
        self,
        path: Path,
        *,
        expected_sha256: str | None | object = _UNSPECIFIED,
        private_output: bool = False,
    ) -> None:
        current_hash = _file_hash(path)
        if expected_sha256 is not _UNSPECIFIED and current_hash != expected_sha256:
            raise InstallError(f"Managed destination changed immediately before removal: {path}")
        if private_output:
            self._private_paths.add(path)
        if path not in self._before:
            if not path.exists():
                self._before[path] = None
                return
            if _is_linklike(path) or not path.is_file():
                raise InstallError(f"Cannot atomically remove non-file destination: {path}")
            self._before[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        _assert_no_link_chain(path)
        if path.exists():
            before_identity = path.stat()
            if expected_sha256 is not _UNSPECIFIED and _file_hash(path) != expected_sha256:
                raise InstallError(f"Managed destination changed immediately before removal: {path}")
            current_identity = path.stat()
            if (before_identity.st_dev, before_identity.st_ino) != (current_identity.st_dev, current_identity.st_ino):
                raise InstallError(f"Managed destination identity changed immediately before removal: {path}")
            path.unlink()
        self._after[path] = None

    def rollback(self) -> None:
        for path, before in reversed(list(self._before.items())):
            if before is None:
                if (
                    path.exists()
                    and path.is_file()
                    and not _is_linklike(path)
                    and self._after.get(path) is not None
                    and _file_hash(path) == self._after[path]
                ):
                    path.unlink()
            else:
                current = _file_hash(path)
                if current == self._after.get(path) or (current is None and self._after.get(path) is None):
                    _atomic_write(
                        path,
                        before[0],
                        before[1],
                        create_only=current is None,
                        private=path in self._private_paths,
                    )
        for directory in sorted(self._created_directories, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass


def _verify_plan(plan: dict[str, Any], accepted: str) -> Path:
    if plan.get("schema_version") != 1 or plan.get("command") != "install":
        raise InstallError("Unsupported install plan schema.")
    claimed = plan.get("plan_sha256")
    calculated = _plan_digest(plan)
    if not isinstance(claimed, str) or claimed != calculated:
        raise InstallError("The install plan digest is invalid.")
    if accepted != claimed:
        raise InstallError("The accepted plan SHA-256 does not exactly match this plan.")
    inputs = plan.get("inputs")
    expected_input_keys = {
        "api_base_url",
        "client",
        "client_config_dir",
        "git_executable",
        "installed_commit_sha",
        "platform",
        "project_id",
        "python_executable",
        "runtime_base",
        "skill_repository_branch",
        "skill_repository_url",
        "target_root",
        "token_env",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected_input_keys:
        raise InstallError("The install plan is missing its canonical inputs.")
    if not all(isinstance(inputs[key], str) for key in expected_input_keys):
        raise InstallError("The install plan canonical inputs must be strings.")
    if inputs["project_id"] == "proj_REPLACE_WITH_PROJECT_ULID":
        raise InstallError("Apply requires an explicit real Acceptora project ID.")
    if urlsplit(inputs["api_base_url"]).hostname == "verify.example.test":
        raise InstallError("Apply requires an explicit real Acceptora API base URL.")
    reconstructed = _build_plan(argparse.Namespace(**inputs))
    if _canonical_json_bytes(plan) != _canonical_json_bytes(reconstructed):
        raise InstallError("The install plan does not match the canonical operations for its accepted inputs.")
    if plan.get("conflicts"):
        raise InstallError("The install plan contains conflicts and cannot be applied.")
    root = _validated_root(inputs["target_root"])
    return root


def _operation_target(root: Path, operation: dict[str, Any]) -> Path:
    target = operation.get("target")
    if not isinstance(target, str):
        raise InstallError("An install operation is missing its target.")
    if operation.get("kind") == "external_runtime":
        runtime = _validate_external_path(Path(target), "Runtime directory")
        try:
            runtime.relative_to(root)
        except ValueError:
            return runtime
        raise InstallError("The executable runtime must be outside the target repository.")
    if operation.get("kind") in {"external_managed_text", "external_json_merge"}:
        path = Path(target)
        if not path.is_absolute():
            raise InstallError("An external client configuration target must be absolute.")
        _validate_external_path(path.parent, "Client configuration directory")
        if _is_linklike(path) or (path.exists() and not path.is_file()):
            raise InstallError("An external client configuration target is not a regular file.")
        try:
            path.resolve(strict=False).relative_to(_enclosing_worktree_root(root))
        except ValueError:
            return path
        raise InstallError("Client configuration must be outside the enclosing repository worktree.")
    return _safe_target(root, target)


def _preflight_operation(root: Path, operation: dict[str, Any]) -> None:
    action = operation.get("action")
    if action == "conflict":
        raise InstallError(f"Operation still contains a conflict: {operation.get('id')}")
    target = _operation_target(root, operation)
    if operation.get("kind") in {"copy_tree", "external_runtime"}:
        if action == "create" and target.exists():
            raise InstallError("The skill destination changed after the plan was created.")
        if action == "no_change":
            if _is_linklike(target) or not target.is_dir():
                raise InstallError("The skill destination changed after the plan was created.")
            actual: dict[str, str] = {}
            for path in sorted(target.rglob("*")):
                if _is_linklike(path):
                    raise InstallError("The skill destination changed after the plan was created.")
                if path.is_file():
                    actual[path.relative_to(target).as_posix()] = _sha256_file(path)
            expected = {
                entry.get("destination", entry.get("source")): entry["sha256"]
                for entry in operation.get("files", [])
            }
            if actual != expected:
                raise InstallError("The skill destination changed after the plan was created.")
        for entry in operation.get("files", []):
            if _sha256_bytes(_planned_file_body(entry)) != entry.get("sha256"):
                raise InstallError(f"Package source changed after planning: {entry.get('source') or entry.get('destination')}")
        return
    if _file_hash(target) != operation.get("expected_before_sha256"):
        raise InstallError(f"Managed target changed after planning: {operation.get('target')}")


def _apply_operation(
    root: Path,
    operation: dict[str, Any],
    transaction: _Transaction,
) -> tuple[dict[str, Any], int]:
    kind = operation["kind"]
    action = operation["action"]
    target = _operation_target(root, operation)
    receipt: dict[str, Any] = {
        "id": operation["id"],
        "kind": kind,
        "target": operation["target"],
        "action": action,
        "before_sha256": operation.get("expected_before_sha256"),
        "after_sha256": operation.get("expected_after_sha256"),
    }
    if action == "no_change":
        if kind in {"copy_tree", "external_runtime"}:
            receipt["files"] = [
                {
                    "path": (
                        f"{operation['target']}/{entry['source']}"
                        if kind == "copy_tree"
                        else _normal_path(target / entry["destination"])
                    ),
                    "sha256": entry["sha256"],
                    "mode": entry["mode"],
                }
                for entry in operation.get("files", [])
            ]
        return receipt, 0
    if kind in {"copy_tree", "external_runtime"}:
        if kind == "external_runtime":
            transaction.ensure_directory(target, private=True, secure_boundary=True)
        copied: list[dict[str, str]] = []
        for entry in operation["files"]:
            if kind == "copy_tree":
                destination_label = f"{operation['target']}/{entry['source']}"
                destination = _safe_target(root, destination_label)
            else:
                destination = _safe_target(target, entry["destination"])
                destination_label = _normal_path(destination)
            body = _planned_file_body(entry)
            if _sha256_bytes(body) != entry["sha256"]:
                raise InstallError(f"Package source changed during apply: {entry.get('source') or entry.get('destination')}")
            transaction.write(
                destination,
                body,
                int(entry["mode"], 8),
                expected_before=None,
                private_parents=kind == "external_runtime",
            )
            copied.append({"path": destination_label, "sha256": entry["sha256"], "mode": entry["mode"]})
        receipt["files"] = copied
        receipt["rollback"] = {"kind": "remove_created_tree"}
        return receipt, len(copied)
    if kind in {"managed_text", "external_managed_text"}:
        before = b"" if operation["expected_before_sha256"] is None else target.read_bytes()
        if action == "create":
            after = operation["content"].encode("utf-8")
            rollback = (
                {"kind": "remove_managed_block"}
                if kind == "external_managed_text"
                else {"kind": "remove_created_file"}
            )
        elif action == "append":
            suffix = operation["append_text"].encode("utf-8")
            after = before + suffix
            rollback = (
                {"kind": "remove_managed_block"}
                if kind == "external_managed_text"
                else {"kind": "remove_suffix", "suffix": operation["append_text"]}
            )
        else:
            raise InstallError(f"Unsupported managed-text action: {action}")
        if _sha256_bytes(after) != operation["expected_after_sha256"]:
            raise InstallError(f"Managed text no longer matches its plan: {operation['target']}")
        transaction.write(
            target,
            after,
            expected_before=operation["expected_before_sha256"],
            private_output=kind == "external_managed_text",
        )
        receipt["rollback"] = rollback
        return receipt, 1
    if kind == "append_lines":
        before = b"" if operation["expected_before_sha256"] is None else target.read_bytes()
        after = before + operation.get("append_text", "").encode("utf-8")
        if _sha256_bytes(after) != operation["expected_after_sha256"]:
            raise InstallError(f"Managed lines no longer match their plan: {operation['target']}")
        transaction.write(
            target,
            after,
            expected_before=operation["expected_before_sha256"],
        )
        receipt["rollback"] = (
            {"kind": "remove_created_file"}
            if action == "create"
            else {"kind": "remove_suffix", "suffix": operation["append_text"]}
        )
        return receipt, 1
    if kind in {"json_merge", "external_json_merge"}:
        if action == "create":
            after = _json_text(operation["desired"]).encode("utf-8")
            inverse = _external_json_rollback_changes(operation) if kind == "external_json_merge" else []
            rollback = (
                {"kind": "remove_owned_json", "changes": inverse}
                if kind == "external_json_merge"
                else {"kind": "remove_created_file"}
            )
        elif action == "merge":
            try:
                current_body = target.read_bytes()
                current = (
                    {}
                    if kind == "external_json_merge" and current_body == b""
                    else json.loads(current_body.decode("utf-8"))
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InstallError(f"Managed JSON changed after planning: {operation['target']}") from exc
            if not isinstance(current, dict):
                raise InstallError(f"Managed JSON changed after planning: {operation['target']}")
            merged, inverse = _merge_json(current, operation["desired"])
            if inverse != operation.get("rollback_changes"):
                raise InstallError(f"Managed JSON rollback changed after planning: {operation['target']}")
            after = _json_text(merged).encode("utf-8")
            rollback = (
                {"kind": "remove_owned_json", "changes": _external_json_rollback_changes(operation)}
                if kind == "external_json_merge"
                else {"kind": "json_inverse", "changes": operation["rollback_changes"]}
            )
        else:
            raise InstallError(f"Unsupported JSON action: {action}")
        if _sha256_bytes(after) != operation["expected_after_sha256"]:
            raise InstallError(f"Managed JSON no longer matches its plan: {operation['target']}")
        transaction.write(
            target,
            after,
            expected_before=operation["expected_before_sha256"],
            private_output=kind == "external_json_merge",
        )
        receipt["rollback"] = rollback
        return receipt, 1
    raise InstallError(f"Unsupported install operation: {kind}")


def _receipt_digest(receipt: dict[str, Any]) -> str:
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _sha256_bytes(_canonical_json_bytes(payload))


def _apply_plan(plan: dict[str, Any], accepted: str) -> dict[str, Any]:
    root = _verify_plan(plan, accepted)
    runtime_root = _validate_external_path(Path(str(plan["runtime_root"])), "Runtime directory")
    receipt_path = runtime_root / RUNTIME_RECEIPT
    if _normal_path(receipt_path) != plan.get("receipt"):
        raise InstallError("The install plan receipt is outside its canonical runtime directory.")
    if receipt_path.exists():
        raise InstallError("An installation receipt already exists; inspect status or roll it back first.")
    for operation in plan["operations"]:
        _preflight_operation(root, operation)
    transaction = _Transaction(
        allowed_runtime_read_execute_sid=(
            _windows_codex_sandbox_users_sid()
            if os.name == "nt" and plan["client"] == "codex"
            else None
        )
    )
    receipt_operations: list[dict[str, Any]] = []
    mutations = 0
    try:
        for operation in plan["operations"]:
            receipt_operation, count = _apply_operation(root, operation, transaction)
            receipt_operations.append(receipt_operation)
            mutations += count
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "installer": PACKAGE_IDENTITY,
            "client": plan["client"],
            "platform": plan["platform"],
            "target_root": plan["target_root"],
            "runtime_root": plan["runtime_root"],
            "package": plan["package"],
            "inputs": plan["inputs"],
            "plan_sha256": plan["plan_sha256"],
            "receipt": plan["receipt"],
            "install_plan": plan,
            "operations": receipt_operations,
        }
        receipt["receipt_sha256"] = _receipt_digest(receipt)
        transaction.write(
            receipt_path,
            _json_text(receipt).encode("utf-8"),
            mode=0o600,
            expected_before=None,
            private_parents=True,
        )
        mutations += 1
    except Exception:
        transaction.rollback()
        raise
    return {
        "schema_version": 1,
        "status": "installed",
        "client": plan["client"],
        "target_root": _normal_path(root),
        "runtime_root": plan["runtime_root"],
        "runtime_base": plan["inputs"]["runtime_base"],
        "installed_commit_sha": plan["package"]["source"]["commit_sha"],
        "token_env": plan["inputs"]["token_env"],
        "plan_sha256": plan["plan_sha256"],
        "receipt": plan["receipt"],
        "trusted_installer": plan["trusted_installer"],
        "mutations_performed": mutations,
    }


def _static_operation(operation: dict[str, Any]) -> dict[str, Any]:
    kind = operation.get("kind")
    base_kind = str(kind).removeprefix("external_")
    keys = ["id", "kind", "target"]
    if kind in {"copy_tree", "external_runtime"}:
        keys.append("files")
    elif base_kind == "managed_text":
        keys.extend(["content", "start_marker", "end_marker"])
    elif base_kind == "append_lines":
        keys.append("content")
    elif base_kind == "json_merge":
        keys.append("desired")
    else:
        raise InstallError(f"The install plan contains an unsupported operation kind: {kind}")
    return {key: operation.get(key) for key in keys}


def _static_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": plan.get("schema_version"),
        "command": plan.get("command"),
        "client": plan.get("client"),
        "platform": plan.get("platform"),
        "target_root": plan.get("target_root"),
        "runtime_root": plan.get("runtime_root"),
        "trusted_installer": plan.get("trusted_installer"),
        "client_config_directory": plan.get("client_config_directory"),
        "mcp_server_alias": plan.get("mcp_server_alias"),
        "receipt": plan.get("receipt"),
        "inputs": plan.get("inputs"),
        "package": plan.get("package"),
        "sources": plan.get("sources"),
        "operations": [_static_operation(operation) for operation in plan.get("operations", [])],
    }


def _valid_sha256(value: Any, allow_none: bool = False) -> bool:
    return (allow_none and value is None) or (isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value) is not None)


def _allowed_json_rollback_changes(desired: dict[str, Any]) -> list[dict[str, Any]]:
    allowed: list[dict[str, Any]] = []

    def walk(value: dict[str, Any], path: list[str]) -> None:
        for key in sorted(value):
            child = value[key]
            child_path = [*path, key]
            allowed.append({"kind": "remove_key", "path": child_path, "value": copy.deepcopy(child)})
            if isinstance(child, dict):
                walk(child, child_path)
            elif isinstance(child, list):
                allowed.extend(
                    {"kind": "remove_list_value", "path": child_path, "value": copy.deepcopy(item)}
                    for item in child
                )

    walk(desired, [])
    return allowed


def _validate_append_lines_suffix(operation: dict[str, Any]) -> None:
    suffix = operation.get("append_text")
    if not isinstance(suffix, str) or not suffix.endswith("\n"):
        raise InstallError(f"The historical line append is invalid: {operation.get('id')}.")
    body = suffix[1:] if suffix.startswith("\n") else suffix
    appended = body.splitlines()
    allowed = [
        line.strip()
        for line in str(operation.get("content", "")).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not appended or len(appended) != len(set(appended)) or any(line not in allowed for line in appended):
        raise InstallError(f"The historical line append exceeds canonical content: {operation.get('id')}.")
    positions = [allowed.index(line) for line in appended]
    if positions != sorted(positions):
        raise InstallError(f"The historical line append order is invalid: {operation.get('id')}.")


def _validate_historical_plan_actions(plan: dict[str, Any]) -> None:
    seen: set[str] = set()
    for operation in plan.get("operations", []):
        operation_id = operation.get("id")
        kind = operation.get("kind")
        base_kind = str(kind).removeprefix("external_")
        action = operation.get("action")
        if not isinstance(operation_id, str) or operation_id in seen:
            raise InstallError("The historical install plan has missing or duplicate operation IDs.")
        seen.add(operation_id)
        allowed = {
            "copy_tree": {"create", "no_change"},
            "external_runtime": {"create", "no_change"},
            "managed_text": {"create", "append", "no_change"},
            "append_lines": {"create", "append", "no_change"},
            "json_merge": {"create", "merge", "no_change"},
        }.get(base_kind if kind not in {"copy_tree", "external_runtime"} else kind, set())
        if action not in allowed:
            raise InstallError(f"The historical install plan has an invalid action for {operation_id}.")
        if "external_json_origin" in operation:
            raise InstallError(f"The historical external JSON origin is unsupported for {operation_id}.")
        before = operation.get("expected_before_sha256")
        after = operation.get("expected_after_sha256")
        if kind in {"copy_tree", "external_runtime"}:
            if "expected_before_sha256" in operation or "expected_after_sha256" in operation:
                raise InstallError(f"The historical copy operation has unexpected target hashes: {operation_id}.")
            continue
        if not _valid_sha256(before, allow_none=True) or not _valid_sha256(after):
            raise InstallError(f"The historical install plan has invalid hashes for {operation_id}.")
        if action == "create" and before is not None:
            raise InstallError(f"The historical create operation has a prior hash: {operation_id}.")
        if action == "no_change" and before != after:
            raise InstallError(f"The historical no-change operation has different hashes: {operation_id}.")
        if action == "create":
            if base_kind == "managed_text":
                expected_body = str(operation.get("content", "")).encode("utf-8")
            elif base_kind == "append_lines":
                expected_body, _ = _append_missing_lines(b"", str(operation.get("content", "")))
            elif base_kind == "json_merge":
                expected_body = _json_text(operation.get("desired")).encode("utf-8")
            else:
                expected_body = b""
            if _sha256_bytes(expected_body) != after:
                raise InstallError(f"The historical create operation has a non-canonical result: {operation_id}.")
        if base_kind == "managed_text" and action == "append":
            content = str(operation.get("content", ""))
            valid_suffixes = {_append_block(b"existing\n", content)[1], _append_block(b"existing", content)[1]}
            if operation.get("append_text") not in valid_suffixes:
                raise InstallError(f"The historical managed append exceeds canonical content: {operation_id}.")
        if base_kind == "append_lines" and action == "append":
            _validate_append_lines_suffix(operation)
        if base_kind == "json_merge" and action == "merge":
            changes = operation.get("rollback_changes")
            if not isinstance(changes, list) or not changes:
                raise InstallError(f"The historical JSON merge is missing canonical rollback changes: {operation_id}.")
            allowed_changes = _allowed_json_rollback_changes(operation.get("desired", {}))
            allowed_encoded = {_canonical_json_bytes(change) for change in allowed_changes}
            encoded_changes = [_canonical_json_bytes(change) for change in changes]
            if any(change not in allowed_encoded for change in encoded_changes) or len(encoded_changes) != len(
                set(encoded_changes)
            ):
                raise InstallError(f"The historical JSON rollback exceeds canonical content: {operation_id}.")
        elif "rollback_changes" in operation:
            raise InstallError(f"The historical operation has unexpected JSON rollback changes: {operation_id}.")


def _validate_historical_install_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != 1 or plan.get("command") != "install":
        raise InstallError("The receipt does not contain a supported historical install plan.")
    if plan.get("plan_sha256") != _plan_digest(plan):
        raise InstallError("The historical install plan content checksum is invalid.")
    inputs = plan.get("inputs")
    if not isinstance(inputs, dict):
        raise InstallError("The historical install plan has no canonical inputs.")
    canonical_now = _build_plan(argparse.Namespace(**inputs), historical=True)
    if _canonical_json_bytes(_static_plan(plan)) != _canonical_json_bytes(_static_plan(canonical_now)):
        raise InstallError("The historical install plan exceeds the canonical installer authority.")
    if plan.get("conflicts"):
        raise InstallError("A receipt cannot be based on an install plan that contained conflicts.")
    _validate_historical_plan_actions(plan)


def _expected_receipt_operation(root: Path, plan_operation: dict[str, Any]) -> dict[str, Any]:
    kind = plan_operation["kind"]
    base_kind = str(kind).removeprefix("external_")
    action = plan_operation["action"]
    target = plan_operation["target"]
    operation: dict[str, Any] = {
        "id": plan_operation["id"],
        "kind": kind,
        "target": target,
        "action": action,
        "before_sha256": plan_operation.get("expected_before_sha256"),
        "after_sha256": plan_operation.get("expected_after_sha256"),
    }
    if kind in {"copy_tree", "external_runtime"}:
        target_path = _operation_target(root, plan_operation)
        operation["files"] = [
            {
                "path": (
                    f"{target}/{entry['source']}"
                    if kind == "copy_tree"
                    else _normal_path(target_path / entry["destination"])
                ),
                "sha256": entry["sha256"],
                "mode": entry["mode"],
            }
            for entry in plan_operation["files"]
        ]
        if action == "create":
            operation["rollback"] = {"kind": "remove_created_tree"}
        return operation
    if action == "no_change":
        return operation
    if kind == "external_managed_text":
        operation["rollback"] = {"kind": "remove_managed_block"}
    elif kind == "external_json_merge":
        changes = _external_json_rollback_changes(plan_operation)
        operation["rollback"] = {"kind": "remove_owned_json", "changes": changes}
    elif action == "create":
        operation["rollback"] = {"kind": "remove_created_file"}
    elif base_kind in {"managed_text", "append_lines"} and action == "append":
        operation["rollback"] = {"kind": "remove_suffix", "suffix": plan_operation["append_text"]}
    elif base_kind == "json_merge" and action == "merge":
        operation["rollback"] = {"kind": "json_inverse", "changes": plan_operation["rollback_changes"]}
    else:
        raise InstallError(f"The historical install operation cannot be rolled back canonically: {plan_operation['id']}")
    return operation


def _validate_receipt(root: Path, client: str, runtime_root: Path, receipt: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "installer",
        "client",
        "platform",
        "target_root",
        "runtime_root",
        "package",
        "inputs",
        "plan_sha256",
        "receipt",
        "install_plan",
        "operations",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise InstallError("The installation receipt has extra or missing fields.")
    if receipt.get("receipt_sha256") != _receipt_digest(receipt):
        raise InstallError("The installation receipt content checksum is invalid; it is not an authenticity signature.")
    expected_receipt_path = _normal_path(runtime_root / RUNTIME_RECEIPT)
    if (
        receipt.get("schema_version") != 1
        or receipt.get("installer") != PACKAGE_IDENTITY
        or receipt.get("client") != client
        or receipt.get("target_root") != _normal_path(root)
        or receipt.get("runtime_root") != _normal_path(runtime_root)
        or receipt.get("receipt") != expected_receipt_path
    ):
        raise InstallError("The installation receipt identity does not match the canonical target and client.")
    install_plan = receipt.get("install_plan")
    if not isinstance(install_plan, dict):
        raise InstallError("The installation receipt is missing its reviewed install plan.")
    _validate_historical_install_plan(install_plan)
    if (
        receipt.get("inputs") != install_plan.get("inputs")
        or receipt.get("package") != install_plan.get("package")
        or receipt.get("platform") != install_plan.get("platform")
        or receipt.get("plan_sha256") != install_plan.get("plan_sha256")
    ):
        raise InstallError("The installation receipt metadata differs from its canonical install plan.")
    expected_operations = [_expected_receipt_operation(root, operation) for operation in install_plan["operations"]]
    if receipt.get("operations") != expected_operations:
        raise InstallError("The installation receipt operations exceed the canonical install plan authority.")


def _load_receipt(
    root: Path,
    client: str,
    runtime_base: str | None,
) -> tuple[Path, Path, dict[str, Any]] | None:
    runtime_root = _runtime_root(root, client, runtime_base)
    path = runtime_root / RUNTIME_RECEIPT
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("The installation receipt is not valid UTF-8 JSON.") from exc
    if not isinstance(receipt, dict):
        raise InstallError("The installation receipt must contain an object.")
    _validate_receipt(root, client, runtime_root, receipt)
    return path, runtime_root, receipt


def _receipt_file_path(root: Path, runtime_root: Path, operation: dict[str, Any], value: str) -> Path:
    if operation.get("kind") == "external_runtime":
        path = Path(value)
        try:
            path.resolve(strict=False).relative_to(runtime_root.resolve(strict=False))
        except ValueError as exc:
            raise InstallError("The receipt external-runtime path leaves its canonical directory.") from exc
        current = runtime_root
        for part in path.relative_to(runtime_root).parts:
            if current.exists() and _is_linklike(current):
                raise InstallError("The receipt external-runtime path crosses a symlink or junction.")
            current = current / part
        if _is_linklike(path):
            raise InstallError("The receipt external-runtime path is a symlink or junction.")
        return path
    return _safe_target(root, value)


def _managed_text_block_bounds(text: str, operation: dict[str, Any]) -> tuple[int, int] | None:
    content = operation.get("content")
    start_marker = operation.get("start_marker")
    end_marker = operation.get("end_marker")
    if not all(isinstance(value, str) and value for value in (content, start_marker, end_marker)):
        raise InstallError("The historical managed-text operation is incomplete.")
    if text.count(start_marker) != 1 or text.count(end_marker) != 1:
        return None
    beginning = text.index(start_marker)
    if beginning != 0 and text[beginning - 1] != "\n":
        return None
    end_beginning = text.index(end_marker)
    if end_beginning < beginning:
        return None
    finish = end_beginning + len(end_marker)
    if finish < len(text) and not text.startswith(("\r\n", "\n"), finish):
        return None
    if text[beginning:finish].strip() != content.strip():
        return None
    if text[finish : finish + 2] == "\r\n":
        finish += 2
    elif text[finish : finish + 1] == "\n":
        finish += 1
    return beginning, finish


def _codex_mcp_server(content: str) -> tuple[str, Any]:
    try:
        document = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("The historical Codex MCP content is invalid.") from exc
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict) or len(servers) != 1:
        raise InstallError("The historical Codex MCP content has an ambiguous server alias.")
    alias = next(iter(servers))
    return alias, servers[alias]


def _external_managed_text_is_owned(path: Path, operation: dict[str, Any]) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        current = tomllib.loads(text)
    except (FileNotFoundError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    alias, desired_server = _codex_mcp_server(str(operation.get("content", "")))
    servers = current.get("mcp_servers")
    if not isinstance(servers, dict) or alias not in servers or not _json_equal(servers[alias], desired_server):
        return False
    if operation.get("action") == "no_change":
        return True
    return _managed_text_block_bounds(text, operation) is not None


def _external_json_owned_desired(desired: Any) -> dict[str, Any]:
    if not isinstance(desired, dict):
        raise InstallError("The historical external JSON operation is invalid.")
    return {key: copy.deepcopy(value) for key, value in desired.items() if key != "$schema"}


def _managed_list_is_unambiguous(current: list[Any], expected: list[Any]) -> bool:
    expected_managed = [item for item in expected if _contains_managed_reference(item)]
    expected_identities = (
        set().union(*(_managed_hook_identities(item) for item in expected_managed))
        if expected_managed
        else set()
    )
    if expected_managed and len(expected_identities) != 1:
        return False
    for item in current:
        if not _contains_managed_reference(item):
            continue
        identities = _managed_hook_identities(item)
        if not identities:
            return False
        if identities.intersection(expected_identities) and not any(
            _json_equal(item, expected_item) for expected_item in expected_managed
        ):
            return False
    return True


def _json_contains_owned(current: Any, owned: Any, path: tuple[str, ...] = ()) -> bool:
    if _is_managed_hook_group_path(path):
        return _json_equal(current, owned)
    if isinstance(owned, dict):
        if not isinstance(current, dict):
            return False
        for key, value in owned.items():
            if key not in current:
                return False
            if path == () and key == "mcpServers":
                if not isinstance(value, dict) or not isinstance(current[key], dict):
                    return False
                if any(
                    alias not in current[key] or not _json_equal(current[key][alias], server)
                    for alias, server in value.items()
                ):
                    return False
                continue
            if not _json_contains_owned(current[key], value, (*path, key)):
                return False
        return True
    if isinstance(owned, list):
        if not isinstance(current, list):
            return False
        if not _managed_list_is_unambiguous(current, owned):
            return False
        return all(
            sum(1 for item in current if _json_equal(item, expected)) == 1
            for expected in owned
        )
    return _json_equal(current, owned)


def _subtract_json_value(value: Any, expected: Any, path: tuple[str, ...]) -> tuple[Any, bool]:
    if _is_managed_hook_group_path(path):
        if not _json_equal(value, expected):
            raise InstallError("A managed JSON hook changed after installation.")
        return None, True
    if _is_mcp_server_path(path):
        if not _json_equal(value, expected):
            raise InstallError("A managed JSON MCP setting changed after installation.")
        return None, True
    if isinstance(expected, dict):
        if not isinstance(value, dict):
            raise InstallError("A managed JSON setting changed after installation.")
        updated = copy.deepcopy(value)
        for key, child in expected.items():
            if key not in updated:
                raise InstallError("A managed JSON setting is missing during rollback.")
            if path == ("mcpServers",):
                if not _json_equal(updated[key], child):
                    raise InstallError("A managed JSON MCP setting changed after installation.")
                del updated[key]
                continue
            replacement, remove = _subtract_json_value(updated[key], child, (*path, key))
            if remove:
                del updated[key]
            else:
                updated[key] = replacement
        return updated, not updated
    if isinstance(expected, list):
        if not isinstance(value, list) or not _managed_list_is_unambiguous(value, expected):
            raise InstallError("A managed JSON hook changed after installation.")
        updated = copy.deepcopy(value)
        for item in expected:
            matches = [index for index, candidate in enumerate(updated) if _json_equal(candidate, item)]
            if len(matches) != 1:
                raise InstallError("A managed JSON hook changed after installation.")
            del updated[matches[0]]
        return updated, not updated
    if not _json_equal(value, expected):
        raise InstallError("A managed JSON setting changed after installation.")
    return None, True


def _apply_owned_json_changes(current: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(current)
    for change in reversed(changes):
        path = change.get("path")
        if not isinstance(path, list) or not path or not all(isinstance(part, str) for part in path):
            raise InstallError("The receipt contains an invalid JSON rollback path.")
        parent: Any = result
        for part in path[:-1]:
            if not isinstance(parent, dict) or part not in parent:
                raise InstallError("A managed JSON setting is missing during rollback.")
            parent = parent[part]
        key = path[-1]
        if change.get("kind") == "remove_key":
            if not isinstance(parent, dict) or key not in parent:
                raise InstallError("A managed JSON setting is missing during rollback.")
            replacement, remove = _subtract_json_value(parent[key], change.get("value"), tuple(path))
            if remove:
                del parent[key]
            else:
                parent[key] = replacement
            continue
        if change.get("kind") == "remove_list_value":
            if not isinstance(parent, dict) or not isinstance(parent.get(key), list):
                raise InstallError("A managed JSON hook changed after installation.")
            values = parent[key]
            expected = change.get("value")
            if not _managed_list_is_unambiguous(values, [expected]):
                raise InstallError("A managed JSON hook changed after installation.")
            matches = [index for index, value in enumerate(values) if _json_equal(value, expected)]
            if len(matches) != 1:
                raise InstallError("A managed JSON hook changed after installation.")
            del values[matches[0]]
            continue
        raise InstallError("The receipt contains an unsupported JSON rollback operation.")
    return result


def _external_json_is_owned(path: Path, operation: dict[str, Any]) -> bool:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    if not _json_contains_owned(document, _external_json_owned_desired(operation.get("desired"))):
        return False
    try:
        _apply_owned_json_changes(document, _external_json_rollback_changes(operation))
    except InstallError:
        return False
    return True


def _receipt_operation_state(
    root: Path,
    runtime_root: Path,
    operation: dict[str, Any],
    plan_operation: dict[str, Any],
) -> dict[str, Any]:
    state = {"id": operation.get("id"), "target": operation.get("target"), "state": "unchanged"}
    if operation.get("kind") in {"copy_tree", "external_runtime"}:
        changed: list[str] = []
        expected_files: set[Path] = set()
        for entry in operation.get("files", []):
            path = _receipt_file_path(root, runtime_root, operation, entry["path"])
            expected_files.add(path)
            if _file_hash(path) != entry["sha256"]:
                changed.append(entry["path"])
        tree_root = runtime_root if operation.get("kind") == "external_runtime" else _safe_target(root, operation["target"])
        allowed_extra_files: set[Path] = set()
        allowed_extra_directories: set[Path] = set()
        if operation.get("kind") == "external_runtime":
            allowed_extra_files.add(runtime_root / RUNTIME_RECEIPT)
            state_root = runtime_root / "state"
            if state_root.exists():
                allowed_extra_directories.add(state_root)
                allowed_extra_files.update(Path(entry["path"]) for entry in _runtime_state_files(runtime_root))
        expected_directories = {tree_root}
        for expected in expected_files:
            parent = expected.parent
            while parent != tree_root.parent:
                expected_directories.add(parent)
                if parent == tree_root:
                    break
                parent = parent.parent
        if tree_root.exists():
            for actual in sorted(tree_root.rglob("*")):
                if _is_linklike(actual):
                    changed.append(_normal_path(actual))
                elif actual.is_file() and actual not in expected_files and actual not in allowed_extra_files:
                    changed.append(_normal_path(actual))
                elif actual.is_dir() and actual not in expected_directories and actual not in allowed_extra_directories:
                    changed.append(_normal_path(actual) + "/")
        if changed:
            state.update({"state": "modified", "changed_paths": sorted(set(changed))})
        return state
    path = _operation_target(root, operation)
    current_sha256 = _file_hash(path)
    state["current_sha256"] = current_sha256
    if operation.get("kind") == "external_managed_text":
        unchanged = _external_managed_text_is_owned(path, plan_operation)
    elif operation.get("kind") == "external_json_merge":
        unchanged = _external_json_is_owned(path, plan_operation)
    else:
        unchanged = current_sha256 == operation.get("after_sha256")
    if not unchanged:
        state["state"] = "modified"
    return state


def _status(root: Path, client: str, runtime_base: str | None) -> dict[str, Any]:
    loaded = _load_receipt(root, client, runtime_base)
    if loaded is None:
        return {
            "schema_version": 1,
            "status": "not_installed",
            "client": client,
            "target_root": _normal_path(root),
            "operations": [],
        }
    _, runtime_root, receipt = loaded
    plan_operations = {operation["id"]: operation for operation in receipt["install_plan"]["operations"]}
    states = [
        _receipt_operation_state(root, runtime_root, operation, plan_operations[operation["id"]])
        for operation in receipt["operations"]
    ]
    modified = [state for state in states if state["state"] == "modified"]
    return {
        "schema_version": 1,
        "status": "modified" if modified else "installed",
        "client": client,
        "target_root": _normal_path(root),
        "runtime_root": _normal_path(runtime_root),
        "package": receipt.get("package"),
        "plan_sha256": receipt.get("plan_sha256"),
        "receipt": receipt.get("receipt"),
        "trusted_installer": receipt["install_plan"].get("trusted_installer"),
        "token_env": receipt["inputs"].get("token_env"),
        "operations": states,
    }


def _remove_json_path(document: dict[str, Any], change: dict[str, Any]) -> None:
    path = change.get("path")
    if not isinstance(path, list) or not path or not all(isinstance(part, str) for part in path):
        raise InstallError("The receipt contains an invalid JSON rollback path.")
    parent: Any = document
    for part in path[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            raise InstallError("A managed JSON setting is missing during rollback.")
        parent = parent[part]
    key = path[-1]
    if change["kind"] == "remove_key":
        if not isinstance(parent, dict) or key not in parent or not _json_equal(parent[key], change.get("value")):
            raise InstallError("A managed JSON setting changed after installation.")
        del parent[key]
        return
    if change["kind"] == "remove_list_value":
        if not isinstance(parent, dict) or not isinstance(parent.get(key), list):
            raise InstallError("A managed JSON hook changed after installation.")
        values = parent[key]
        matches = [index for index, value in enumerate(values) if _json_equal(value, change.get("value"))]
        if len(matches) != 1:
            raise InstallError("A managed JSON hook changed after installation.")
        del values[matches[0]]
        return
    raise InstallError("The receipt contains an unsupported JSON rollback operation.")


def _rollback_operation(
    root: Path,
    runtime_root: Path,
    operation: dict[str, Any],
    plan_operation: dict[str, Any],
    expected_current_sha256: str | None,
    transaction: _Transaction,
) -> int:
    if operation.get("action") == "no_change":
        return 0
    kind = operation.get("kind")
    if kind in {"copy_tree", "external_runtime"}:
        files = operation.get("files", [])
        for entry in reversed(files):
            transaction.remove(
                _receipt_file_path(root, runtime_root, operation, entry["path"]),
                expected_sha256=entry["sha256"],
                private_output=kind == "external_runtime",
            )
        return len(files)
    target = _operation_target(root, operation)
    rollback = operation.get("rollback", {})
    rollback_kind = rollback.get("kind")
    if rollback_kind == "remove_managed_block":
        try:
            text = target.read_bytes().decode("utf-8")
        except (FileNotFoundError, UnicodeDecodeError) as exc:
            raise InstallError(f"Managed text cannot be safely rolled back: {operation['target']}") from exc
        bounds = _managed_text_block_bounds(text, plan_operation)
        if bounds is None or not _external_managed_text_is_owned(target, plan_operation):
            raise InstallError(f"Managed text changed after installation: {operation['target']}")
        beginning, finish = bounds
        retained_separator = ""
        if operation.get("action") == "append":
            suffix = plan_operation.get("append_text")
            start_marker = plan_operation.get("start_marker")
            if isinstance(suffix, str) and isinstance(start_marker, str):
                marker_offset = suffix.find(start_marker)
                candidate_beginning = beginning - marker_offset
                if (
                    marker_offset >= 0
                    and candidate_beginning >= 0
                    and text[candidate_beginning:finish] == suffix
                ):
                    beginning = candidate_beginning
                    unmanaged_tail = text[finish:]
                    if (
                        beginning > 0
                        and unmanaged_tail
                        and text[beginning - 1] not in "\r\n"
                        and not unmanaged_tail.startswith(("\r", "\n"))
                    ):
                        retained_separator = "\n"
        restored = text[:beginning] + retained_separator + text[finish:]
        if operation.get("action") == "create" and not restored.strip():
            transaction.remove(
                target,
                expected_sha256=expected_current_sha256,
                private_output=kind == "external_managed_text",
            )
        else:
            transaction.write(
                target,
                restored.encode("utf-8"),
                expected_before=expected_current_sha256,
                private_output=True,
            )
        return 1
    if rollback_kind == "remove_owned_json":
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError(f"Managed JSON cannot be safely rolled back: {operation['target']}") from exc
        desired = plan_operation.get("desired")
        changes = rollback.get("changes")
        if not isinstance(document, dict) or not isinstance(desired, dict) or not isinstance(changes, list):
            raise InstallError(f"Managed JSON cannot be safely rolled back: {operation['target']}")
        restored = _apply_owned_json_changes(document, changes)
        transaction.write(
            target,
            _json_text(restored).encode("utf-8"),
            expected_before=expected_current_sha256,
            private_output=True,
        )
        return 1
    if rollback_kind == "remove_created_file":
        transaction.remove(
            target,
            expected_sha256=expected_current_sha256,
            private_output=str(kind).startswith("external_"),
        )
        return 1
    if rollback_kind == "remove_suffix":
        body = target.read_bytes()
        suffix = str(rollback.get("suffix", "")).encode("utf-8")
        if not suffix or not body.endswith(suffix):
            raise InstallError(f"Managed suffix changed after installation: {operation['target']}")
        restored = body[: -len(suffix)]
        if _sha256_bytes(restored) != operation.get("before_sha256"):
            raise InstallError(f"Managed suffix cannot be safely removed: {operation['target']}")
        transaction.write(
            target,
            restored,
            expected_before=expected_current_sha256,
            private_output=kind == "external_managed_text",
        )
        return 1
    if rollback_kind == "json_inverse":
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError(f"Managed JSON cannot be safely rolled back: {operation['target']}") from exc
        if not isinstance(document, dict):
            raise InstallError(f"Managed JSON cannot be safely rolled back: {operation['target']}")
        for change in reversed(rollback.get("changes", [])):
            _remove_json_path(document, change)
        transaction.write(
            target,
            _json_text(document).encode("utf-8"),
            expected_before=expected_current_sha256,
            private_output=kind == "external_json_merge",
        )
        return 1
    raise InstallError(f"Unsupported rollback operation: {rollback_kind}")


def _runtime_state_files(runtime_root: Path) -> list[dict[str, str]]:
    state_root = runtime_root / "state"
    if not state_root.exists():
        return []
    if _is_linklike(state_root) or not state_root.is_dir():
        raise InstallError("The external runtime state path is not a regular directory.")
    allowed_name = re.compile(
        r"(?:[A-Za-z0-9_-]{1,120}\.(?:baseline|loop)\.json|pending-sync\.json|skill-update\.json|"
        r"instructions-proj_[0-9A-HJKMNP-TV-Z]{26}-[a-f0-9]{16}\.json)"
    )
    files: list[dict[str, str]] = []
    for path in sorted(state_root.rglob("*")):
        if _is_linklike(path):
            raise InstallError("The external runtime state contains a symlink or junction.")
        if path.is_dir():
            if path != state_root:
                raise InstallError("The external runtime state contains an unexpected subdirectory.")
            continue
        if not path.is_file() or path.parent != state_root or not allowed_name.fullmatch(path.name):
            raise InstallError(f"The external runtime state contains an unexpected path: {path}")
        files.append({"path": _normal_path(path), "sha256": _sha256_file(path)})
    return files


def _rollback_preview_operation(operation: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    action = operation.get("action")
    rollback = operation.get("rollback", {})
    preview: dict[str, Any] = {
        "id": operation["id"],
        "kind": operation["kind"],
        "target": operation["target"],
        "expected_current_sha256": state.get("current_sha256", operation.get("after_sha256")),
    }
    if action == "no_change":
        preview["action"] = "no_change"
    elif operation["kind"] in {"copy_tree", "external_runtime"}:
        preview.update({"action": "remove_created_files", "files": operation["files"]})
    elif rollback.get("kind") == "remove_created_file":
        preview["action"] = "remove_created_file"
    elif rollback.get("kind") == "remove_suffix":
        preview.update({"action": "remove_managed_suffix", "suffix": rollback["suffix"]})
    elif rollback.get("kind") == "remove_managed_block":
        preview["action"] = "remove_managed_block"
    elif rollback.get("kind") == "remove_owned_json":
        preview.update({"action": "remove_owned_json", "changes": rollback["changes"]})
    elif rollback.get("kind") == "json_inverse":
        preview.update({"action": "remove_managed_json_values", "changes": rollback["changes"]})
    else:
        raise InstallError(f"The receipt has no canonical rollback for {operation['id']}.")
    return preview


def _rollback_plan_digest(plan: dict[str, Any]) -> str:
    payload = {key: value for key, value in plan.items() if key != "rollback_plan_sha256"}
    return _sha256_bytes(_canonical_json_bytes(payload))


def _build_rollback_plan(root: Path, client: str, runtime_base: str | None) -> dict[str, Any]:
    loaded = _load_receipt(root, client, runtime_base)
    if loaded is None:
        raise InstallError("No installation receipt exists for this client.")
    receipt_path, runtime_root, receipt = loaded
    plan_operations = {operation["id"]: operation for operation in receipt["install_plan"]["operations"]}
    states = [
        _receipt_operation_state(root, runtime_root, operation, plan_operations[operation["id"]])
        for operation in receipt["operations"]
    ]
    states_by_id = {state["id"]: state for state in states}
    conflicts = [
        {"operation": state["id"], "target": state["target"], "message": "Installed content changed after apply."}
        for state in states
        if state["state"] == "modified"
    ]
    state_files = _runtime_state_files(runtime_root)
    operations = [
        _rollback_preview_operation(operation, states_by_id[operation["id"]])
        for operation in reversed(receipt["operations"])
    ]
    operations.append(
        {
            "id": "external-runtime-state",
            "kind": "external_runtime_state",
            "target": _normal_path(runtime_root / "state"),
            "action": "remove_runtime_state" if state_files else "no_change",
            "files": state_files,
        }
    )
    operations.append(
        {
            "id": "installation-receipt",
            "kind": "external_receipt",
            "target": _normal_path(receipt_path),
            "action": "remove_receipt",
            "expected_current_sha256": _sha256_file(receipt_path),
        }
    )
    plan: dict[str, Any] = {
        "schema_version": 1,
        "command": "rollback",
        "preview_only": True,
        "mutations_performed": 0,
        "client": client,
        "target_root": _normal_path(root),
        "runtime_root": _normal_path(runtime_root),
        "receipt": _normal_path(receipt_path),
        "install_plan_sha256": receipt["plan_sha256"],
        "inputs": {
            "client": client,
            "target_root": _normal_path(root),
            "runtime_base": _normal_path(runtime_root.parent),
        },
        "operations": operations,
        "conflicts": conflicts,
        "warnings": [
            "No files were changed. Review every removal before accepting this rollback plan.",
            "The receipt checksum detects corruption only; canonical paths and this accepted rollback digest bound deletion authority.",
        ],
    }
    plan["rollback_plan_sha256"] = _rollback_plan_digest(plan)
    return plan


def _verify_rollback_plan(plan: dict[str, Any], accepted: str) -> tuple[Path, Path, Path, dict[str, Any]]:
    if plan.get("schema_version") != 1 or plan.get("command") != "rollback":
        raise InstallError("Unsupported rollback plan schema.")
    claimed = plan.get("rollback_plan_sha256")
    if not isinstance(claimed, str) or claimed != _rollback_plan_digest(plan):
        raise InstallError("The rollback plan content checksum is invalid.")
    if accepted != claimed:
        raise InstallError("The accepted rollback-plan SHA-256 does not exactly match this plan.")
    inputs = plan.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"client", "target_root", "runtime_base"}:
        raise InstallError("The rollback plan is missing its canonical inputs.")
    if inputs.get("client") not in _client_names() or not all(isinstance(value, str) for value in inputs.values()):
        raise InstallError("The rollback plan canonical inputs are invalid.")
    root = _validated_root(inputs["target_root"])
    reconstructed = _build_rollback_plan(root, inputs["client"], inputs["runtime_base"])
    if _canonical_json_bytes(plan) != _canonical_json_bytes(reconstructed):
        raise InstallError("The rollback plan no longer matches the canonical receipt and current files.")
    if plan.get("conflicts"):
        raise InstallError("The rollback plan contains changed installed files and cannot be applied.")
    loaded = _load_receipt(root, inputs["client"], inputs["runtime_base"])
    if loaded is None:
        raise InstallError("The installation receipt disappeared before rollback.")
    receipt_path, runtime_root, receipt = loaded
    return root, runtime_root, receipt_path, receipt


def _execute_rollback(plan: dict[str, Any], accepted: str) -> dict[str, Any]:
    root, runtime_root, receipt_path, receipt = _verify_rollback_plan(plan, accepted)
    plan_operations = {operation["id"]: operation for operation in receipt["install_plan"]["operations"]}
    states = [
        _receipt_operation_state(root, runtime_root, operation, plan_operations[operation["id"]])
        for operation in receipt["operations"]
    ]
    if any(state["state"] == "modified" for state in states):
        raise InstallError("Rollback refused because installed files changed after the accepted rollback plan.")
    transaction = _Transaction()
    mutations = 0
    rollback_operations = {operation["id"]: operation for operation in plan["operations"]}
    try:
        for operation in reversed(receipt["operations"]):
            preview_operation = rollback_operations[operation["id"]]
            mutations += _rollback_operation(
                root,
                runtime_root,
                operation,
                plan_operations[operation["id"]],
                preview_operation.get("expected_current_sha256"),
                transaction,
            )
        state_operation = next(operation for operation in plan["operations"] if operation["id"] == "external-runtime-state")
        for entry in state_operation["files"]:
            state_path = Path(entry["path"])
            try:
                state_path.relative_to(runtime_root / "state")
            except ValueError as exc:
                raise InstallError("Rollback runtime-state path exceeds the canonical runtime.") from exc
            if _file_hash(state_path) != entry["sha256"]:
                raise InstallError("External runtime state changed after the accepted rollback plan.")
            transaction.remove(
                state_path,
                expected_sha256=entry["sha256"],
                private_output=True,
            )
            mutations += 1
        if _sha256_file(receipt_path) != next(
            operation["expected_current_sha256"] for operation in plan["operations"] if operation["id"] == "installation-receipt"
        ):
            raise InstallError("The installation receipt changed after the accepted rollback plan.")
        transaction.remove(
            receipt_path,
            expected_sha256=next(
                operation["expected_current_sha256"]
                for operation in plan["operations"]
                if operation["id"] == "installation-receipt"
            ),
            private_output=True,
        )
        mutations += 1
    except Exception:
        transaction.rollback()
        raise

    cleanup_directories: set[Path] = {receipt_path.parent, runtime_root / "state"}
    for operation in receipt["operations"]:
        if operation["kind"] == "external_runtime":
            parents = [Path(entry["path"]).parent for entry in operation.get("files", [])]
            boundary = runtime_root.parent
        elif str(operation.get("kind", "")).startswith("external_"):
            parents = []
            boundary = root
        else:
            labels = [operation.get("target"), *(entry.get("path") for entry in operation.get("files", []))]
            parents = [_safe_target(root, label).parent for label in labels if isinstance(label, str)]
            boundary = root
        for parent in parents:
            while parent != boundary:
                cleanup_directories.add(parent)
                parent = parent.parent
    for directory in sorted(cleanup_directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "schema_version": 1,
        "status": "rolled_back",
        "client": receipt["client"],
        "target_root": _normal_path(root),
        "runtime_root": _normal_path(runtime_root),
        "install_plan_sha256": receipt["plan_sha256"],
        "rollback_plan_sha256": plan["rollback_plan_sha256"],
        "mutations_performed": mutations,
    }


def _write_output(value: dict[str, Any], output: str | None, output_format: str) -> None:
    json_body = _json_text(value)
    plan_path: str | None = None
    if output:
        output_path = Path(output).expanduser().resolve(strict=False)
        if output_path.exists() or _is_linklike(output_path):
            raise InstallError("The plan output already exists; refusing to overwrite it.")
        _atomic_write(output_path, json_body.encode("utf-8"), create_only=True)
        plan_path = _normal_path(output_path)
        if output_format == "json":
            return
    if output_format == "json":
        sys.stdout.write(json_body)
        return
    sys.stdout.write(_text_result(value, plan_path=plan_path))


def _text_result(value: dict[str, Any], plan_path: str | None = None) -> str:
    if "rollback_plan_sha256" in value and value.get("preview_only"):
        lines = [
            "ROLLBACK PLAN - 0 mutations performed",
            f"Client: {value['client']}",
            f"Target: {value['target_root']}",
            f"Runtime: {value['runtime_root']}",
            f"Rollback plan SHA-256: {value['rollback_plan_sha256']}",
        ]
        lines.extend(f"- {operation['action']}: {operation['target']}" for operation in value["operations"])
        if value["conflicts"]:
            lines.append("Conflicts:")
            lines.extend(f"- {entry['operation']}: {entry['message']}" for entry in value["conflicts"])
        lines.append("No files were changed.")
        return "\n".join(lines) + "\n"
    if "plan_sha256" in value and value.get("preview_only"):
        source = (value.get("package") or {}).get("source") or {}
        inputs = value.get("inputs") or {}
        lines = [
            "INSTALL PLAN - 0 mutations performed",
            f"Client: {value['client']}",
            f"Target: {value['target_root']}",
            f"Runtime: {value['runtime_root']}",
        ]
        repository = source.get("repository_url")
        branch = source.get("branch")
        commit = source.get("commit_sha")
        if repository or commit:
            lines.append("Source: " + " ".join(part for part in (repository, branch, commit) if part))
        if inputs.get("project_id"):
            lines.append(f"Project: {inputs['project_id']}")
        if inputs.get("token_env"):
            lines.append(f"Credential environment variable: {inputs['token_env']}")
        if inputs.get("api_base_url"):
            lines.append(f"Origin: {inputs['api_base_url']}")
        lines.append(f"Plan SHA-256: {value['plan_sha256']}")
        lines.append("Operations:")
        lines.extend(f"- {operation['action']}: {operation['target']}" for operation in value["operations"])
        if value.get("conflicts"):
            lines.append("Conflicts:")
            lines.extend(f"- {entry['operation']}: {entry['message']}" for entry in value["conflicts"])
        for warning in value.get("warnings") or []:
            lines.append(f"Warning: {warning}")
        lines.append("No files were changed.")
        python = inputs.get("python_executable") or "<absolute-python>"
        saved_plan = plan_path or "<saved-plan.json>"
        installer = _normal_path(Path(__file__).resolve())
        lines.append("Apply only after you accept this exact digest:")
        lines.append(
            f'"{python}" -I "{installer}" apply --plan "{saved_plan}" '
            f'--accept-plan-sha256 "{value["plan_sha256"]}"'
        )
        return "\n".join(lines) + "\n"
    return _json_text(value)


def _preview_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a deterministic, non-mutating manual installation preview.")
    parser.add_argument("--client", choices=_client_names(), required=True)
    parser.add_argument("--target-root", required=True, help="Repository that would receive the installation.")
    parser.add_argument("--platform", choices=PLATFORMS, default="auto")
    parser.add_argument("--project-id", default="proj_REPLACE_WITH_PROJECT_ULID")
    parser.add_argument("--api-base-url", default="https://verify.example.test")
    parser.add_argument("--token-env", help="Optional assertion of the project-derived credential variable name.")
    parser.add_argument("--runtime-base")
    parser.add_argument("--client-config-dir")
    parser.add_argument("--python-executable")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def preview_main(argv: list[str] | None = None) -> int:
    try:
        _assert_running_python_version()
        arguments = _preview_parser().parse_args(argv)
        preview = build_preview(arguments)
        sys.stdout.write(_json_text(preview) if arguments.format == "json" else _text_preview(preview))
        return 0
    except InstallError as exc:
        sys.stderr.write(f"Installer preview failed: {exc}\n")
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    client_help = (
        "Coding-agent client. Omit to auto-detect from the agent environment or a unique project marker."
    )
    plan = subparsers.add_parser("plan", help="Render a complete non-mutating installation plan")
    plan.add_argument("--client", choices=_client_names(), help=client_help)
    plan.add_argument("--target-root", required=True)
    plan.add_argument("--platform", choices=PLATFORMS, default="auto")
    plan.add_argument("--project-id", default="proj_REPLACE_WITH_PROJECT_ULID")
    plan.add_argument("--api-base-url", default="https://verify.example.test")
    plan.add_argument("--token-env", help="Optional assertion of the project-derived credential variable name.")
    plan.add_argument("--runtime-base")
    plan.add_argument("--client-config-dir")
    plan.add_argument("--python-executable")
    plan.add_argument("--git-executable")
    plan.add_argument("--format", choices=("text", "json"), default="json")
    plan.add_argument("--output", help="Optional caller-selected plan file")
    apply = subparsers.add_parser("apply", help="Apply a saved plan after accepting its exact SHA-256")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--accept-plan-sha256", required=True)
    apply.add_argument("--format", choices=("text", "json"), default="json")
    status_parser = subparsers.add_parser("status", help="Inspect a receipt without changing files")
    status_parser.add_argument("--client", choices=_client_names(), help=client_help)
    status_parser.add_argument("--target-root", required=True)
    status_parser.add_argument("--runtime-base")
    status_parser.add_argument("--format", choices=("text", "json"), default="json")
    rollback_plan = subparsers.add_parser("rollback-plan", help="Render a non-mutating canonical rollback plan")
    rollback_plan.add_argument("--client", choices=_client_names(), help=client_help)
    rollback_plan.add_argument("--target-root", required=True)
    rollback_plan.add_argument("--runtime-base")
    rollback_plan.add_argument("--format", choices=("text", "json"), default="json")
    rollback_plan.add_argument("--output", help="Optional caller-selected rollback plan file")
    rollback_parser = subparsers.add_parser("rollback", help="Apply an accepted canonical rollback plan")
    rollback_parser.add_argument("--plan", required=True)
    rollback_parser.add_argument("--accept-rollback-plan-sha256", required=True)
    rollback_parser.add_argument("--format", choices=("text", "json"), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        _assert_running_python_version()
        arguments = _parser().parse_args(argv)
        if arguments.command in {"plan", "status", "rollback-plan"}:
            arguments.client = _detect_client(
                explicit=arguments.client,
                target_root=Path(arguments.target_root),
                environ=os.environ,
            )
        if arguments.command == "plan":
            _assert_client_install_supported(arguments.client)
            result = _build_plan(arguments)
            _write_output(result, arguments.output, arguments.format)
        elif arguments.command == "apply":
            install_plan = _load_json_file(arguments.plan)
            plan_client = install_plan.get("client")
            if not isinstance(plan_client, str):
                raise InstallError("The install plan has no client identity.")
            _assert_client_install_supported(plan_client)
            result = _apply_plan(install_plan, arguments.accept_plan_sha256)
            _write_output(result, None, arguments.format)
        elif arguments.command == "status":
            result = _status(_validated_root(arguments.target_root), arguments.client, arguments.runtime_base)
            _write_output(result, None, arguments.format)
        elif arguments.command == "rollback-plan":
            result = _build_rollback_plan(
                _validated_root(arguments.target_root),
                arguments.client,
                arguments.runtime_base,
            )
            _write_output(result, arguments.output, arguments.format)
        else:
            result = _execute_rollback(
                _load_json_file(arguments.plan),
                arguments.accept_rollback_plan_sha256,
            )
            _write_output(result, None, arguments.format)
        return 0
    except (InstallError, OSError) as exc:
        sys.stderr.write(f"Installer failed: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
