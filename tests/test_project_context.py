from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_ROOT / "scripts" / "project_context.py"
SPEC = importlib.util.spec_from_file_location("acceptora_project_context", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PROJECT_CONTEXT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROJECT_CONTEXT
SPEC.loader.exec_module(PROJECT_CONTEXT)

PROJECT_ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PROJECT_ID = f"proj_{PROJECT_ULID}"
TOKEN_ENV = f"ACCEPTORA_AGENT_TOKEN_PROJ_{PROJECT_ULID}"
TOKEN = f"avt_{PROJECT_ULID}_" + ("A" * 48)
SCOPES = sorted(PROJECT_CONTEXT.REQUIRED_SCOPES | {"exceptions:write"})


def instructions(revision: int = 1) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "account_revision": revision,
        "project_revision": 2,
        "instructions": {
            "analysis_guidance": f"Fresh account guidance {revision}.",
            "manual_verification_guidance": "Open the generated project link.",
            "test_data_guidance": None,
        },
        "sources": {
            "analysis_guidance": "account",
            "manual_verification_guidance": "project",
            "test_data_guidance": "default",
        },
    }
    return {**payload, "effective_digest": "sha256:" + ("a" * 64), "configured": True}


def project_payload(revision: int = 1) -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "granted_scopes": SCOPES,
        "verification_instructions": instructions(revision),
    }


class Response:
    def __init__(self, payload: object, *, raw: bytes | None = None) -> None:
        self.body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, size: int) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


class Opener:
    def __init__(self, *payloads: object) -> None:
        self.payloads = list(payloads)
        self.requests: list[object] = []

    def open(self, request: object, timeout: int) -> Response:
        self.requests.append(request)
        return Response(self.payloads.pop(0))


class RegistryKey:
    def __enter__(self) -> "RegistryKey":
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        return None


class Registry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.paths: list[str] = []

    def CreateKeyEx(self, root: object, path: str, reserved: int, access: int) -> RegistryKey:
        self.paths.append(path)
        return RegistryKey()

    def QueryValueEx(self, key: object, name: str) -> tuple[str, int]:
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key: object, name: str, reserved: int, value_type: int, value: str) -> None:
        self.values[name] = value

    def DeleteValue(self, key: object, name: str) -> None:
        del self.values[name]


def write_config(root: Path, *, installed_version: str = "1.0.0") -> None:
    config = root / ".acceptora" / "config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "project_id": PROJECT_ID,
                "token_env": TOKEN_ENV,
                "origin": PROJECT_CONTEXT.ACCEPTORA_ORIGIN,
                "installed_version": installed_version,
            }
        ),
        encoding="utf-8",
    )


def run_main(arguments: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO(stdin)),
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "stderr", stderr),
    ):
        result = PROJECT_CONTEXT.main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class ProjectContextTest(unittest.TestCase):
    def test_validate_uses_hidden_stdin_and_returns_secret_free_project_identity(self) -> None:
        opener = Opener(project_payload())
        with patch.object(PROJECT_CONTEXT, "_project_opener", return_value=opener):
            status, stdout, stderr = run_main(
                ["validate"],
                stdin=TOKEN + "\n",
            )

        self.assertEqual(0, status, stderr)
        result = json.loads(stdout)
        self.assertEqual("validated", result["status"])
        self.assertEqual(PROJECT_ID, result["project_id"])
        self.assertEqual(TOKEN_ENV, result["environment_variable"])
        self.assertFalse(result["persistence_performed"])
        self.assertEqual(SCOPES, result["granted_scopes"])
        self.assertNotIn(TOKEN, stdout + stderr)
        request = opener.requests[0]
        self.assertEqual(PROJECT_CONTEXT.PROJECT_URL, request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertEqual(f"Bearer {TOKEN}", request.get_header("Authorization"))

    def test_preflight_fetches_fresh_instructions_on_every_run(self) -> None:
        opener = Opener(project_payload(1), project_payload(2))
        update = {
            "status": "current",
            "package": "acceptora-agent-skill",
            "registry": "https://registry.npmjs.org",
            "installed_version": "1.0.0",
            "latest_version": "1.0.0",
            "auto_apply": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            with (
                patch.dict(
                    os.environ,
                    {TOKEN_ENV: TOKEN, "ACCEPTORA_AGENT_TOKEN": "legacy-must-not-be-used"},
                    clear=False,
                ),
                patch.object(PROJECT_CONTEXT, "_project_opener", return_value=opener),
                patch.object(PROJECT_CONTEXT, "_npm_update_status", return_value=update) as check,
            ):
                first = run_main(["preflight", "--project-root", str(root)])
                second = run_main(["preflight", "--project-root", str(root)])

        self.assertEqual(0, first[0], first[2])
        self.assertEqual(0, second[0], second[2])
        first_result = json.loads(first[1])
        second_result = json.loads(second[1])
        self.assertEqual("Fresh account guidance 1.", first_result["verification_instructions"]["instructions"]["analysis_guidance"])
        self.assertEqual("Fresh account guidance 2.", second_result["verification_instructions"]["instructions"]["analysis_guidance"])
        self.assertEqual(2, len(opener.requests))
        self.assertEqual(2, check.call_count)
        self.assertNotIn(TOKEN, first[1] + first[2] + second[1] + second[2])

    def test_update_check_failure_does_not_hide_fresh_project_instructions(self) -> None:
        opener = Opener(project_payload())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            with (
                patch.dict(os.environ, {TOKEN_ENV: TOKEN}, clear=True),
                patch.object(PROJECT_CONTEXT, "_project_opener", return_value=opener),
                patch.object(
                    PROJECT_CONTEXT,
                    "_npm_update_status",
                    side_effect=PROJECT_CONTEXT.ProjectContextError("The update check is unavailable."),
                ),
            ):
                status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        self.assertEqual(0, status, stderr)
        result = json.loads(stdout)
        self.assertEqual("ready", result["status"])
        self.assertEqual("unavailable", result["skill_update"]["status"])
        self.assertEqual(
            "Fresh account guidance 1.",
            result["verification_instructions"]["instructions"]["analysis_guidance"],
        )
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_preflight_never_loads_a_key_from_project_env_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            (root / ".env").write_text(f"{TOKEN_ENV}={TOKEN}\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    PROJECT_CONTEXT,
                    "_project_opener",
                    side_effect=AssertionError("missing process environment reached network"),
                ),
            ):
                status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        self.assertEqual(2, status)
        self.assertEqual("", stdout)
        self.assertIn(TOKEN_ENV, stderr)
        self.assertNotIn(TOKEN, stderr)

    def test_project_identity_scopes_and_instruction_contract_are_enforced(self) -> None:
        cases = []
        wrong_project = project_payload()
        wrong_project["project_id"] = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAA"
        cases.append(wrong_project)
        missing_scope = project_payload()
        missing_scope["granted_scopes"] = SCOPES[1:]
        cases.append(missing_scope)
        malformed_digest = project_payload()
        malformed_digest["verification_instructions"] = {
            **instructions(),
            "effective_digest": "sha256:not-a-digest",
        }
        cases.append(malformed_digest)

        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(PROJECT_CONTEXT.ProjectContextError):
                PROJECT_CONTEXT._validate_project(payload, PROJECT_ID)

    def test_instruction_contract_accepts_additive_server_fields(self) -> None:
        payload = project_payload()
        verification_instructions = payload["verification_instructions"]
        assert isinstance(verification_instructions, dict)
        instruction_values = verification_instructions["instructions"]
        instruction_sources = verification_instructions["sources"]
        assert isinstance(instruction_values, dict)
        assert isinstance(instruction_sources, dict)
        verification_instructions["future_metadata"] = {"version": 2}
        instruction_values["future_guidance"] = "Future server guidance."
        instruction_sources["future_guidance"] = "account"

        scopes, validated = PROJECT_CONTEXT._validate_project(payload, PROJECT_ID)

        self.assertEqual(SCOPES, scopes)
        self.assertEqual({"version": 2}, validated["future_metadata"])
        self.assertEqual("Future server guidance.", validated["instructions"]["future_guidance"])

    def test_oversized_project_response_and_redirect_are_rejected(self) -> None:
        oversized = Response({}, raw=b"x" * (PROJECT_CONTEXT.MAX_RESPONSE_BYTES + 1))

        class OversizedOpener:
            def open(self, request: object, timeout: int) -> Response:
                return oversized

        with self.assertRaisesRegex(PROJECT_CONTEXT.ProjectContextError, "oversized"):
            PROJECT_CONTEXT._request_project(TOKEN, opener=OversizedOpener())

        class RedirectOpener:
            def open(self, request: object, timeout: int) -> Response:
                raise HTTPError(request.full_url, 302, "redirect", {}, None)

        with self.assertRaisesRegex(PROJECT_CONTEXT.ProjectContextError, "verification failed"):
            PROJECT_CONTEXT._request_project(TOKEN, opener=RedirectOpener())
        self.assertIsNone(PROJECT_CONTEXT._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test"))

    def test_update_check_reads_only_the_latest_npm_package_version(self) -> None:
        opener = Opener({"version": "1.1.0"})
        result = PROJECT_CONTEXT._npm_update_status("1.0.0", opener=opener)

        self.assertEqual("update_available", result["status"])
        self.assertFalse(result["auto_apply"])
        self.assertEqual("1.0.0", result["installed_version"])
        self.assertEqual("1.1.0", result["latest_version"])
        self.assertEqual(PROJECT_CONTEXT.NPM_PACKAGE_URL, opener.requests[0].full_url)
        self.assertIsNone(opener.requests[0].get_header("Authorization"))

    def test_store_windows_validates_before_persisting_only_the_derived_name(self) -> None:
        opener = Opener(project_payload())
        with (
            patch.object(PROJECT_CONTEXT, "_require_windows"),
            patch.object(PROJECT_CONTEXT, "_project_opener", return_value=opener),
            patch.object(PROJECT_CONTEXT, "_store_current_user_environment", return_value=True) as store,
        ):
            status, stdout, stderr = run_main(
                ["store-windows"],
                stdin=TOKEN + "\n",
            )

        self.assertEqual(0, status, stderr)
        store.assert_called_once_with(TOKEN_ENV, TOKEN)
        result = json.loads(stdout)
        self.assertEqual("stored", result["status"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(TOKEN_ENV, result["environment_variable"])
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_windows_registry_write_uses_only_the_project_derived_name(self) -> None:
        registry = Registry()

        PROJECT_CONTEXT._write_windows_registry(registry, TOKEN_ENV, TOKEN)

        self.assertEqual(["Environment"], registry.paths)
        self.assertEqual({TOKEN_ENV: TOKEN}, registry.values)

    def test_store_windows_fails_before_reading_stdin_on_other_platforms(self) -> None:
        with (
            patch.object(PROJECT_CONTEXT.os, "name", "posix"),
            patch.object(
                PROJECT_CONTEXT,
                "_read_hidden_token",
                side_effect=AssertionError("unsupported platform read stdin"),
            ),
        ):
            status, stdout, stderr = run_main(["store-windows"], stdin=TOKEN + "\n")

        self.assertEqual(2, status)
        self.assertEqual("", stdout)
        self.assertNotIn(TOKEN, stderr)


if __name__ == "__main__":
    unittest.main()
