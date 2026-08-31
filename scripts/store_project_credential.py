#!/usr/bin/env python3
"""Validate one project-scoped Acceptora credential and optionally persist it on Windows."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import re
import ssl
import sys
import warnings
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


PROJECT_URL = "https://www.acceptora.com/api/v1/integrations/project"
TOKEN_PATTERN = re.compile(r"^avt_(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})_[A-Za-z0-9]{48}$")
PROJECT_ID_PATTERN = re.compile(r"^proj_[0-9A-HJKMNP-TV-Z]{26}$")
PROJECT_TOKEN_ENV_PREFIX = "ACCEPTORA_AGENT_TOKEN_PROJ_"
REQUIRED_SCOPES = {
    "projects:read",
    "features:resolve",
    "features:read",
    "checklists:write",
    "feedback:read",
    "feedback:address",
    "gates:read",
}
MAX_RESPONSE_BYTES = 1_048_576


class CredentialStoreError(RuntimeError):
    """Raised when a credential cannot be validated or stored safely."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CredentialStoreError("Invalid credential helper arguments.")


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


def _credential_identity(token: str) -> tuple[str, str]:
    match = TOKEN_PATTERN.fullmatch(token)
    if match is None:
        raise CredentialStoreError("The supplied value is not a valid Acceptora project key.")
    ulid = match.group("ulid")
    return f"proj_{ulid}", f"{PROJECT_TOKEN_ENV_PREFIX}{ulid}"


def _read_credential() -> str:
    if sys.stdin.isatty():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass("Acceptora project key: ")
        except getpass.GetPassWarning:
            raise CredentialStoreError("A hidden credential prompt is not available.") from None
    value = sys.stdin.readline(256)
    if not value or len(value) >= 256 or sys.stdin.read(1):
        raise CredentialStoreError("Read exactly one Acceptora project key from standard input.")
    return value.removesuffix("\n").removesuffix("\r")


def _project_opener() -> Any:
    return build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))


def _require_supported_platform() -> None:
    if os.name != "nt":
        raise CredentialStoreError(
            "Persistent current-user credential storage is not supported on this operating system."
        )


def _validate_remote_credential(token: str, project_id: str, *, opener: Any | None = None) -> None:
    request = Request(
        PROJECT_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Acceptora-Agent-Skill-Credential-Setup",
        },
        method="GET",
    )
    try:
        response = (opener or _project_opener()).open(request, timeout=15)
        try:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise CredentialStoreError("Acceptora rejected the supplied project key.") from None
        raise CredentialStoreError("Acceptora could not validate the project key.") from None
    except (OSError, URLError):
        raise CredentialStoreError("Acceptora could not validate the project key.") from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise CredentialStoreError("Acceptora returned an invalid project response.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CredentialStoreError("Acceptora returned an invalid project response.") from None
    if not isinstance(payload, dict) or payload.get("project_id") != project_id:
        raise CredentialStoreError("The project key does not match its authenticated Acceptora project.")
    granted_scopes = payload.get("granted_scopes")
    if not isinstance(granted_scopes, list) or any(not isinstance(scope, str) for scope in granted_scopes):
        raise CredentialStoreError("The project key response did not include valid scopes.")
    if not REQUIRED_SCOPES.issubset(granted_scopes):
        raise CredentialStoreError("The project key lacks required Acceptora workflow scopes.")


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
            raise CredentialStoreError("Windows did not confirm the current-user environment update.") from None


def _send_windows_environment_change(send_message: Any) -> bool:
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
        return _send_windows_environment_change(send_message)
    except Exception:
        return False


def _store_current_user_environment(name: str, token: str) -> bool:
    _require_supported_platform()
    import winreg

    _write_windows_registry(winreg, name, token)
    return _broadcast_windows_environment_change()


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Validate and store one Acceptora project key without accepting it as a command argument."
    )
    parser.add_argument(
        "--expect-project",
        help="Optional proj_<ULID> assertion from an already validated install receipt.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the project key without storing it.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.expect_project is not None and PROJECT_ID_PATTERN.fullmatch(args.expect_project) is None:
            raise CredentialStoreError("--expect-project must use the public proj_<ULID> form.")
        if not args.validate_only:
            _require_supported_platform()
        token = _read_credential()
        project_id, environment_variable = _credential_identity(token)
        if args.expect_project is not None and project_id != args.expect_project:
            raise CredentialStoreError("The supplied project key does not match the selected Acceptora project.")
        _validate_remote_credential(token, project_id)
        if not args.validate_only:
            broadcast_sent = _store_current_user_environment(environment_variable, token)
    except CredentialStoreError as exc:
        print(f"Credential setup failed: {exc}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Credential setup cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print("Credential setup failed safely.", file=sys.stderr)
        return 1

    if args.validate_only:
        result = {
            "status": "validated",
            "project_id": project_id,
            "environment_variable": environment_variable,
            "persistence_performed": False,
        }
    else:
        result = {
            "status": "stored",
            "project_id": project_id,
            "environment_variable": environment_variable,
            "scope": "windows_current_user",
            "restart_required": True,
            "environment_change_broadcast": broadcast_sent,
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
