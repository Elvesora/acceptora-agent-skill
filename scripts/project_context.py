#!/usr/bin/env python3
"""Validate one Acceptora project key or load fresh project instructions."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


ACCEPTORA_ORIGIN = "https://www.acceptora.com"
PROJECT_URL = f"{ACCEPTORA_ORIGIN}/api/v1/integrations/project"
REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
PRODUCTION_BRANCH = "main"
CONFIG_RELATIVE_PATH = Path(".acceptora/config.json")
TOKEN_ENV_PREFIX = "ACCEPTORA_AGENT_TOKEN_PROJ_"
TOKEN_PATTERN = re.compile(r"^avt_(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})_[A-Za-z0-9]{48}$")
PROJECT_ID_PATTERN = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_SCOPES = {
    "projects:read",
    "features:resolve",
    "features:read",
    "checklists:write",
    "feedback:read",
    "feedback:address",
    "gates:read",
}
INSTRUCTION_FIELDS = (
    "analysis_guidance",
    "manual_verification_guidance",
    "test_data_guidance",
)
INSTRUCTION_SOURCES = {"default", "account", "project"}
MAX_RESPONSE_BYTES = 1_048_576
MAX_CONFIG_BYTES = 4_096
MAX_INSTRUCTION_CHARACTERS = 12_000
MAX_REVISION = 9_223_372_036_854_775_807
MAX_GIT_OUTPUT_BYTES = 1_024


class ProjectContextError(RuntimeError):
    """Raised for a safe, secret-free project context failure."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProjectContextError("Invalid project context arguments.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _credential_identity(token: str) -> tuple[str, str]:
    match = TOKEN_PATTERN.fullmatch(token)
    if match is None:
        raise ProjectContextError("The supplied value is not a valid Acceptora project key.")
    ulid = match.group("ulid")
    return f"proj_{ulid}", f"{TOKEN_ENV_PREFIX}{ulid}"


def _environment_variable(project_id: str) -> str:
    if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ProjectContextError("project_id must use the public proj_<ULID> form.")
    return f"ACCEPTORA_AGENT_TOKEN_{project_id.upper()}"


def _read_hidden_token() -> str:
    if sys.stdin.isatty():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass("Acceptora project key: ")
        except getpass.GetPassWarning:
            raise ProjectContextError("A hidden credential prompt is not available.") from None
    value = sys.stdin.readline(256)
    if not value or len(value) >= 256 or sys.stdin.read(1):
        raise ProjectContextError("Read exactly one Acceptora project key from standard input.")
    return value.removesuffix("\n").removesuffix("\r")


def _project_opener() -> Any:
    return build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))


def _request_project(token: str, *, opener: Any | None = None) -> dict[str, Any]:
    request = Request(
        PROJECT_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Acceptora-Agent-Skill",
        },
        method="GET",
    )
    try:
        response = (opener or _project_opener()).open(request, timeout=15)
        try:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
    except HTTPError as error:
        status = error.code
        error.close()
        if status in {401, 403}:
            raise ProjectContextError("Acceptora rejected the supplied project key.") from None
        raise ProjectContextError("Acceptora project verification failed.") from None
    except (OSError, URLError):
        raise ProjectContextError("Acceptora project verification failed.") from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise ProjectContextError("Acceptora returned an oversized project response.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProjectContextError("Acceptora returned an invalid project response.") from None
    if not isinstance(payload, dict):
        raise ProjectContextError("Acceptora returned an invalid project response.")
    return payload


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_REVISION:
        raise ProjectContextError("Acceptora returned invalid verification instructions.")
    return value


def _normalized_instruction(value: str) -> str | None:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip(" \t\n\r\0\x0b")
    return normalized or None


def _validate_instructions(value: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "account_revision",
        "project_revision",
        "effective_digest",
        "configured",
        "instructions",
        "sources",
    }
    if not isinstance(value, dict) or not fields.issubset(value) or value.get("schema_version") != "1.0":
        raise ProjectContextError("Acceptora returned invalid verification instructions.")
    _revision(value.get("account_revision"))
    _revision(value.get("project_revision"))
    configured = value.get("configured")
    instructions = value.get("instructions")
    sources = value.get("sources")
    if (
        not isinstance(configured, bool)
        or not isinstance(instructions, dict)
        or not set(INSTRUCTION_FIELDS).issubset(instructions)
        or not isinstance(sources, dict)
        or not set(INSTRUCTION_FIELDS).issubset(sources)
    ):
        raise ProjectContextError("Acceptora returned invalid verification instructions.")

    for field in INSTRUCTION_FIELDS:
        instruction = instructions[field]
        source = sources[field]
        if instruction is not None and (
            not isinstance(instruction, str)
            or len(instruction) > MAX_INSTRUCTION_CHARACTERS
            or _normalized_instruction(instruction) != instruction
        ):
            raise ProjectContextError("Acceptora returned invalid verification instructions.")
        if not isinstance(source, str) or source not in INSTRUCTION_SOURCES:
            raise ProjectContextError("Acceptora returned invalid verification instructions.")
        if (instruction is None) != (source == "default"):
            raise ProjectContextError("Acceptora returned invalid verification instructions.")

    if configured is not any(instructions[field] is not None for field in INSTRUCTION_FIELDS):
        raise ProjectContextError("Acceptora returned invalid verification instructions.")
    observed_digest = value.get("effective_digest")
    if not isinstance(observed_digest, str) or DIGEST_PATTERN.fullmatch(observed_digest) is None:
        raise ProjectContextError("Acceptora returned invalid verification instructions.")
    return value


def _validate_project(payload: dict[str, Any], project_id: str) -> tuple[list[str], dict[str, Any]]:
    if payload.get("project_id") != project_id:
        raise ProjectContextError("The project key does not match the selected Acceptora project.")
    scopes = payload.get("granted_scopes")
    if (
        not isinstance(scopes, list)
        or len(scopes) > 64
        or any(not isinstance(scope, str) or not scope or len(scope) > 128 for scope in scopes)
        or len(scopes) != len(set(scopes))
    ):
        raise ProjectContextError("Acceptora returned invalid project scopes.")
    if not REQUIRED_SCOPES.issubset(scopes):
        raise ProjectContextError("The project key lacks required Acceptora workflow scopes.")
    instructions = _validate_instructions(payload.get("verification_instructions"))
    return scopes, instructions


def _load_config(project_root: Path) -> dict[str, str]:
    root = project_root.resolve()
    path = root / CONFIG_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise ProjectContextError("The project Acceptora config is unavailable.")
    try:
        with path.open("rb") as config_file:
            raw = config_file.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ProjectContextError("The project Acceptora config is unavailable.") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ProjectContextError("The project Acceptora config is invalid.")
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProjectContextError("The project Acceptora config is invalid.") from None
    expected_fields = {"project_id", "token_env", "origin", "installed_commit"}
    if not isinstance(config, dict) or set(config) != expected_fields:
        raise ProjectContextError("The project Acceptora config is invalid.")
    project_id = config.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ProjectContextError("The project Acceptora config is invalid.")
    if config.get("token_env") != _environment_variable(project_id):
        raise ProjectContextError("The project Acceptora config is invalid.")
    if config.get("origin") != ACCEPTORA_ORIGIN:
        raise ProjectContextError("The project Acceptora config is invalid.")
    installed_commit = config.get("installed_commit")
    if not isinstance(installed_commit, str) or COMMIT_PATTERN.fullmatch(installed_commit) is None:
        raise ProjectContextError("The project Acceptora config is invalid.")
    return config


def _require_windows() -> None:
    if os.name != "nt":
        raise ProjectContextError("Windows current-user credential storage is unavailable.")


def _write_windows_registry(registry: Any, name: str, token: str) -> None:
    access = registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE
    with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, "Environment", 0, access) as key:
        try:
            previous = registry.QueryValueEx(key, name)
        except FileNotFoundError:
            previous = None
        registry.SetValueEx(key, name, 0, registry.REG_SZ, token)
        try:
            persisted, value_type = registry.QueryValueEx(key, name)
            if value_type != registry.REG_SZ or persisted != token:
                raise OSError
        except Exception:
            try:
                if previous is None:
                    registry.DeleteValue(key, name)
                else:
                    registry.SetValueEx(key, name, 0, previous[1], previous[0])
            except Exception:
                pass
            raise ProjectContextError("Windows did not confirm the current-user environment update.") from None


def _broadcast_windows_environment_change() -> bool:
    try:
        send_message = ctypes.windll.user32.SendMessageTimeoutW
        send_message.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        send_message.restype = ctypes.c_size_t
        result = ctypes.c_size_t()
        sent = send_message(
            0xFFFF,
            0x001A,
            0,
            ctypes.c_wchar_p("Environment"),
            0x0002,
            5_000,
            ctypes.byref(result),
        )
        return bool(sent)
    except Exception:
        return False


def _store_current_user_environment(name: str, token: str) -> bool:
    _require_windows()
    import winreg

    _write_windows_registry(winreg, name, token)
    return _broadcast_windows_environment_change()


def _git_environment(work_directory: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        normalized = key.upper()
        if normalized == "ACCEPTORA_AGENT_TOKEN" or normalized.startswith(
            ("ACCEPTORA_AGENT_TOKEN_", "GIT_")
        ):
            continue
        if normalized in {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}:
            continue
        environment[key] = value
    environment.update(
        {
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_CEILING_DIRECTORIES": str(work_directory.resolve()),
        }
    )
    return environment


def _github_update_status(installed_commit: str) -> dict[str, object]:
    git = shutil.which("git")
    if git is None:
        raise ProjectContextError("Git is required to check Acceptora skill updates.")
    with tempfile.TemporaryDirectory(prefix="acceptora-update-") as temporary:
        work_directory = Path(temporary)
        command = [
            str(Path(git).resolve()),
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "http.followRedirects=false",
            "ls-remote",
            "--exit-code",
            "--heads",
            REPOSITORY_URL,
            f"refs/heads/{PRODUCTION_BRANCH}",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=work_directory,
                env=_git_environment(work_directory),
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProjectContextError("The Acceptora skill update check is unavailable.") from None
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_GIT_OUTPUT_BYTES
        or len(completed.stderr) > MAX_GIT_OUTPUT_BYTES
    ):
        raise ProjectContextError("The Acceptora skill update check is unavailable.")
    try:
        output = completed.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise ProjectContextError("The Acceptora skill update response is invalid.") from None
    match = re.fullmatch(r"([0-9a-f]{40})\trefs/heads/main\r?\n", output)
    if match is None:
        raise ProjectContextError("The Acceptora skill update response is invalid.")
    main_commit = match.group(1)
    return {
        "status": "current" if main_commit == installed_commit else "update_available",
        "repository": REPOSITORY_URL,
        "branch": PRODUCTION_BRANCH,
        "installed_commit": installed_commit,
        "main_commit": main_commit,
        "auto_apply": False,
    }


def _validate_command() -> dict[str, object]:
    token = _read_hidden_token()
    project_id, token_env = _credential_identity(token)
    scopes, _ = _validate_project(_request_project(token), project_id)
    return {
        "status": "validated",
        "project_id": project_id,
        "environment_variable": token_env,
        "granted_scopes": scopes,
        "persistence_performed": False,
    }


def _store_windows_command() -> dict[str, object]:
    _require_windows()
    token = _read_hidden_token()
    project_id, token_env = _credential_identity(token)
    scopes, _ = _validate_project(_request_project(token), project_id)
    broadcast_sent = _store_current_user_environment(token_env, token)
    return {
        "status": "stored",
        "project_id": project_id,
        "environment_variable": token_env,
        "granted_scopes": scopes,
        "scope": "windows_current_user",
        "restart_required": True,
        "environment_change_broadcast": broadcast_sent,
    }


def _preflight_command(project_root: Path) -> dict[str, object]:
    config = _load_config(project_root)
    token_env = config["token_env"]
    token = os.environ.get(token_env)
    if token is None:
        raise ProjectContextError(f"The required project environment variable {token_env} is missing.")
    token_project_id, derived_token_env = _credential_identity(token)
    if token_project_id != config["project_id"] or derived_token_env != token_env:
        raise ProjectContextError("The project environment key does not match the configured Acceptora project.")
    scopes, instructions = _validate_project(_request_project(token), config["project_id"])
    try:
        update = _github_update_status(config["installed_commit"])
    except ProjectContextError as error:
        update = {
            "status": "unavailable",
            "repository": REPOSITORY_URL,
            "branch": PRODUCTION_BRANCH,
            "installed_commit": config["installed_commit"],
            "auto_apply": False,
            "error": str(error),
        }
    return {
        "status": "ready",
        "project_id": config["project_id"],
        "environment_variable": token_env,
        "granted_scopes": scopes,
        "verification_instructions": instructions,
        "skill_update": update,
    }


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Load one project-scoped Acceptora context.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_SafeArgumentParser)
    commands.add_parser("validate", add_help=True)
    commands.add_parser("store-windows", add_help=True)
    preflight = commands.add_parser("preflight", add_help=True)
    preflight.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "validate":
            result = _validate_command()
        elif arguments.command == "store-windows":
            result = _store_windows_command()
        else:
            result = _preflight_command(arguments.project_root)
    except ProjectContextError as error:
        print(_compact_json({"status": "error", "error": str(error)}), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print(_compact_json({"status": "cancelled"}), file=sys.stderr)
        return 130
    except Exception:
        print(_compact_json({"status": "error", "error": "Project context failed safely."}), file=sys.stderr)
        return 1
    print(_compact_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
