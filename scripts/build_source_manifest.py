#!/usr/bin/env python3
"""Build deterministic baseline and changed-surface manifests.

The strict Git adapter hashes every eligible tracked worktree file plus every
eligible, non-ignored untracked file. It never trusts Git's stat-cache-based
dirty-path discovery.
The filesystem adapter is an explicit best-effort fallback for non-Git sources.
No source bodies are written to the manifest.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = "1.0"
FULL_CAPTURE_MODE = "full_worktree_bytes_v1"
GIT_COMMAND_TIMEOUT_SECONDS = 15
DEFAULT_IGNORES = (
    ".git/**",
    ".verification/**",
)


class ManifestError(RuntimeError):
    """Raised for deterministic adapter failures."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalise_relative(path: str) -> str:
    value = path.replace("\\", "/")
    if value == ".":
        return ""
    while value.startswith("./"):
        value = value[2:]
    if not value:
        return ""
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ManifestError(f"source path must be repository-relative: {path}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"source path contains an unsafe segment: {path}")
    return "/".join(parts)


def _is_ignored(path: str, patterns: Iterable[str]) -> bool:
    value = _normalise_relative(path)
    for raw_pattern in patterns:
        pattern = _normalise_relative(raw_pattern)
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if value == prefix or value.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatchcase(value, pattern):
            return True
    return False


def _run_git(
    root: Path,
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
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
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ManifestError(f"git {' '.join(args)} exceeded the {GIT_COMMAND_TIMEOUT_SECONDS}s limit") from error
    if check and process.returncode != 0:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestError(f"git {' '.join(args)} failed: {message or process.returncode}")
    return process


def find_repository_root(start: str | os.PathLike[str]) -> Path:
    candidate = Path(start).resolve()
    process = _run_git(candidate, "rev-parse", "--show-toplevel", check=False)
    if process.returncode == 0:
        return Path(process.stdout.decode("utf-8", errors="strict").strip()).resolve()
    return candidate


def _sanitise_remote(remote: str) -> str:
    value = remote.strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    # Preserve SSH-style user@host:path locators; reject accidental secret-like userinfo.
    return re.sub(r"^[^@/]+@(?=[^:]+:)", "git@", value)


def _is_junction(path: Path) -> bool:
    predicate = getattr(path, "is_junction", None)
    if predicate and predicate():
        return True
    if os.name == "nt" and not path.is_symlink():
        try:
            attributes = path.lstat().st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _assert_contained(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ManifestError(f"source path leaves the repository root: {path}") from error


def _assert_parent_contained(path: Path, root: Path) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ManifestError(f"source path parent leaves the repository root: {path}") from error


def _hash_file(path: Path, root: Path) -> tuple[str, int, str]:
    _assert_parent_contained(path, root)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        body = os.readlink(path).encode("utf-8", errors="surrogateescape")
        after = path.lstat()
        if _stat_identity(before) != _stat_identity(after):
            raise ManifestError(f"source symlink changed during capture: {path}")
        _assert_parent_contained(path, root)
        return hashlib.sha256(body).hexdigest(), len(body), "symlink"
    if _is_junction(path):
        raise ManifestError(f"source path is a junction and cannot be captured safely: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise ManifestError(f"source path is not a regular file or symlink: {path}")
    _assert_contained(path, root)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ManifestError(f"source file could not be opened without following links: {path}") from error
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        before_identity = _stat_identity(before)
        opened_identity = _stat_identity(opened)
        if os.name == "nt":
            before_identity = before_identity[:-1]
            opened_identity = opened_identity[:-1]
        if not stat.S_ISREG(opened.st_mode) or before_identity != opened_identity:
            raise ManifestError(f"source file identity changed before capture: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            handle.seek(0)
            verification_digest = hashlib.sha256()
            verification_size = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                verification_digest.update(chunk)
                verification_size += len(chunk)
        if size != verification_size or digest.hexdigest() != verification_digest.hexdigest():
            raise ManifestError(f"source file changed during capture: {path}")
        finished = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = path.lstat()
    opened_identity = _stat_identity(opened)
    finished_identity = _stat_identity(finished)
    after_identity = _stat_identity(after)
    if os.name == "nt":
        changed = opened_identity != finished_identity or finished_identity[:-1] != after_identity[:-1]
    else:
        changed = _stat_identity(before) != finished_identity or finished_identity != after_identity
    if changed:
        raise ManifestError(f"source file changed during capture: {path}")
    _assert_contained(path, root)
    return digest.hexdigest(), size, "file"


def _working_entry(root: Path, relative: str) -> dict[str, Any]:
    path = root / Path(relative)
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "sha256": None, "size": 0, "kind": "missing"}
    if _is_junction(path):
        raise ManifestError(f"source path is a junction and cannot be captured safely: {path}")
    if path.is_symlink():
        checksum, size, kind = _hash_file(path, root)
        return {"exists": True, "sha256": checksum, "size": size, "kind": kind}
    if path.is_dir():
        return {"exists": True, "sha256": None, "size": 0, "kind": "directory"}
    checksum, size, kind = _hash_file(path, root)
    return {"exists": True, "sha256": checksum, "size": size, "kind": kind}


def _decode_git_path(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ManifestError("strict Git source capture requires UTF-8 repository paths") from error
    if (
        "\\" in decoded
        or decoded.startswith("/")
        or re.match(r"^[A-Za-z]:", decoded)
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
        or any(part in {"", ".", ".."} for part in decoded.split("/"))
    ):
        raise ManifestError("strict Git source capture encountered an unsafe or non-portable repository path")
    return decoded


def _tracked_index_entries(root: Path) -> dict[str, dict[str, str]]:
    process = _run_git(root, "ls-files", "-s", "-z")
    entries: dict[str, dict[str, str]] = {}
    for record in (value for value in process.stdout.split(b"\0") if value):
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3:
            raise ManifestError("Git returned an invalid tracked-file index record")
        mode_bytes, object_id_bytes, stage = fields
        try:
            mode = mode_bytes.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise ManifestError("Git returned an invalid tracked-file mode") from error
        if stage != b"0":
            raise ManifestError("strict Git source capture does not support unresolved index stages")
        if mode == "160000":
            raise ManifestError("strict Git source capture does not support Git submodules")
        if mode not in {"100644", "100755", "120000"}:
            raise ManifestError(f"strict Git source capture does not support tracked mode {mode}")
        try:
            object_id = object_id_bytes.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise ManifestError("Git returned an invalid tracked-file object ID") from error
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None:
            raise ManifestError("Git returned an invalid tracked-file object ID")
        relative = _decode_git_path(raw_path)
        if relative in entries:
            raise ManifestError(f"Git returned a duplicate tracked path: {relative}")
        entries[relative] = {"mode": mode, "object_id": object_id}
    return entries


def _assert_no_hidden_index_entries(root: Path) -> None:
    process = _run_git(root, "ls-files", "-v", "-z")
    for record in (value for value in process.stdout.split(b"\0") if value):
        tag, separator, raw_path = record[:1], record[1:2], record[2:]
        if separator != b" " or not raw_path:
            raise ManifestError("Git returned an invalid tracked-file flag record")
        if tag == b"S" or tag.islower():
            relative = _decode_git_path(raw_path)
            raise ManifestError(
                f"strict Git source capture does not support assume-unchanged or skip-worktree paths: {relative}"
            )


def _untracked_paths(root: Path) -> set[str]:
    process = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = {_decode_git_path(value) for value in process.stdout.split(b"\0") if value}
    return paths


def _filesystem_relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
        encoded = relative.encode("utf-8", errors="strict")
    except (UnicodeEncodeError, ValueError) as error:
        raise ManifestError("strict Git source capture requires UTF-8 repository paths") from error
    return _decode_git_path(encoded)


def _git_ignored_paths(root: Path, candidates: Iterable[str]) -> set[str]:
    queries: dict[bytes, str] = {}
    for relative in sorted(set(candidates)):
        validated = _decode_git_path(relative.encode("utf-8", errors="strict"))
        query = validated.encode("utf-8")
        queries[query] = validated
    if not queries:
        return set()

    process = _run_git(
        root,
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        check=False,
        input_bytes=b"\0".join(queries) + b"\0",
    )
    if process.returncode not in {0, 1}:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestError(f"Git ignore query failed: {message or process.returncode}")
    if process.stderr:
        raise ManifestError("Git ignore query returned unexpected diagnostics")
    if process.stdout and not process.stdout.endswith(b"\0"):
        raise ManifestError("Git ignore query returned an invalid path list")

    records = process.stdout[:-1].split(b"\0") if process.stdout else []
    if len(records) != len(set(records)) or any(record not in queries for record in records):
        raise ManifestError("Git ignore query returned an unexpected path")
    if (process.returncode == 0) != bool(records):
        raise ManifestError("Git ignore query returned an inconsistent result")
    return {queries[record] for record in records}


def _assert_no_git_omitted_special_files(root: Path, ignores: tuple[str, ...]) -> None:
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise ManifestError(f"source directory could not be inspected safely: {root}") from error
    pending_directories = [(root, _stat_identity(root_metadata))]

    while pending_directories:
        candidates: list[tuple[str, Path, os.stat_result, str]] = []
        for directory, expected_identity in pending_directories:
            try:
                before = directory.lstat()
            except OSError as error:
                raise ManifestError(f"source directory could not be inspected safely: {directory}") from error
            if _stat_identity(before) != expected_identity:
                raise ManifestError(f"source directory changed during capture: {directory}")
            if stat.S_ISLNK(before.st_mode) or _is_junction(directory) or not stat.S_ISDIR(before.st_mode):
                raise ManifestError(f"source directory cannot be traversed safely: {directory}")
            _assert_contained(directory, root)

            discovered: list[str] = []
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        discovered.append(entry.name)
            except OSError as error:
                raise ManifestError(f"source directory could not be scanned safely: {directory}") from error
            try:
                after = directory.lstat()
            except OSError as error:
                raise ManifestError(f"source directory changed during capture: {directory}") from error
            if _stat_identity(before) != _stat_identity(after):
                raise ManifestError(f"source directory changed during capture: {directory}")

            for name in sorted(discovered):
                path = directory / name
                relative = _filesystem_relative_path(root, path)
                try:
                    metadata = path.lstat()
                except OSError as error:
                    raise ManifestError(f"source path could not be inspected safely: {path}") from error
                if _is_ignored(relative, ignores) or stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    continue
                if _is_junction(path):
                    candidates.append((relative, path, metadata, "junction"))
                elif stat.S_ISDIR(metadata.st_mode):
                    candidates.append((relative, path, metadata, "directory"))
                else:
                    candidates.append((relative, path, metadata, "special"))

        ignored = _git_ignored_paths(
            root,
            (relative for relative, _, _, _ in candidates),
        )
        next_directories: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
        for relative, path, metadata, kind in candidates:
            if relative in ignored:
                continue
            if kind == "directory":
                next_directories.append((path, _stat_identity(metadata)))
                continue
            if kind == "junction":
                raise ManifestError(f"source path is a junction and cannot be captured safely: {path}")
            raise ManifestError(f"source path is not a regular file or symlink: {path}")
        pending_directories = next_directories


def _assert_eligible_path_ancestors(root: Path, relative: str) -> None:
    current = root
    for part in Path(relative).parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ManifestError(f"source path ancestor could not be inspected safely: {current}") from error
        if stat.S_ISLNK(metadata.st_mode) or _is_junction(current):
            raise ManifestError(f"source path ancestor is a symlink or junction: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ManifestError(f"source path ancestor is not a directory: {current}")
        _assert_contained(current, root)


def _working_git_mode(path: Path, entry: dict[str, Any], index_mode: str | None) -> str | None:
    if not entry["exists"]:
        return None
    if entry["kind"] == "symlink":
        return "120000"
    if entry["kind"] != "file":
        return None
    if os.name != "nt":
        return "100755" if path.stat().st_mode & 0o111 else "100644"
    return index_mode if index_mode in {"100644", "100755"} else "100644"


def _git_head(root: Path) -> str:
    process = _run_git(root, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    if process.returncode == 0:
        return process.stdout.decode("ascii", errors="strict").strip()
    unborn = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no", check=False)
    if unborn.returncode == 0:
        return "UNBORN"
    raise ManifestError("Git HEAD cannot be resolved and the repository is not a valid unborn worktree")


def _git_branch(root: Path) -> str:
    process = _run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return process.stdout.decode("utf-8", errors="replace").strip() if process.returncode == 0 else "DETACHED"


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    core = {
        "adapter": snapshot["adapter"],
        "capture_mode": snapshot.get("capture_mode"),
        "repository": snapshot["repository"],
        "head": snapshot.get("head"),
        "entries": snapshot["entries"],
    }
    return digest_value(core)


def _capture_git(root: Path, ignores: tuple[str, ...]) -> dict[str, Any]:
    remote_process = _run_git(root, "config", "--get", "remote.origin.url", check=False)
    remote = _sanitise_remote(remote_process.stdout.decode("utf-8", errors="replace"))
    repository = remote or f"local:{root.name}"
    _assert_no_hidden_index_entries(root)
    tracked = _tracked_index_entries(root)
    untracked = _untracked_paths(root)
    if set(tracked) & untracked:
        raise ManifestError("Git returned the same path as both tracked and untracked")
    entries = []
    for relative in sorted(set(tracked) | untracked):
        index_entry = tracked.get(relative)
        if index_entry is None and _is_ignored(relative, ignores):
            continue
        path = root / Path(relative)
        _assert_eligible_path_ancestors(root, relative)
        entry = _working_entry(root, relative)
        index_mode = index_entry["mode"] if index_entry is not None else None
        if index_entry is None and entry["kind"] == "directory":
            raise ManifestError(
                f"strict Git source capture does not support untracked nested repositories or directory boundaries: {relative}"
            )
        if index_mode is not None and entry["kind"] == "directory":
            entry = {"exists": False, "sha256": None, "size": 0, "kind": "missing"}
        entries.append(
            {
                "path": relative,
                **entry,
                "git_mode": _working_git_mode(path, entry, index_mode),
                "index_mode": index_mode,
                "index_object_id": index_entry["object_id"] if index_entry is not None else None,
            }
        )
    _assert_no_git_omitted_special_files(root, ignores)
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "adapter": "git-v1",
        "guarantee": "strict",
        "capture_mode": FULL_CAPTURE_MODE,
        "repository": repository,
        "head": _git_head(root),
        "branch": _git_branch(root),
        "entries": entries,
        "ignored_patterns": list(ignores),
    }
    snapshot["source_digest"] = _snapshot_digest(snapshot)
    return snapshot


def _iter_files(root: Path, ignores: tuple[str, ...]) -> Iterable[tuple[str, Path]]:
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_directory = _normalise_relative(str(directory_path.relative_to(root)))
        kept = []
        for name in sorted(names):
            relative = _normalise_relative(f"{relative_directory}/{name}" if relative_directory else name)
            if not _is_ignored(relative, ignores):
                kept.append(name)
        names[:] = kept
        for name in sorted(files):
            path = directory_path / name
            relative = _normalise_relative(str(path.relative_to(root)))
            if not _is_ignored(relative, ignores):
                yield relative, path


def _capture_filesystem(root: Path, ignores: tuple[str, ...]) -> dict[str, Any]:
    entries = [{"path": relative, **_working_entry(root, relative)} for relative, _ in _iter_files(root, ignores)]
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "adapter": "filesystem-v1",
        "guarantee": "best_effort",
        "capture_mode": FULL_CAPTURE_MODE,
        "repository": f"filesystem:{root.name}",
        "head": None,
        "branch": None,
        "entries": entries,
        "ignored_patterns": list(ignores),
    }
    snapshot["source_digest"] = _snapshot_digest(snapshot)
    return snapshot


def capture_snapshot(
    root: str | os.PathLike[str],
    adapter: str = "auto",
    extra_ignores: Iterable[str] = (),
) -> dict[str, Any]:
    source_root = Path(root).resolve()
    ignores = tuple(dict.fromkeys((*DEFAULT_IGNORES, *extra_ignores)))
    is_git = _run_git(source_root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    if adapter == "git" and not is_git:
        raise ManifestError(f"{source_root} is not inside a Git worktree")
    if adapter in {"auto", "git"} and is_git:
        return _capture_git(find_repository_root(source_root), ignores)
    if adapter in {"auto", "filesystem"}:
        return _capture_filesystem(source_root, ignores)
    raise ManifestError(f"unsupported adapter: {adapter}")


def _entry_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = snapshot.get("entries", [])
    if not isinstance(entries, list):
        raise ManifestError("source snapshot entries must be a list")
    mapped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ManifestError("source snapshot contains an invalid entry")
        relative = _normalise_relative(entry["path"])
        if not relative or relative in mapped:
            raise ManifestError(f"source snapshot contains a duplicate or empty path: {relative}")
        mapped[relative] = entry
    return mapped


def _source_descriptor(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "adapter": snapshot["adapter"],
        "guarantee": snapshot["guarantee"],
        "capture_mode": snapshot.get("capture_mode"),
        "repository": snapshot["repository"],
        "head": snapshot.get("head"),
        "branch": snapshot.get("branch"),
        "source_digest": snapshot["source_digest"],
    }


def compare_with_baseline(
    baseline: dict[str, Any],
    root: str | os.PathLike[str],
    extra_ignores: Iterable[str] = (),
) -> dict[str, Any]:
    requested_adapter = "git" if baseline.get("adapter") == "git-v1" else "filesystem"
    if requested_adapter == "git" and baseline.get("capture_mode") != FULL_CAPTURE_MODE:
        raise ManifestError("the Git baseline was not captured with full worktree byte hashing")
    source_root = find_repository_root(root) if baseline.get("adapter") == "git-v1" else Path(root).resolve()
    current = capture_snapshot(source_root, requested_adapter, extra_ignores)
    if baseline.get("repository") != current.get("repository"):
        raise ManifestError("baseline repository identity does not match the current source")

    baseline_entries = _entry_map(baseline)
    current_entries = _entry_map(current)
    if requested_adapter == "git" and any(
        entry.get("git_mode") == "160000" or entry.get("index_mode") == "160000"
        for entry in baseline_entries.values()
    ):
        raise ManifestError("strict Git source capture does not support Git submodules")
    candidates = set(baseline_entries) | set(current_entries)

    ignores = tuple(dict.fromkeys((*DEFAULT_IGNORES, *baseline.get("ignored_patterns", []), *extra_ignores)))
    changes: list[dict[str, Any]] = []
    for relative in sorted(candidates):
        if requested_adapter != "git" and _is_ignored(relative, ignores):
            continue
        before = (
            {
                key: baseline_entries[relative].get(key)
                for key in (
                    "exists", "sha256", "size", "kind", "git_mode", "index_mode", "index_object_id"
                )
            }
            if relative in baseline_entries
            else {
                "exists": False,
                "sha256": None,
                "size": 0,
                "kind": "missing",
                "git_mode": None,
                "index_mode": None,
                "index_object_id": None,
            }
        )

        if relative in current_entries:
            after = {
                key: current_entries[relative].get(key)
                for key in (
                    "exists", "sha256", "size", "kind", "git_mode", "index_mode", "index_object_id"
                )
            }
        else:
            after = {
                "exists": False,
                "sha256": None,
                "size": 0,
                "kind": "missing",
                "git_mode": None,
                "index_mode": None,
                "index_object_id": None,
            }

        if before == after:
            continue
        if not before["exists"] and after["exists"]:
            change_kind = "added"
        elif before["exists"] and not after["exists"]:
            change_kind = "deleted"
        else:
            change_kind = "modified"
        changes.append(
            {
                "anchor": f"file:{relative}",
                "path": relative,
                "change": change_kind,
                "base_sha256": before["sha256"],
                "current_sha256": after["sha256"],
                "current_size": after["size"],
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "adapter": current["adapter"],
        "guarantee": current["guarantee"],
        "repository": current["repository"],
        "base": _source_descriptor(baseline),
        "current": _source_descriptor(current),
        "entries": changes,
    }
    manifest["changed_surface_digest"] = digest_value(
        {
            "repository": manifest["repository"],
            "base_source_digest": manifest["base"]["source_digest"],
            "current_source_digest": manifest["current"]["source_digest"],
            "entries": changes,
        }
    )
    return manifest


def _load_json(path: str) -> dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    if not isinstance(value, dict):
        raise ManifestError("baseline must be a JSON object")
    return value


def _write_json(value: dict[str, Any], output: str) -> None:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(body)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="Capture a baseline/current source snapshot")
    snapshot.add_argument("--root", default=".")
    snapshot.add_argument("--adapter", choices=("auto", "git", "filesystem"), default="auto")
    snapshot.add_argument("--ignore", action="append", default=[])
    snapshot.add_argument("--output", default="-")

    compare = subparsers.add_parser("compare", help="Compare a stored baseline with current source")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--root", default=".")
    compare.add_argument("--ignore", action="append", default=[])
    compare.add_argument("--output", default="-")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "snapshot":
            result = capture_snapshot(arguments.root, arguments.adapter, arguments.ignore)
        else:
            result = compare_with_baseline(_load_json(arguments.baseline), arguments.root, arguments.ignore)
        _write_json(result, arguments.output)
        return 0
    except (ManifestError, OSError, json.JSONDecodeError) as error:
        sys.stderr.write(json.dumps({"error": "MANIFEST_FAILED", "message": str(error)}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
