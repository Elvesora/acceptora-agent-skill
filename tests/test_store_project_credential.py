from __future__ import annotations

import importlib.util
import io
import json
import unittest
import warnings
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE_ROOT / "scripts" / "store_project_credential.py"
TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)
PROJECT_ID = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TOKEN_ENV = "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def load_module():
    specification = importlib.util.spec_from_file_location("acceptora_store_project_credential", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def close(self) -> None:
        return None


class Opener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.request = None

    def open(self, request, timeout: int):
        self.request = request
        return Response(self.payload)


class RegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        return None


class Registry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.create_calls = []
        self.set_calls = []

    def CreateKeyEx(self, root, path: str, reserved: int, access: int) -> RegistryKey:
        self.create_calls.append((root, path, reserved, access))
        return RegistryKey()

    def SetValueEx(self, key, name: str, reserved: int, value_type: int, value: str) -> None:
        self.set_calls.append((name, reserved, value_type, value))
        self.values[name] = value

    def QueryValueEx(self, key, name: str) -> tuple[str, int]:
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def DeleteValue(self, key, name: str) -> None:
        del self.values[name]


class StoreProjectCredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_project_identity_and_environment_name_are_derived_from_key(self) -> None:
        self.assertEqual((PROJECT_ID, TOKEN_ENV), self.module._credential_identity(TOKEN))

        with self.assertRaises(self.module.CredentialStoreError):
            self.module._credential_identity("not-a-project-key")

    def test_authenticated_project_and_required_scopes_are_checked_before_storage(self) -> None:
        scopes = sorted(self.module.REQUIRED_SCOPES)
        opener = Opener({"project_id": PROJECT_ID, "granted_scopes": scopes})

        self.module._validate_remote_credential(TOKEN, PROJECT_ID, opener=opener)

        self.assertEqual("Bearer " + TOKEN, opener.request.get_header("Authorization"))

        for payload in (
            {"project_id": "proj_01ARZ3NDEKTSV4RRFFQ69G5FAA", "granted_scopes": scopes},
            {"project_id": PROJECT_ID, "granted_scopes": scopes[:-1]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(self.module.CredentialStoreError):
                    self.module._validate_remote_credential(TOKEN, PROJECT_ID, opener=Opener(payload))

    def test_windows_registry_write_is_project_scoped_and_verified(self) -> None:
        registry = Registry()

        self.module._write_windows_registry(registry, TOKEN_ENV, TOKEN)
        self.module._write_windows_registry(registry, TOKEN_ENV, TOKEN)

        self.assertEqual({TOKEN_ENV: TOKEN}, registry.values)
        self.assertEqual(
            [
                (
                    registry.HKEY_CURRENT_USER,
                    "Environment",
                    0,
                    registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE,
                ),
                (
                    registry.HKEY_CURRENT_USER,
                    "Environment",
                    0,
                    registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE,
                ),
            ],
            registry.create_calls,
        )
        self.assertTrue(all(call[2] == registry.REG_SZ for call in registry.set_calls))

    def test_environment_notification_contains_no_credential(self) -> None:
        calls = []

        def send_message(*arguments):
            calls.append(arguments)
            return 1

        self.assertTrue(self.module._send_windows_environment_change(send_message))
        self.assertEqual(1, len(calls))
        self.assertEqual("Environment", calls[0][3].value)
        self.assertNotIn(TOKEN, repr(calls))

    def test_main_never_prints_or_passes_the_key_as_an_argument(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO(TOKEN + "\n")

        with (
            mock.patch.object(self.module.os, "name", "nt"),
            mock.patch.object(self.module.sys, "stdin", stdin),
            mock.patch.object(self.module.sys, "stdout", stdout),
            mock.patch.object(self.module.sys, "stderr", stderr),
            mock.patch.object(self.module, "_validate_remote_credential") as validate,
            mock.patch.object(self.module, "_store_current_user_environment", return_value=True) as store,
        ):
            result = self.module.main([])

        self.assertEqual(0, result)
        validate.assert_called_once_with(TOKEN, PROJECT_ID)
        store.assert_called_once_with(TOKEN_ENV, TOKEN)
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(
            {
                "environment_change_broadcast": True,
                "environment_variable": TOKEN_ENV,
                "project_id": PROJECT_ID,
                "restart_required": True,
                "scope": "windows_current_user",
                "status": "stored",
            },
            json.loads(stdout.getvalue()),
        )

    def test_validate_only_authenticates_without_persistence_on_non_windows(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO(TOKEN + "\n")

        with (
            mock.patch.object(self.module.os, "name", "posix"),
            mock.patch.object(self.module.sys, "stdin", stdin),
            mock.patch.object(self.module.sys, "stdout", stdout),
            mock.patch.object(self.module.sys, "stderr", stderr),
            mock.patch.object(self.module, "_validate_remote_credential") as validate,
            mock.patch.object(self.module, "_store_current_user_environment") as store,
        ):
            result = self.module.main(["--validate-only", "--expect-project", PROJECT_ID])

        self.assertEqual(0, result)
        validate.assert_called_once_with(TOKEN, PROJECT_ID)
        store.assert_not_called()
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(
            {
                "environment_variable": TOKEN_ENV,
                "persistence_performed": False,
                "project_id": PROJECT_ID,
                "status": "validated",
            },
            json.loads(stdout.getvalue()),
        )

    def test_selected_project_mismatch_fails_before_network_or_storage(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO(TOKEN + "\n")

        with (
            mock.patch.object(self.module.os, "name", "nt"),
            mock.patch.object(self.module.sys, "stdin", stdin),
            mock.patch.object(self.module.sys, "stdout", stdout),
            mock.patch.object(self.module.sys, "stderr", stderr),
            mock.patch.object(self.module, "_validate_remote_credential") as validate,
            mock.patch.object(self.module, "_store_current_user_environment") as store,
        ):
            result = self.module.main(["--expect-project", "proj_01ARZ3NDEKTSV4RRFFQ69G5FAA"])

        self.assertEqual(2, result)
        validate.assert_not_called()
        store.assert_not_called()
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())

    def test_failed_acceptora_validation_performs_no_environment_write(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO(TOKEN + "\n")

        with (
            mock.patch.object(self.module.os, "name", "nt"),
            mock.patch.object(self.module.sys, "stdin", stdin),
            mock.patch.object(self.module.sys, "stdout", stdout),
            mock.patch.object(self.module.sys, "stderr", stderr),
            mock.patch.object(
                self.module,
                "_validate_remote_credential",
                side_effect=self.module.CredentialStoreError("Acceptora rejected the supplied project key."),
            ) as validate,
            mock.patch.object(self.module, "_store_current_user_environment") as store,
        ):
            result = self.module.main([])

        self.assertEqual(2, result)
        validate.assert_called_once_with(TOKEN, PROJECT_ID)
        store.assert_not_called()
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())

    def test_unsupported_platform_fails_closed(self) -> None:
        stdin = mock.Mock()
        with (
            mock.patch.object(self.module.os, "name", "posix"),
            mock.patch.object(self.module.sys, "stdin", stdin),
            mock.patch.object(self.module.sys, "stderr", io.StringIO()),
        ):
            with self.assertRaises(self.module.CredentialStoreError):
                self.module._store_current_user_environment(TOKEN_ENV, TOKEN)
            self.assertEqual(2, self.module.main([]))
        stdin.readline.assert_not_called()

    def test_invalid_command_arguments_do_not_echo_a_key(self) -> None:
        for arguments in ([TOKEN], ["--token", TOKEN]):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with (
                    mock.patch.object(self.module.sys, "stderr", stderr),
                    mock.patch.object(self.module.os, "name", "nt"),
                ):
                    result = self.module.main(arguments)

                self.assertEqual(2, result)
                self.assertNotIn(TOKEN, stderr.getvalue())

    def test_hidden_prompt_fails_closed_if_getpass_would_echo(self) -> None:
        stdin = mock.Mock()
        stdin.isatty.return_value = True

        def unsafe_getpass(prompt: str) -> str:
            warnings.warn("echo fallback", self.module.getpass.GetPassWarning)
            return TOKEN

        with (
            mock.patch.object(self.module.sys, "stdin", stdin),
            mock.patch.object(self.module.getpass, "getpass", side_effect=unsafe_getpass),
        ):
            with self.assertRaises(self.module.CredentialStoreError):
                self.module._read_credential()


if __name__ == "__main__":
    unittest.main()
