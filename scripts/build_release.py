#!/usr/bin/env python3
"""Build deterministic public bundles from the canonical production source.

Publishable bundles must come from a clean ``main`` checkout of the canonical
Acceptora Agent Skill repository. The Git repository remains the source and
update authority; the ZIP is a directly extractable installation convenience.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from validate_checklist_payload import find_secret_paths  # noqa: E402


ARCHIVE_PREFIX = "acceptora"
CANONICAL_REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
PRODUCTION_BRANCH = "main"
EMBEDDED_PROVENANCE_FILENAME = "acceptora-agent-skill-provenance.json"
CLIENT_REGISTRY_PATH = "config/client-profiles.json"
EXCLUDED_PARTS = {".git", ".github", ".pytest_cache", "__pycache__", "dist", "tests", ".verification"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".deferred"}
EXCLUDED_FILES = {
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
SECRET_FILE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "service-account.json"}
SECRET_FILE_SUFFIXES = {".key", ".p12", ".pfx"}
SECRET_PATTERNS = (
    re.compile(br"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(br"avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}"),
    re.compile(br"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
STRUCTURED_CONFIG_SUFFIXES = {".conf", ".config", ".ini", ".json", ".toml", ".yaml", ".yml"}
CONFIG_ASSIGNMENT = re.compile(
    r"^\s*[\"']?(?P<key>[A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*(?P<value>.*?)\s*,?\s*$"
)
MAX_FILE_SIZE = 8 * 1024 * 1024
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ReleaseError(RuntimeError):
    """Raised when a release cannot be built safely and deterministically."""


def _is_semantic_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = SEMVER_PATTERN.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group(4)
    return prerelease is None or not any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in prerelease.split(".")
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _normal_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return _validate_relative(relative)


def _validate_relative(relative: str) -> str:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ReleaseError(f"Unsafe release path: {relative}")
    if re.match(r"^[A-Za-z]:", relative) or "\\" in relative or re.search(r"[\x00-\x1f\x7f]", relative):
        raise ReleaseError(f"Unsafe release path: {relative}")
    return relative


def _normal_mode(relative: str) -> int:
    return 0o755 if relative.endswith(".py") else 0o644


def _is_excluded(relative: str, dist_directory: Path, candidate: Path) -> bool:
    if candidate == dist_directory or dist_directory in candidate.parents:
        return True
    parts = PurePosixPath(relative).parts
    return (
        any(part in EXCLUDED_PARTS for part in parts)
        or PurePosixPath(relative).suffix in EXCLUDED_SUFFIXES
        or relative in EXCLUDED_FILES
    )


def _structured_config_looks_secret(relative: str, body: bytes) -> bool:
    suffixes = set(PurePosixPath(relative).suffixes)
    if not suffixes.intersection(STRUCTURED_CONFIG_SUFFIXES):
        return False
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if decoded is not None and find_secret_paths(decoded):
        return True
    for line in text.splitlines():
        match = CONFIG_ASSIGNMENT.fullmatch(line)
        if match is None:
            continue
        candidate = match.group("value").strip().strip("\"'")
        if find_secret_paths({match.group("key"): candidate}):
            return True
    return False


def _looks_secret(relative: str, body: bytes) -> bool:
    name = PurePosixPath(relative).name.lower()
    if name in SECRET_FILE_NAMES or name.startswith(".env."):
        return True
    if PurePosixPath(name).suffix in SECRET_FILE_SUFFIXES:
        return True
    return any(pattern.search(body) for pattern in SECRET_PATTERNS) or _structured_config_looks_secret(relative, body)


def _validate_root(path_value: str) -> Path:
    requested = Path(path_value).expanduser().absolute()
    if any(path.exists() and path.is_symlink() for path in (requested, *requested.parents)):
        raise ReleaseError("The release source path must not cross a symlink.")
    path = requested.resolve(strict=False)
    if not path.exists() or not path.is_dir():
        raise ReleaseError("The release source must be an existing, non-symlink directory.")
    return path


def _validate_dist(path_value: str, source_root: Path) -> Path:
    requested = Path(path_value).expanduser().absolute()
    if any(path.exists() and path.is_symlink() for path in (requested, *requested.parents)):
        raise ReleaseError("The release destination must not cross a symlink.")
    dist = requested.resolve(strict=False)
    if dist == source_root:
        raise ReleaseError("The release destination must not be the package root.")
    if dist.exists() or dist.is_symlink():
        raise ReleaseError("The release destination must not already exist.")
    return dist


def _read_stable_file(candidate: Path, relative: str) -> tuple[bytes, os.stat_result]:
    before = candidate.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise ReleaseError(f"Release source contains a symlink: {relative}")
    if not stat.S_ISREG(before.st_mode):
        raise ReleaseError(f"Release source contains an unsupported filesystem entry: {relative}")
    if before.st_size > MAX_FILE_SIZE:
        raise ReleaseError(f"Release source file exceeds the size limit: {relative}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise ReleaseError(f"Release source could not be opened safely: {relative}") from error

    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise ReleaseError(f"Release source is not a regular file: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            body = handle.read(MAX_FILE_SIZE + 1)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    after = candidate.lstat()
    identity_before = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
        opened_before.st_mode,
    )
    identity_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_mode,
    )
    path_identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)

    if len(body) > MAX_FILE_SIZE:
        raise ReleaseError(f"Release source file exceeds the size limit: {relative}")
    if identity_before != identity_after or identity_after != path_identity_after or len(body) != opened_after.st_size:
        raise ReleaseError(f"Release source changed while it was being captured: {relative}")

    return body, opened_after


def _collect_files(source_root: Path, dist_directory: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    for candidate in sorted(source_root.rglob("*"), key=lambda path: path.relative_to(source_root).as_posix()):
        relative = _normal_relative(candidate, source_root)
        if _is_excluded(relative, dist_directory, candidate):
            continue
        if candidate.is_dir():
            continue
        folded = relative.casefold()
        if folded in casefolded and casefolded[folded] != relative:
            raise ReleaseError(f"Release source contains case-colliding paths: {casefolded[folded]} and {relative}")
        casefolded[folded] = relative
        body, metadata = _read_stable_file(candidate, relative)
        if _looks_secret(relative, body):
            raise ReleaseError(f"Release source appears to contain a credential: {relative}")
        files.append(
            {
                "path": relative,
                "archive_path": f"{ARCHIVE_PREFIX}/{relative}",
                "size": metadata.st_size,
                "mode": format(_normal_mode(relative), "04o"),
                "sha256": _sha256_bytes(body),
                "body": body,
            }
        )
    if not any(entry["path"] == "SKILL.md" for entry in files):
        raise ReleaseError("Release source is missing SKILL.md.")
    if not any(entry["path"] == "config/package-manifest.json" for entry in files):
        raise ReleaseError("Release source is missing config/package-manifest.json.")
    if not any(entry["path"] == CLIENT_REGISTRY_PATH for entry in files):
        raise ReleaseError(f"Release source is missing {CLIENT_REGISTRY_PATH}.")
    return files


def _load_package_manifest(files: list[dict[str, Any]]) -> dict[str, Any]:
    entry = next((item for item in files if item["path"] == "config/package-manifest.json"), None)
    if entry is None:
        raise ReleaseError("Release source is missing a regular package manifest.")
    try:
        value = json.loads(entry["body"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("The package manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise ReleaseError("The package manifest must contain an object.")
    skill = value.get("skill")
    if not isinstance(skill, dict) or skill.get("name") != ARCHIVE_PREFIX:
        raise ReleaseError("The package manifest has an unexpected skill identity.")
    version = skill.get("version")
    if not _is_semantic_version(version):
        raise ReleaseError("The package manifest has an invalid semantic version.")
    distribution = value.get("distribution")
    if not isinstance(distribution, dict):
        raise ReleaseError("The package manifest is missing its distribution identity.")
    if distribution.get("repository_url") != CANONICAL_REPOSITORY_URL:
        raise ReleaseError("The package manifest does not name the canonical repository.")
    if distribution.get("branch") != PRODUCTION_BRANCH:
        raise ReleaseError("The package manifest does not name the production main branch.")
    return value


def _load_client_registry(files: list[dict[str, Any]]) -> dict[str, Any]:
    entries = {entry["path"]: entry for entry in files}
    entry = entries.get(CLIENT_REGISTRY_PATH)
    if entry is None:
        raise ReleaseError("Release source is missing a regular client provider registry.")
    try:
        value = json.loads(entry["body"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("The client provider registry is not valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ReleaseError("The client provider registry has an unsupported schema.")
    reviewed_on = value.get("capabilities_reviewed_on")
    try:
        if not isinstance(reviewed_on, str) or date.fromisoformat(reviewed_on).isoformat() != reviewed_on:
            raise ValueError
    except ValueError as exc:
        raise ReleaseError("The client provider registry has an invalid capability review date.") from exc
    profiles = value.get("clients")
    if not isinstance(profiles, list) or not profiles:
        raise ReleaseError("The client provider registry does not contain client profiles.")

    identifiers: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ReleaseError("The client provider registry contains an invalid profile.")
        client = profile.get("id")
        if (
            not isinstance(client, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", client) is None
            or client in identifiers
        ):
            raise ReleaseError("The client provider registry contains an invalid or duplicate client ID.")
        identifiers.add(client)
        if not isinstance(profile.get("reference_build"), str) or not profile["reference_build"].strip():
            raise ReleaseError(f"The client profile has no reference build: {client}")
        minimum_build = profile.get("minimum_build")
        if minimum_build is not None and (not isinstance(minimum_build, str) or not minimum_build.strip()):
            raise ReleaseError(f"The client profile has an invalid minimum build: {client}")

        project = profile.get("project_layout")
        user_config = profile.get("user_config")
        templates = profile.get("templates")
        lifecycle = profile.get("lifecycle")
        official_docs = profile.get("official_docs")
        runtime_adapters = profile.get("runtime_adapters")
        if not all(isinstance(item, dict) for item in (project, user_config, templates, lifecycle, official_docs)):
            raise ReleaseError(f"The client profile is incomplete: {client}")
        if not isinstance(runtime_adapters, list) or not runtime_adapters:
            raise ReleaseError(f"The client profile has invalid runtime adapters: {client}")

        referenced_sources: list[str] = []
        for field in ("skill_directory", "instruction_file", "instruction_source"):
            relative = project.get(field)
            if not isinstance(relative, str):
                raise ReleaseError(f"The client project layout is missing {field}: {client}")
            _validate_relative(relative)
        referenced_sources.append(project["instruction_source"])
        default_directory = user_config.get("default_directory")
        if not isinstance(default_directory, str):
            raise ReleaseError(f"The client profile has no default configuration directory: {client}")
        _validate_relative(default_directory)
        for field in ("settings", "mcp"):
            target = user_config.get(field)
            if not isinstance(target, dict) or target.get("base") not in {"client_config", "client_config_parent"}:
                raise ReleaseError(f"The client profile has an invalid {field} configuration target: {client}")
            relative = target.get("path")
            if not isinstance(relative, str):
                raise ReleaseError(f"The client profile has no {field} configuration path: {client}")
            _validate_relative(relative)

        hooks = templates.get("hooks")
        mcp = templates.get("mcp")
        if not isinstance(hooks, dict) or not isinstance(hooks.get("default"), str):
            raise ReleaseError(f"The client profile has no default hook template: {client}")
        referenced_sources.append(hooks["default"])
        overrides = hooks.get("platform_overrides", {})
        if not isinstance(overrides, dict) or any(
            platform not in {"windows", "posix"} or not isinstance(relative, str)
            for platform, relative in overrides.items()
        ):
            raise ReleaseError(f"The client profile has invalid hook template overrides: {client}")
        referenced_sources.extend(overrides.values())
        if (
            not isinstance(mcp, dict)
            or not isinstance(mcp.get("path"), str)
            or mcp.get("renderer") not in {"codex_toml", "claude_json", "gemini_json"}
        ):
            raise ReleaseError(f"The client profile has an invalid MCP template: {client}")
        referenced_sources.append(mcp["path"])
        if any(not isinstance(relative, str) for relative in runtime_adapters):
            raise ReleaseError(f"The client profile has invalid runtime adapters: {client}")
        referenced_sources.extend(runtime_adapters)
        for relative in referenced_sources:
            _validate_relative(relative)
            if relative not in entries:
                raise ReleaseError(f"The client profile references a missing release file: {relative}")

        baseline_events = lifecycle.get("baseline_events")
        if not isinstance(baseline_events, list) or not baseline_events or any(
            not isinstance(event, str) or not event for event in baseline_events
        ):
            raise ReleaseError(f"The client profile has invalid baseline events: {client}")
        if any(not isinstance(lifecycle.get(field), str) or not lifecycle[field] for field in ("completion_event", "update_check_event")):
            raise ReleaseError(f"The client profile has an invalid lifecycle: {client}")
        discovery_checks = profile.get("discovery_checks")
        if not isinstance(discovery_checks, list) or not discovery_checks or any(
            not isinstance(check, str) or not check for check in discovery_checks
        ):
            raise ReleaseError(f"The client profile has invalid discovery checks: {client}")
        if set(official_docs) != {"skills", "hooks", "mcp", "configuration"} or any(
            not isinstance(url, str) or not url.startswith("https://") for url in official_docs.values()
        ):
            raise ReleaseError(f"The client profile has invalid official documentation links: {client}")
    return value


def _validated_git_executable(source_root: Path) -> Path | None:
    candidate = shutil.which("git")
    if candidate is None:
        return None

    unresolved = Path(candidate).expanduser().absolute()
    if unresolved.is_symlink():
        raise ReleaseError("The Git executable used for release provenance must not be a symlink.")
    executable = unresolved.resolve(strict=True)
    if not executable.is_file():
        raise ReleaseError("The Git executable used for release provenance is not a regular file.")
    worktree_root = next(
        (
            candidate
            for candidate in (source_root, *source_root.parents)
            if (candidate / ".git").is_dir() or (candidate / ".git").is_file()
        ),
        source_root,
    )
    try:
        executable.relative_to(worktree_root)
    except ValueError:
        return executable
    raise ReleaseError("The Git executable used for release provenance must be outside the release worktree.")


def _run_git(executable: Path, source_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        [
            str(executable),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(source_root),
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
        env=environment,
    )


def _git_text(executable: Path, repository_root: Path, *arguments: str) -> str:
    result = _run_git(executable, repository_root, *arguments)
    if result.returncode != 0 or result.stderr.strip():
        raise ReleaseError("The canonical release source identity could not be verified.")
    try:
        value = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ReleaseError("The canonical release source identity is not valid UTF-8.") from error
    if not value or "\n" in value or "\r" in value:
        raise ReleaseError("The canonical release source identity is invalid.")
    return value


def _normalized_repository_url(value: str) -> str:
    normalized = value.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _canonical_source_identity(
    git_executable: Path,
    repository_root: Path,
    head: str,
) -> dict[str, str]:
    observed_url = _git_text(git_executable, repository_root, "remote", "get-url", "origin")
    if _normalized_repository_url(observed_url) != CANONICAL_REPOSITORY_URL:
        raise ReleaseError("A publishable bundle must use the canonical HTTPS origin.")
    branch = _git_text(git_executable, repository_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != PRODUCTION_BRANCH:
        raise ReleaseError("A publishable bundle must be built from the production main branch.")
    branch_commit = _git_text(
        git_executable,
        repository_root,
        "rev-parse",
        "--verify",
        f"refs/heads/{PRODUCTION_BRANCH}^{{commit}}",
    ).lower()
    if branch_commit != head:
        raise ReleaseError("The release checkout HEAD does not match its production main branch.")
    return {
        "source_repository_url": CANONICAL_REPOSITORY_URL,
        "source_branch": PRODUCTION_BRANCH,
    }


def _source_provenance(
    source_root: Path,
    git_executable: Path | None,
    override: str | None,
    allow_dirty: bool,
) -> dict[str, str]:
    if override and override != "UNVERSIONED" and not re.fullmatch(r"[0-9a-fA-F]{7,64}", override):
        raise ReleaseError("The source commit must be a hexadecimal revision or UNVERSIONED.")

    if git_executable is None:
        if not allow_dirty:
            raise ReleaseError("Git is required for a publishable clean release; use --allow-dirty only for a local candidate.")
        if override not in {None, "UNVERSIONED"}:
            raise ReleaseError("A hexadecimal source commit cannot be verified without Git.")
        return {"source_commit": "UNVERSIONED", "source_state": "unversioned"}

    root_result = _run_git(git_executable, source_root, "rev-parse", "--show-toplevel")
    if root_result.returncode != 0 or root_result.stderr.strip():
        if not allow_dirty:
            raise ReleaseError("A publishable release must be built from a clean Git worktree.")
        if override not in {None, "UNVERSIONED"}:
            raise ReleaseError("A hexadecimal source commit cannot be verified outside a Git worktree.")
        return {"source_commit": "UNVERSIONED", "source_state": "unversioned"}

    repository_root = Path(root_result.stdout.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    try:
        relative = source_root.relative_to(repository_root).as_posix() or "."
    except ValueError as error:
        raise ReleaseError("The release source is outside its reported Git worktree.") from error

    if os.name == "nt" and not allow_dirty:
        line_endings = _run_git(git_executable, repository_root, "config", "--local", "--get", "core.autocrlf")
        if line_endings.returncode != 0 or line_endings.stderr.strip():
            raise ReleaseError(
                "A publishable Windows release requires repository-local core.autocrlf=false; "
                "run `git config --local core.autocrlf false`."
            )
        try:
            line_ending_setting = line_endings.stdout.decode("ascii", errors="strict").strip().lower()
        except UnicodeDecodeError as error:
            raise ReleaseError("The repository-local core.autocrlf setting is invalid.") from error
        if line_ending_setting != "false":
            raise ReleaseError(
                "A publishable Windows release requires repository-local core.autocrlf=false; "
                "run `git config --local core.autocrlf false`."
            )

    head_result = _run_git(git_executable, repository_root, "rev-parse", "HEAD")
    status_result = _run_git(
        git_executable,
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative,
    )
    if (
        head_result.returncode != 0
        or status_result.returncode != 0
        or head_result.stderr.strip()
        or status_result.stderr.strip()
    ):
        raise ReleaseError("The release source Git provenance could not be verified.")

    head = head_result.stdout.decode("ascii", errors="strict").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise ReleaseError("The release source Git revision is invalid.")
    dirty = bool(status_result.stdout.strip())

    if dirty:
        if not allow_dirty:
            raise ReleaseError("The release source has uncommitted or untracked files; build from a clean checkout.")
        if override not in {None, "UNVERSIONED"}:
            raise ReleaseError("A dirty release source cannot claim a hexadecimal source commit.")
        return {"source_commit": "UNVERSIONED", "source_state": "dirty_allowed"}

    if override == "UNVERSIONED":
        return {"source_commit": "UNVERSIONED", "source_state": "unversioned_requested"}
    if override and not head.startswith(override.lower()):
        raise ReleaseError("The requested source commit does not match the clean checkout HEAD.")

    return {
        "source_commit": head,
        "source_state": "clean",
        **_canonical_source_identity(git_executable, repository_root, head),
    }


def _collect_git_files(source_root: Path, git_executable: Path, commit: str) -> list[dict[str, Any]]:
    root_result = _run_git(git_executable, source_root, "rev-parse", "--show-toplevel")
    if root_result.returncode != 0 or root_result.stderr.strip():
        raise ReleaseError("The immutable release source root could not be resolved.")
    repository_root = Path(root_result.stdout.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    try:
        source_prefix = source_root.relative_to(repository_root).as_posix()
    except ValueError as error:
        raise ReleaseError("The release source is outside its reported Git worktree.") from error

    tree_result = _run_git(
        git_executable,
        repository_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        "--",
        source_prefix,
    )
    if tree_result.returncode != 0 or tree_result.stderr.strip():
        raise ReleaseError("The immutable release source tree could not be enumerated.")

    files: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    prefix = "" if source_prefix in {"", "."} else source_prefix.rstrip("/") + "/"
    for record in tree_result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, repository_path_bytes = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii", errors="strict").split(" ", 2)
            repository_path = repository_path_bytes.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise ReleaseError("The immutable release source contains an unsupported Git tree entry.") from error
        if prefix and not repository_path.startswith(prefix):
            raise ReleaseError("The immutable release source escaped the package path.")
        relative = _validate_relative(repository_path[len(prefix) :])
        if _is_excluded(relative, source_root / "dist", source_root / relative):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ReleaseError(f"Release source contains an unsupported Git entry: {relative}")
        folded = relative.casefold()
        if folded in casefolded and casefolded[folded] != relative:
            raise ReleaseError(f"Release source contains case-colliding paths: {casefolded[folded]} and {relative}")
        casefolded[folded] = relative

        blob_result = _run_git(git_executable, repository_root, "cat-file", "blob", object_id)
        if blob_result.returncode != 0 or blob_result.stderr.strip():
            raise ReleaseError(f"The immutable release source blob could not be read: {relative}")
        body = blob_result.stdout
        if len(body) > MAX_FILE_SIZE:
            raise ReleaseError(f"Release source file exceeds the size limit: {relative}")
        if _looks_secret(relative, body):
            raise ReleaseError(f"Release source appears to contain a credential: {relative}")
        files.append(
            {
                "path": relative,
                "archive_path": f"{ARCHIVE_PREFIX}/{relative}",
                "size": len(body),
                "mode": format(_normal_mode(relative), "04o"),
                "sha256": _sha256_bytes(body),
                "body": body,
            }
        )

    if not any(entry["path"] == "SKILL.md" for entry in files):
        raise ReleaseError("Release source is missing SKILL.md.")
    if not any(entry["path"] == "config/package-manifest.json" for entry in files):
        raise ReleaseError("Release source is missing config/package-manifest.json.")
    if not any(entry["path"] == CLIENT_REGISTRY_PATH for entry in files):
        raise ReleaseError(f"Release source is missing {CLIENT_REGISTRY_PATH}.")
    return files


def _release_files(
    source_root: Path,
    dist_directory: Path,
    git_executable: Path | None,
    provenance: dict[str, str],
) -> list[dict[str, Any]]:
    if provenance["source_state"] == "clean":
        if git_executable is None:
            raise ReleaseError("Git is required to capture a publishable release from immutable source blobs.")
        return _collect_git_files(source_root, git_executable, provenance["source_commit"])
    return _collect_files(source_root, dist_directory)


def _atomic_output(path: Path, writer: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_bytes_atomic(path: Path, body: bytes) -> None:
    def writer(temporary: Path) -> None:
        with temporary.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_output(path, writer)


def _write_zip(path: Path, files: list[dict[str, Any]]) -> None:
    def writer(temporary: Path) -> None:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry in sorted(files, key=lambda item: item["archive_path"]):
                info = zipfile.ZipInfo(entry["archive_path"], date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | int(entry["mode"], 8)) << 16
                info.flag_bits |= 0x800
                archive.writestr(info, entry["body"], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    _atomic_output(path, writer)


def _public_file_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("path", "archive_path", "size", "mode", "sha256")}


def _embedded_provenance(
    provenance: dict[str, str],
    source_tree_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if provenance["source_state"] != "clean":
        return None
    record = {
        "schema_version": 1,
        "repository_url": provenance["source_repository_url"],
        "branch": provenance["source_branch"],
        "commit_sha": provenance["source_commit"],
        "source_tree_sha256": source_tree_sha256,
    }
    body = _json_bytes(record)
    entry = {
        "path": EMBEDDED_PROVENANCE_FILENAME,
        "archive_path": EMBEDDED_PROVENANCE_FILENAME,
        "size": len(body),
        "mode": "0644",
        "sha256": _sha256_bytes(body),
        "body": body,
    }
    return record, entry


def build_release(
    source_root_value: str,
    dist_directory_value: str,
    commit_override: str | None = None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    source_root = _validate_root(source_root_value)
    dist_directory = _validate_dist(dist_directory_value, source_root)
    git_executable = _validated_git_executable(source_root)
    provenance = _source_provenance(source_root, git_executable, commit_override, allow_dirty)
    files = _release_files(source_root, dist_directory, git_executable, provenance)
    package = _load_package_manifest(files)
    client_registry = _load_client_registry(files)
    client_profiles = client_registry["clients"]
    version = package["skill"]["version"]
    zip_name = f"{ARCHIVE_PREFIX}-{version}.zip"
    public_files = [_public_file_entry(entry) for entry in files]
    source_tree_sha256 = _sha256_bytes(_canonical_json_bytes(public_files))
    embedded_provenance = _embedded_provenance(provenance, source_tree_sha256)
    zip_files = [*files]
    if embedded_provenance is not None:
        zip_files.append(embedded_provenance[1])
    dist_directory.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{dist_directory.name}.", suffix=".tmp", dir=dist_directory.parent)
    )

    try:
        zip_path = staging_directory / zip_name
        _write_zip(zip_path, zip_files)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "name": package["skill"]["name"],
            "version": version,
            "integration_version": package.get("integration", {}).get("version"),
            "contract_version": package.get("contract", {}).get("version"),
            "mcp_protocol_version": package.get("contract", {}).get("mcp_protocol_version"),
            "distribution": package["distribution"],
            **provenance,
            "source_tree_sha256": source_tree_sha256,
            "client_capabilities_reviewed_on": client_registry["capabilities_reviewed_on"],
            "supported_clients": [profile["id"] for profile in client_profiles],
            "reference_client_builds": {
                profile["id"]: profile["reference_build"]
                for profile in client_profiles
            },
            "minimum_client_builds": {
                profile["id"]: profile["minimum_build"]
                for profile in client_profiles
                if profile.get("minimum_build") is not None
            },
            "archive_prefix": ARCHIVE_PREFIX,
            "files": public_files,
            "artifacts": [
                {
                    "filename": zip_name,
                    "format": "zip",
                    "size": zip_path.stat().st_size,
                    "sha256": _sha256_file(zip_path),
                },
            ],
        }
        if embedded_provenance is not None:
            manifest["embedded_provenance"] = {
                "filename": EMBEDDED_PROVENANCE_FILENAME,
                "sha256": embedded_provenance[1]["sha256"],
            }
        manifest_path = staging_directory / "release-manifest.json"
        manifest_body = _json_bytes(manifest)
        _write_bytes_atomic(manifest_path, manifest_body)
        checksum_entries = [
            (artifact["sha256"].removeprefix("sha256:"), artifact["filename"])
            for artifact in manifest["artifacts"]
        ]
        checksum_entries.append((_sha256_bytes(manifest_body).removeprefix("sha256:"), manifest_path.name))
        checksum_entries.sort(key=lambda entry: entry[1])
        checksums = "".join(f"{digest}  {filename}\n" for digest, filename in checksum_entries)
        checksums_path = staging_directory / "SHA256SUMS"
        _write_bytes_atomic(checksums_path, checksums.encode("ascii"))

        verified_files = [
            _public_file_entry(entry)
            for entry in _release_files(source_root, dist_directory, git_executable, provenance)
        ]
        if verified_files != public_files or _source_provenance(
            source_root,
            git_executable,
            commit_override,
            allow_dirty,
        ) != provenance:
            raise ReleaseError("The release source changed before the artifacts were finalized.")
        if dist_directory.exists() or dist_directory.is_symlink():
            raise ReleaseError("The release destination appeared while the build was running.")

        os.replace(staging_directory, dist_directory)
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise

    return {
        "schema_version": 1,
        "status": "built",
        "dist_directory": dist_directory.as_posix(),
        "version": version,
        **provenance,
        "source_tree_sha256": source_tree_sha256,
        "artifacts": manifest["artifacts"],
        "manifest": {"filename": "release-manifest.json", "sha256": _sha256_bytes(manifest_body)},
        "checksums": "SHA256SUMS",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-dir",
        required=True,
        help="Caller-selected directory for public release outputs",
    )
    parser.add_argument("--source-root", default=str(PACKAGE_ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--source-commit")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Allow a local uncommitted source snapshot and mark it UNVERSIONED; "
            "the resulting candidate has no installable provenance and must never be published"
        ),
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = build_release(
            arguments.source_root,
            arguments.dist_dir,
            arguments.source_commit,
            arguments.allow_dirty,
        )
        if arguments.format == "json":
            sys.stdout.write(_json_bytes(result).decode("utf-8"))
        else:
            sys.stdout.write(
                f"Built acceptora {result['version']} in {result['dist_directory']}\n"
                f"Source tree: {result['source_tree_sha256']}\n"
            )
        return 0
    except (OSError, ReleaseError) as exc:
        sys.stderr.write(f"Release build failed: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
