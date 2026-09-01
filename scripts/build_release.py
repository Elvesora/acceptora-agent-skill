#!/usr/bin/env python3
"""Build the deterministic Acceptora skill ZIP used by GitHub releases."""

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
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PREFIX = "acceptora"
CANONICAL_REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
PRODUCTION_BRANCH = "main"
EMBEDDED_PROVENANCE_FILENAME = "acceptora-agent-skill-provenance.json"
PACKAGE_MANIFEST = "config/package-manifest.json"
PAYLOAD_PATHS = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/api-mcp.md",
    "scripts/mcp-headers.mjs",
    "scripts/project_context.py",
    "LICENSE",
)
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ReleaseError(RuntimeError):
    """Raised when a release candidate cannot satisfy the public contract."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _is_semantic_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = SEMVER_PATTERN.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group(4)
    return prerelease is None or not any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in prerelease.split(".")
    )


def _source_root(value: str) -> Path:
    requested = Path(value).expanduser().absolute()
    if requested.is_symlink():
        raise ReleaseError("The release source must not be a symlink.")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise ReleaseError("The release source does not exist.") from error
    if not resolved.is_dir():
        raise ReleaseError("The release source must be a directory.")
    return resolved


def _dist_directory(value: str, source_root: Path) -> Path:
    requested = Path(value).expanduser().absolute()
    if requested.exists() or requested.is_symlink():
        raise ReleaseError("The release destination must not already exist.")
    resolved = requested.resolve(strict=False)
    if resolved == source_root:
        raise ReleaseError("The release destination must not be the package root.")
    return resolved


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "ACCEPTORA_AGENT_TOKEN" and not key.startswith("ACCEPTORA_AGENT_TOKEN_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _run_git(git: Path, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(git), "-c", "credential.helper=", *arguments],
        cwd=cwd,
        capture_output=True,
        check=False,
        env=_git_environment(),
    )


def _git_text(git: Path, cwd: Path, *arguments: str) -> str:
    completed = _run_git(git, cwd, *arguments)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseError(detail or f"Git command failed: {' '.join(arguments)}")
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ReleaseError("Git returned invalid UTF-8 output.") from error


def _normalized_repository_url(value: str) -> str:
    normalized = value.rstrip("/")
    return normalized[:-4] if normalized.endswith(".git") else normalized


def _git_context(
    source_root: Path,
    commit_override: str | None,
    allow_dirty: bool,
) -> dict[str, Any]:
    if commit_override not in {None, "UNVERSIONED"} and re.fullmatch(
        r"[0-9a-fA-F]{7,64}", commit_override
    ) is None:
        raise ReleaseError("The source commit must be a hexadecimal revision or UNVERSIONED.")

    executable = shutil.which("git")
    if executable is None:
        if allow_dirty and commit_override in {None, "UNVERSIONED"}:
            return {"source_state": "unversioned", "source_commit": "UNVERSIONED", "git": None}
        raise ReleaseError("Git is required for a publishable release.")

    git = Path(executable).resolve(strict=True)
    try:
        git.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ReleaseError("The Git executable must be outside the release source.")

    root_result = _run_git(git, source_root, "rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        if allow_dirty and commit_override in {None, "UNVERSIONED"}:
            return {"source_state": "unversioned", "source_commit": "UNVERSIONED", "git": None}
        raise ReleaseError("A publishable release must come from a Git worktree.")

    try:
        repository_root = Path(root_result.stdout.decode("utf-8", errors="strict").strip()).resolve(strict=True)
        source_prefix = source_root.relative_to(repository_root).as_posix()
    except (UnicodeDecodeError, ValueError, OSError) as error:
        raise ReleaseError("The release source is outside its Git worktree.") from error

    head = _git_text(git, repository_root, "rev-parse", "HEAD").lower()
    if re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
        raise ReleaseError("The Git HEAD revision is invalid.")
    status_path = source_prefix or "."
    dirty = bool(
        _git_text(
            git,
            repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            status_path,
        )
    )

    publishable = not allow_dirty or commit_override not in {None, "UNVERSIONED"}
    if dirty:
        if publishable:
            raise ReleaseError("The release source has uncommitted or untracked files; use a clean checkout.")
        return {"source_state": "dirty_allowed", "source_commit": "UNVERSIONED", "git": None}
    if not publishable:
        return {"source_state": "unversioned", "source_commit": "UNVERSIONED", "git": None}

    if commit_override not in {None, "UNVERSIONED"} and not head.startswith(commit_override.lower()):
        raise ReleaseError("The requested source commit does not match Git HEAD.")
    branch = _git_text(git, repository_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != PRODUCTION_BRANCH:
        raise ReleaseError("A publishable release must be built from the production main branch.")
    origin = _normalized_repository_url(_git_text(git, repository_root, "remote", "get-url", "origin"))
    if origin != CANONICAL_REPOSITORY_URL:
        raise ReleaseError("A publishable release must use the canonical HTTPS origin.")

    return {
        "source_state": "clean",
        "source_commit": head,
        "source_repository_url": CANONICAL_REPOSITORY_URL,
        "source_branch": PRODUCTION_BRANCH,
        "git": git,
        "repository_root": repository_root,
        "source_prefix": source_prefix,
    }


def _read_source_file(source_root: Path, relative: str, context: dict[str, Any]) -> bytes:
    git = context.get("git")
    if git is not None:
        repository_path = "/".join(part for part in (context["source_prefix"], relative) if part)
        completed = _run_git(
            git,
            context["repository_root"],
            "cat-file",
            "blob",
            f"{context['source_commit']}:{repository_path}",
        )
        if completed.returncode != 0:
            raise ReleaseError(f"The release source is missing tracked file: {relative}")
        return completed.stdout

    path = source_root / Path(relative)
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"The release source is missing regular file: {relative}")
    return path.read_bytes()


def _package_metadata(source_root: Path, context: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest = json.loads(_read_source_file(source_root, PACKAGE_MANIFEST, context).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("The package manifest is not valid UTF-8 JSON.") from error
    if not isinstance(manifest, dict):
        raise ReleaseError("The package manifest must contain an object.")
    distribution = manifest.get("distribution")
    skill = manifest.get("skill")
    if distribution != {"repository_url": CANONICAL_REPOSITORY_URL, "branch": PRODUCTION_BRANCH}:
        raise ReleaseError("The package manifest has an unexpected distribution identity.")
    if not isinstance(skill, dict) or skill.get("name") != ARCHIVE_PREFIX:
        raise ReleaseError("The package manifest has an unexpected skill identity.")
    if not _is_semantic_version(skill.get("version")):
        raise ReleaseError("The package manifest has an invalid semantic version.")
    return manifest


def _file_entry(relative: str, body: bytes) -> dict[str, Any]:
    mode = "0755" if relative.endswith(".py") else "0644"
    return {
        "path": relative,
        "archive_path": f"{ARCHIVE_PREFIX}/{relative}",
        "size": len(body),
        "mode": mode,
        "sha256": _sha256_bytes(body),
        "body": body,
    }


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("path", "archive_path", "size", "mode", "sha256")}


def _write_zip(path: Path, entries: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for entry in sorted(entries, key=lambda item: item["archive_path"]):
            info = zipfile.ZipInfo(entry["archive_path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | int(entry["mode"], 8)) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, entry["body"], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build_release(
    source_root_value: str,
    dist_directory_value: str,
    commit_override: str | None = None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    source_root = _source_root(source_root_value)
    dist_directory = _dist_directory(dist_directory_value, source_root)
    context = _git_context(source_root, commit_override, allow_dirty)
    package = _package_metadata(source_root, context)
    entries = [
        _file_entry(relative, _read_source_file(source_root, relative, context))
        for relative in PAYLOAD_PATHS
    ]
    public_entries = [_public_entry(entry) for entry in sorted(entries, key=lambda item: item["path"])]
    source_tree_sha256 = _sha256_bytes(_canonical_json_bytes(public_entries))
    provenance = {
        "schema_version": 1,
        "repository_url": CANONICAL_REPOSITORY_URL,
        "branch": PRODUCTION_BRANCH,
        "commit_sha": context["source_commit"],
        "source_state": context["source_state"],
        "source_tree_sha256": source_tree_sha256,
    }
    provenance_body = _json_bytes(provenance)
    provenance_entry = {
        "path": EMBEDDED_PROVENANCE_FILENAME,
        "archive_path": EMBEDDED_PROVENANCE_FILENAME,
        "size": len(provenance_body),
        "mode": "0644",
        "sha256": _sha256_bytes(provenance_body),
        "body": provenance_body,
    }

    version = package["skill"]["version"]
    zip_name = f"{ARCHIVE_PREFIX}-{version}.zip"
    dist_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dist_directory.name}.", suffix=".tmp", dir=dist_directory.parent))
    try:
        zip_path = staging / zip_name
        _write_zip(zip_path, [*entries, provenance_entry])
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "name": package["skill"]["name"],
            "version": version,
            "distribution": package["distribution"],
            "source_state": context["source_state"],
            "source_commit": context["source_commit"],
            "source_tree_sha256": source_tree_sha256,
            "archive_prefix": ARCHIVE_PREFIX,
            "files": public_entries,
            "embedded_provenance": {
                "filename": EMBEDDED_PROVENANCE_FILENAME,
                "sha256": provenance_entry["sha256"],
            },
            "artifacts": [
                {
                    "filename": zip_name,
                    "format": "zip",
                    "size": zip_path.stat().st_size,
                    "sha256": _sha256_file(zip_path),
                }
            ],
        }
        for key in ("source_repository_url", "source_branch"):
            if key in context:
                manifest[key] = context[key]
        manifest_body = _json_bytes(manifest)
        (staging / "release-manifest.json").write_bytes(manifest_body)
        checksum_rows = sorted(
            (
                (_sha256_file(zip_path).removeprefix("sha256:"), zip_name),
                (_sha256_bytes(manifest_body).removeprefix("sha256:"), "release-manifest.json"),
            ),
            key=lambda item: item[1],
        )
        (staging / "SHA256SUMS").write_text(
            "".join(f"{digest}  {filename}\n" for digest, filename in checksum_rows),
            encoding="ascii",
            newline="\n",
        )
        if dist_directory.exists() or dist_directory.is_symlink():
            raise ReleaseError("The release destination appeared while the build was running.")
        os.replace(staging, dist_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "schema_version": 1,
        "status": "built",
        "dist_directory": dist_directory.as_posix(),
        "version": version,
        "source_state": context["source_state"],
        "source_commit": context["source_commit"],
        "source_tree_sha256": source_tree_sha256,
        "artifacts": manifest["artifacts"],
        "manifest": {
            "filename": "release-manifest.json",
            "sha256": _sha256_bytes(manifest_body),
        },
        "checksums": "SHA256SUMS",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--source-root", default=str(PACKAGE_ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--format", choices=("json", "text"), default="json", help=argparse.SUPPRESS)
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
    except (OSError, ReleaseError) as error:
        sys.stderr.write(f"Release build failed: {error}\n")
        return 2

    if arguments.format == "text":
        sys.stdout.write(f"Built acceptora {result['version']} in {result['dist_directory']}\n")
    else:
        sys.stdout.write(_json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
