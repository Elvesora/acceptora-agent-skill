#!/usr/bin/env python3
"""Install or update the Acceptora skill inside one Git worktree."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "https://github.com/Elvesora/acceptora-agent-skill"
CANONICAL_BRANCH = "main"
ACCEPTORA_ORIGIN = "https://www.acceptora.com"
PROJECT_CONFIG = ".acceptora/config.json"
CLIENTS = {
    "codex": {
        "skill_directory": ".agents/skills/acceptora",
        "instruction_file": "AGENTS.md",
    },
    "claude-code": {
        "skill_directory": ".claude/skills/acceptora",
        "instruction_file": "CLAUDE.md",
    },
    "gemini-cli": {
        "skill_directory": ".gemini/skills/acceptora",
        "instruction_file": "GEMINI.md",
    },
}
SUPPORTED_CLIENTS = tuple(CLIENTS)
SKILL_PAYLOAD = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/api-mcp.md",
    "scripts/project_context.py",
)
INSTRUCTION_START = "<!-- acceptora:start -->"
INSTRUCTION_END = "<!-- acceptora:end -->"
PROJECT_INSTRUCTION = (
    f"{INSTRUCTION_START} Before analyzing or changing this project, open and follow the project-local "
    f"Acceptora skill; reread it before describing manual verification. {INSTRUCTION_END}"
)
MCP_START = "# acceptora-mcp:start"
MCP_END = "# acceptora-mcp:end"
PROJECT_ID_PATTERN = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
TOKEN_ENV_PATTERN = re.compile(r"^ACCEPTORA_AGENT_TOKEN_PROJ_[0-9A-HJKMNP-TV-Z]{26}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ENV_TEMPLATE_PARTS = {"default", "defaults", "dist", "example", "sample", "template"}


class InstallError(RuntimeError):
    """Raised when a project-local installation cannot proceed safely."""


def _json_output(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _run_git(root: Path, *arguments: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise InstallError("Git inspection failed.") from error
    if process.returncode not in allowed:
        message = process.stderr.strip() or "Git inspection failed."
        raise InstallError(message)
    return process


def _git_worktree_root(path: Path) -> Path:
    if not path.exists() or not path.is_dir():
        raise InstallError(f"Git worktree does not exist: {path}")
    resolved = path.resolve(strict=True)
    result = _run_git(resolved, "rev-parse", "--show-toplevel").stdout.strip()
    try:
        observed = Path(result).resolve(strict=True)
    except OSError as error:
        raise InstallError("Git returned an invalid worktree root.") from error
    if observed != resolved:
        raise InstallError("The target must be the exact Git worktree root.")
    return resolved


def _normalized_remote(value: str) -> str:
    return value.strip().removesuffix(".git").rstrip("/")


def _is_linklike(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _relative_path(value: str, label: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise InstallError(f"{label} is not a safe relative path.")
    return relative


def _project_path(root: Path, relative_value: str) -> Path:
    relative = _relative_path(relative_value, "Project path")
    destination = root.joinpath(*relative.parts)
    cursor = root
    for index, part in enumerate(relative.parts):
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if _is_linklike(cursor):
                raise InstallError(f"Project path crosses a link or reparse point: {relative_value}")
            if index < len(relative.parts) - 1 and not cursor.is_dir():
                raise InstallError(f"Project path has a non-directory parent: {relative_value}")
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise InstallError(f"Project path escapes the Git worktree: {relative_value}") from error
    return destination


def _source_file(relative_value: str) -> Path:
    relative = _relative_path(relative_value, "Package source path")
    path = PACKAGE_ROOT.joinpath(*relative.parts)
    if not path.exists() or _is_linklike(path):
        raise InstallError(f"Package source is missing or unsafe: {relative_value}")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"Package source is not a regular file: {relative_value}")
    return path


def _source_identity() -> str:
    source_root = _git_worktree_root(PACKAGE_ROOT)
    remote = _normalized_remote(_run_git(source_root, "remote", "get-url", "origin").stdout)
    if remote != CANONICAL_REPOSITORY:
        raise InstallError("Installer source must use the canonical Acceptora GitHub origin.")
    branch = _run_git(source_root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch != CANONICAL_BRANCH:
        raise InstallError("Installer source must be the main branch.")
    commit = _run_git(source_root, "rev-parse", "HEAD").stdout.strip()
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise InstallError("Installer source has an invalid commit identity.")

    sources = {
        "scripts/install.py",
        *SKILL_PAYLOAD,
    }
    for relative in sorted(sources):
        _source_file(relative)
        _run_git(source_root, "ls-files", "--error-unmatch", "--", relative)
        if _run_git(source_root, "diff", "--quiet", "--", relative, allowed=(0, 1)).returncode != 0:
            raise InstallError(f"Installer source has an uncommitted payload change: {relative}")
        if _run_git(source_root, "diff", "--cached", "--quiet", "--", relative, allowed=(0, 1)).returncode != 0:
            raise InstallError(f"Installer source has a staged payload change: {relative}")
    return commit


def _token_env(project_id: str) -> str:
    if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise InstallError("Project ID must be proj_ followed by one uppercase ULID.")
    return f"ACCEPTORA_AGENT_TOKEN_{project_id.upper()}"


def _select_token_environment(root: Path, explicit: str | None) -> str:
    if explicit is not None:
        if TOKEN_ENV_PATTERN.fullmatch(explicit) is None:
            raise InstallError("--token-env must name one project-scoped Acceptora variable.")
        _require_token_environment(root, explicit)
        return explicit
    candidates = sorted(name for name in os.environ if TOKEN_ENV_PATTERN.fullmatch(name) is not None)
    if not candidates:
        _require_token_environment(root, None)
        raise AssertionError("missing credential preflight must stop")
    if len(candidates) > 1:
        raise InstallError(
            "Multiple project-scoped Acceptora variables are available; select the intended project with --token-env."
        )
    return candidates[0]


def _validate_selected_key(token_env: str) -> str:
    helper_path = _source_file("scripts/project_context.py")
    spec = importlib.util.spec_from_file_location("acceptora_installer_project_context", helper_path)
    if spec is None or spec.loader is None:
        raise InstallError("Acceptora project validation helper is unavailable.")
    helper = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(helper)
        token = os.environ[token_env]
        helper._validate_token(token)
        payload = helper._request_project(token)
        project_id, derived_token_env = helper._project_identity(payload)
        if derived_token_env != token_env:
            raise InstallError("The selected project key does not match --token-env.")
        helper._validate_project(payload, project_id)
    except InstallError:
        raise
    except Exception:
        raise InstallError("Acceptora rejected or could not validate the selected project key.") from None
    return project_id


def _is_environment_filename(name: str) -> bool:
    normalized = name.casefold()
    parts = {part for part in re.split(r"[._-]+", normalized) if part}
    if parts & ENV_TEMPLATE_PARTS:
        return False
    return (
        normalized in {".env", ".envrc", ".dev.vars"}
        or normalized.startswith(".env.")
        or normalized.startswith(".dev.vars.")
        or normalized.endswith(".env")
    )


def _require_token_environment(root: Path, token_env: str | None) -> None:
    if token_env is not None and token_env in os.environ:
        return

    variable = token_env or "an ACCEPTORA_AGENT_TOKEN_PROJ_<ULID> variable"

    candidates: list[Path] = []
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name.casefold())
    except OSError as error:
        raise InstallError("Project environment storage could not be inspected.") from error
    for path in entries:
        if not _is_environment_filename(path.name):
            continue
        if _is_linklike(path) or not path.is_file():
            raise InstallError(f"Project environment store is not a regular file: {path.name}")
        candidates.append(path)

    safe: list[str] = []
    unsafe: list[str] = []
    for path in candidates:
        ignored = _run_git(root, "check-ignore", "--quiet", "--", path.name, allowed=(0, 1)).returncode == 0
        tracked = _run_git(root, "ls-files", "--error-unmatch", "--", path.name, allowed=(0, 1)).returncode == 0
        (safe if ignored and not tracked else unsafe).append(path.name)
    if unsafe:
        raise InstallError(
            "Refusing credential storage in environment files that are not both Git-ignored and untracked: "
            + ", ".join(unsafe)
        )
    if len(safe) > 1:
        raise InstallError(
            f"{variable} is missing. Ask the user which ignored project environment store is active: "
            + ", ".join(safe)
        )
    if safe:
        raise InstallError(
            f"{variable} is missing. Stop installation, validate the project key with Acceptora, then ask the "
            f"user to place its derived project variable in {safe[0]} using the project's existing environment-file format. "
            "Restart the client through the project's environment loader before retrying."
        )
    raise InstallError(
        f"{variable} is missing. Ask the user for the project key, validate it with Acceptora, and configure "
        "the derived variable in the project's established secret-loading mechanism before retrying."
    )


def _managed_bounds(text: str, start_marker: str, end_marker: str, label: str) -> tuple[int, int] | None:
    starts = text.count(start_marker)
    ends = text.count(end_marker)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise InstallError(f"{label} has malformed managed markers.")
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    return start, end


def _upsert_block(text: str, block: str, start_marker: str, end_marker: str, label: str) -> str:
    normalized = block.strip() + "\n"
    bounds = _managed_bounds(text, start_marker, end_marker, label)
    if bounds is None:
        prefix = text.rstrip()
        return (prefix + "\n\n" if prefix else "") + normalized
    start, end = bounds
    return text[:start] + normalized.rstrip("\n") + text[end:]


def _remove_block(text: str, start_marker: str, end_marker: str, label: str) -> str:
    bounds = _managed_bounds(text, start_marker, end_marker, label)
    if bounds is None:
        raise InstallError(f"{label} has no managed Acceptora block.")
    start, end = bounds
    prefix = text[:start].rstrip()
    suffix = text[end:].lstrip("\r\n")
    if prefix and suffix:
        return prefix + "\n\n" + suffix
    if prefix:
        return prefix + "\n"
    return suffix


def _read_utf8(path: Path, label: str) -> str:
    if not path.exists():
        return ""
    if _is_linklike(path) or not path.is_file():
        raise InstallError(f"{label} is not a regular project file.")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"{label} is not valid UTF-8.") from error


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _config_document(project_id: str, token_env: str, commit: str) -> dict[str, str]:
    return {
        "project_id": project_id,
        "token_env": token_env,
        "origin": ACCEPTORA_ORIGIN,
        "installed_commit": commit,
    }


def _load_config(root: Path) -> dict[str, str]:
    path = _project_path(root, PROJECT_CONFIG)
    if not path.exists():
        raise InstallError("Acceptora is not installed in this project.")
    try:
        document = json.loads(_read_utf8(path, "Acceptora project config"))
    except json.JSONDecodeError as error:
        raise InstallError("Acceptora project config is invalid JSON.") from error
    required = {"project_id", "token_env", "origin", "installed_commit"}
    if not isinstance(document, dict) or set(document) != required:
        raise InstallError("Acceptora project config must contain exactly four supported fields.")
    if not all(isinstance(document[key], str) for key in required):
        raise InstallError("Acceptora project config contains an invalid value.")
    project_id = document["project_id"]
    if document["token_env"] != _token_env(project_id):
        raise InstallError("Acceptora project config has a mismatched credential variable.")
    if document["origin"] != ACCEPTORA_ORIGIN or COMMIT_PATTERN.fullmatch(document["installed_commit"]) is None:
        raise InstallError("Acceptora project config has an invalid origin or commit.")
    return document


def _mcp_relative(client: str) -> str:
    return {
        "codex": ".codex/config.toml",
        "claude-code": ".mcp.json",
        "gemini-cli": ".gemini/settings.json",
    }[client]


def _codex_mcp_block(token_env: str) -> str:
    return (
        f"{MCP_START}\n"
        "[mcp_servers.acceptora]\n"
        f'url = "{ACCEPTORA_ORIGIN}/mcp"\n'
        f'bearer_token_env_var = "{token_env}"\n'
        f"{MCP_END}\n"
    )


def _json_mcp_server(client: str, token_env: str) -> dict[str, Any]:
    authorization = {"Authorization": f"Bearer ${{{token_env}}}"}
    if client == "claude-code":
        return {"type": "http", "url": f"{ACCEPTORA_ORIGIN}/mcp", "headers": authorization}
    return {
        "type": "http",
        "url": f"{ACCEPTORA_ORIGIN}/mcp",
        "headers": authorization,
    }


def _parse_codex_mcp_without_managed(current: str, token_env: str, *, owned: bool) -> str:
    bounds = _managed_bounds(current, MCP_START, MCP_END, "Codex MCP config")
    if bounds is None:
        if owned:
            raise InstallError("Codex MCP config is missing its installer-owned Acceptora block.")
        without_managed = current
    else:
        if not owned:
            raise InstallError("Codex MCP config already has an unmanaged Acceptora block.")
        start, end = bounds
        if current[start:end] != _codex_mcp_block(token_env).rstrip("\n"):
            raise InstallError("Codex MCP config installer-owned Acceptora block has drifted.")
        without_managed = current[:start] + current[end:]

    try:
        document = tomllib.loads(without_managed)
    except tomllib.TOMLDecodeError as error:
        raise InstallError("Project MCP config is invalid TOML.") from error
    servers = document.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        raise InstallError("Codex MCP config mcp_servers must be a table.")
    if isinstance(servers, dict) and "acceptora" in servers:
        raise InstallError("Codex MCP config already defines an unmanaged acceptora server.")
    return without_managed


def _parse_json_mcp_document(current: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if current:
        try:
            document = json.loads(current)
        except json.JSONDecodeError as error:
            raise InstallError("Project MCP config is invalid JSON.") from error
    else:
        document = {}
    if not isinstance(document, dict):
        raise InstallError("Project MCP config must be a JSON object.")
    servers = document.get("mcpServers")
    if servers is None:
        servers = {}
        document["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise InstallError("Project MCP config mcpServers must be an object.")
    return document, servers


def _gemini_allowed_variables(document: dict[str, Any], *, create: bool) -> list[str]:
    security = document.get("security")
    if security is None:
        if not create:
            raise InstallError("Gemini MCP config is missing the installer-owned environment allowlist.")
        security = {}
        document["security"] = security
    if not isinstance(security, dict):
        raise InstallError("Gemini MCP config security must be an object.")

    redaction = security.get("environmentVariableRedaction")
    if redaction is None:
        if not create:
            raise InstallError("Gemini MCP config is missing the installer-owned environment allowlist.")
        redaction = {}
        security["environmentVariableRedaction"] = redaction
    if not isinstance(redaction, dict):
        raise InstallError("Gemini MCP config environmentVariableRedaction must be an object.")

    allowed = redaction.get("allowed")
    if allowed is None:
        if not create:
            raise InstallError("Gemini MCP config is missing the installer-owned environment allowlist.")
        allowed = []
        redaction["allowed"] = allowed
    if not isinstance(allowed, list) or not all(isinstance(value, str) for value in allowed):
        raise InstallError("Gemini MCP config environmentVariableRedaction.allowed must be a string array.")
    return allowed


def _prepare_mcp(root: Path, client: str, token_env: str, *, owned: bool) -> tuple[Path, str]:
    path = _project_path(root, _mcp_relative(client))
    current = _read_utf8(path, "Project MCP config")
    if client == "codex":
        _parse_codex_mcp_without_managed(current, token_env, owned=owned)
        result = _upsert_block(current, _codex_mcp_block(token_env), MCP_START, MCP_END, "Codex MCP config")
        try:
            tomllib.loads(result)
        except tomllib.TOMLDecodeError as error:
            raise InstallError("Project MCP config cannot be merged safely.") from error
        return path, result

    document, servers = _parse_json_mcp_document(current)
    expected_server = _json_mcp_server(client, token_env)
    if owned:
        if servers.get("acceptora") != expected_server:
            raise InstallError("Project MCP config installer-owned acceptora server has drifted.")
    elif "acceptora" in servers:
        raise InstallError("Project MCP config already defines an unmanaged acceptora server.")
    else:
        servers["acceptora"] = expected_server

    if client == "gemini-cli":
        allowed = _gemini_allowed_variables(document, create=not owned)
        occurrences = allowed.count(token_env)
        if owned:
            if occurrences != 1:
                raise InstallError("Gemini MCP config installer-owned environment allowlist has drifted.")
        else:
            if occurrences != 0:
                raise InstallError("Gemini MCP config already allowlists the Acceptora project variable.")
            allowed.append(token_env)
    return path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _remove_mcp(root: Path, client: str, token_env: str) -> tuple[Path, str]:
    path = _project_path(root, _mcp_relative(client))
    current = _read_utf8(path, "Project MCP config")
    if client == "codex":
        _parse_codex_mcp_without_managed(current, token_env, owned=True)
        return path, _remove_block(current, MCP_START, MCP_END, "Codex MCP config")

    document, servers = _parse_json_mcp_document(current)
    if servers.get("acceptora") != _json_mcp_server(client, token_env):
        raise InstallError("Project MCP config does not contain the installer-owned acceptora server.")
    del servers["acceptora"]
    if client == "gemini-cli":
        allowed = _gemini_allowed_variables(document, create=False)
        if allowed.count(token_env) != 1:
            raise InstallError("Gemini MCP config installer-owned environment allowlist has drifted.")
        allowed.remove(token_env)
    return path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _skill_tree_has_exact_paths(destination: Path, payload: dict[str, bytes]) -> bool:
    if not destination.is_dir() or _is_linklike(destination):
        return False
    expected_files = set(payload)
    expected_directories = {
        parent.as_posix()
        for relative in expected_files
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    pending = [destination]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _is_linklike(path):
                        return False
                    relative = path.relative_to(destination).as_posix()
                    if entry.is_file(follow_symlinks=False):
                        observed_files.add(relative)
                    elif entry.is_dir(follow_symlinks=False):
                        observed_directories.add(relative)
                        pending.append(path)
                    else:
                        return False
    except OSError:
        return False
    return observed_files == expected_files and observed_directories == expected_directories


def _require_exact_skill_tree(destination: Path, payload: dict[str, bytes]) -> None:
    if not _skill_tree_has_exact_paths(destination, payload):
        raise InstallError("Project skill tree has missing or extra entries; refusing the requested lifecycle change.")


def _replace_skill(root: Path, skill_relative: str, payload: dict[str, bytes], *, exists: bool) -> Path:
    destination = _project_path(root, skill_relative)
    if destination.exists() != exists:
        expected = "exist" if exists else "not exist"
        raise InstallError(f"Project skill directory must {expected} for this operation.")
    if destination.exists() and (_is_linklike(destination) or not destination.is_dir()):
        raise InstallError("Project skill destination is not a regular directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".acceptora-skill-", dir=destination.parent))
    try:
        for relative, body in payload.items():
            target = staging.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def _payload() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for relative in SKILL_PAYLOAD:
        try:
            result[relative] = _source_file(relative).read_bytes()
        except OSError as error:
            raise InstallError(f"Could not read package payload: {relative}") from error
    return result


def _install(args: argparse.Namespace, *, update: bool) -> dict[str, Any]:
    profile = CLIENTS[args.client]
    commit = _source_identity()
    root = _git_worktree_root(Path(args.target_root))
    config_path = _project_path(root, PROJECT_CONFIG)
    config = _load_config(root) if update else None
    if update:
        assert config is not None
        project_id = config["project_id"]
        token_env = config["token_env"]
        _require_token_environment(root, token_env)
        if _validate_selected_key(token_env) != project_id:
            raise InstallError("The selected project key does not match the installed Acceptora project.")
    else:
        if config_path.exists():
            raise InstallError("Acceptora is already installed; use update.")
        token_env = _select_token_environment(root, args.token_env)
        project_id = _validate_selected_key(token_env)

    payload = _payload()
    skill_path = _project_path(root, profile["skill_directory"])
    if update and not skill_path.exists():
        raise InstallError("The selected client's project skill is not installed.")
    if not update and skill_path.exists():
        raise InstallError("The selected client's project skill directory already exists.")
    if update:
        _require_exact_skill_tree(skill_path, payload)

    instruction_path = _project_path(root, profile["instruction_file"])
    instruction = _read_utf8(instruction_path, "Project instruction file")
    instruction_result = _upsert_block(
        instruction,
        PROJECT_INSTRUCTION,
        INSTRUCTION_START,
        INSTRUCTION_END,
        "Project instruction file",
    )
    mcp_path, mcp_result = _prepare_mcp(root, args.client, token_env, owned=update)

    _replace_skill(root, profile["skill_directory"], payload, exists=update)
    _atomic_write(instruction_path, instruction_result)
    _atomic_write(mcp_path, mcp_result)
    _atomic_write(
        config_path,
        json.dumps(_config_document(project_id, token_env, commit), ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "status": "updated" if update else "installed",
        "client": args.client,
        "project_id": project_id,
        "token_env": token_env,
        "installed_commit": commit,
        "skill_directory": profile["skill_directory"],
        "instruction_file": profile["instruction_file"],
        "mcp_config": _mcp_relative(args.client),
    }


def _status(args: argparse.Namespace) -> dict[str, Any]:
    profile = CLIENTS[args.client]
    source_commit = _source_identity()
    root = _git_worktree_root(Path(args.target_root))
    config_path = _project_path(root, PROJECT_CONFIG)
    if not config_path.exists():
        return {"status": "not_installed", "client": args.client}
    config = _load_config(root)
    skill_root = _project_path(root, profile["skill_directory"])
    payload = _payload()
    payload_matches = _skill_tree_has_exact_paths(skill_root, payload)
    if payload_matches:
        for relative, body in payload.items():
            path = skill_root.joinpath(*PurePosixPath(relative).parts)
            if not path.is_file() or _is_linklike(path) or path.read_bytes() != body:
                payload_matches = False
                break
    instruction = _read_utf8(_project_path(root, profile["instruction_file"]), "Project instruction file")
    instruction_matches = PROJECT_INSTRUCTION in instruction
    try:
        mcp_path, mcp_expected = _prepare_mcp(root, args.client, config["token_env"], owned=True)
        mcp_matches = _read_utf8(mcp_path, "Project MCP config") == mcp_expected
    except InstallError:
        mcp_matches = False
    drift = not (payload_matches and instruction_matches and mcp_matches)
    return {
        "status": "drift" if drift else ("current" if config["installed_commit"] == source_commit else "update_available"),
        "client": args.client,
        "project_id": config["project_id"],
        "token_env": config["token_env"],
        "token_env_present": config["token_env"] in os.environ,
        "installed_commit": config["installed_commit"],
        "source_commit": source_commit,
        "payload_matches": payload_matches,
        "instruction_matches": instruction_matches,
        "mcp_matches": mcp_matches,
    }


def _remove_empty_parents(path: Path, root: Path) -> None:
    cursor = path
    while cursor != root:
        try:
            cursor.rmdir()
        except OSError:
            return
        cursor = cursor.parent


def _uninstall(args: argparse.Namespace) -> dict[str, Any]:
    profile = CLIENTS[args.client]
    _source_identity()
    root = _git_worktree_root(Path(args.target_root))
    config = _load_config(root)
    skill_path = _project_path(root, profile["skill_directory"])
    if not skill_path.is_dir() or _is_linklike(skill_path):
        raise InstallError("The selected client's project skill is not installed safely.")
    _require_exact_skill_tree(skill_path, _payload())
    instruction_path = _project_path(root, profile["instruction_file"])
    instruction = _remove_block(
        _read_utf8(instruction_path, "Project instruction file"),
        INSTRUCTION_START,
        INSTRUCTION_END,
        "Project instruction file",
    )
    mcp_path, mcp = _remove_mcp(root, args.client, config["token_env"])

    shutil.rmtree(skill_path)
    _remove_empty_parents(skill_path.parent, root)
    _atomic_write(instruction_path, instruction)
    _atomic_write(mcp_path, mcp)
    config_path = _project_path(root, PROJECT_CONFIG)
    config_path.unlink()
    _remove_empty_parents(config_path.parent, root)
    return {"status": "uninstalled", "client": args.client, "project_id": config["project_id"]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "update", "status", "uninstall"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--client", choices=SUPPORTED_CLIENTS, required=True)
        command_parser.add_argument("--target-root", required=True)
        if command == "install":
            command_parser.add_argument("--token-env")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "install":
            result = _install(args, update=False)
        elif args.command == "update":
            result = _install(args, update=True)
        elif args.command == "status":
            result = _status(args)
        else:
            result = _uninstall(args)
        _json_output(result)
        return 0
    except (InstallError, OSError, UnicodeError, json.JSONDecodeError) as error:
        sys.stderr.write(f"Acceptora installer failed: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
