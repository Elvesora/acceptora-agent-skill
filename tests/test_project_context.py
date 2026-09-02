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
CREDENTIAL_ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAA"
PROJECT_ID = f"proj_{PROJECT_ULID}"
TOKEN_ENV = "ACCEPTORA_PROJECT_TOKEN"
TOKEN = f"avt_{CREDENTIAL_ULID}_" + ("A" * 48)
SECOND_PROJECT_ID = "proj_01BX5ZZKBKACTAV9WEVGEMMVRZ"
SECOND_TOKEN = "avt_01BX5ZZKBKACTAV9WEVGEMMVRX_" + ("B" * 48)
SCOPES = sorted(PROJECT_CONTEXT.REQUIRED_SCOPES | {"exceptions:write"})


def instructions(revision: int = 1) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "account_revision": revision,
        "project_revision": 2,
        "instructions": {
            "analysis_guidance": f"Fresh account guidance {revision}.",
            "manual_verification_guidance": "Open the generated project link.",
            "test_data_guidance": "Use the default test data guidance.",
        },
        "sources": {
            "analysis_guidance": "account",
            "manual_verification_guidance": "project",
            "test_data_guidance": "default",
        },
    }
    return {**payload, "effective_digest": "sha256:" + ("a" * 64), "configured": True}


def project_payload(revision: int = 1, *, project_id: str = PROJECT_ID) -> dict[str, object]:
    return {
        "project_id": project_id,
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


def write_config(
    root: Path,
    *,
    installed_version: str = "1.0.0",
    project_id: str = PROJECT_ID,
    token_env: str = TOKEN_ENV,
) -> None:
    config = root / ".acceptora" / "config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "project_id": project_id,
                "token_env": token_env,
                "origin": PROJECT_CONTEXT.ACCEPTORA_ORIGIN,
                "installed_version": installed_version,
            }
        ),
        encoding="utf-8",
    )


def write_project_token(root: Path, token: str = TOKEN) -> None:
    (root / ".acceptora-env").write_text(f"{TOKEN_ENV}={token}\n", encoding="utf-8")


def run_main(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "stderr", stderr),
    ):
        result = PROJECT_CONTEXT.main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class ProjectContextTest(unittest.TestCase):
    def assert_degraded_preflight(self, status: int, stdout: str, stderr: str) -> dict[str, object]:
        self.assertEqual(0, status, stderr)
        self.assertEqual("", stderr)
        result = json.loads(stdout)
        self.assertEqual("degraded", result["status"])
        self.assertIsNone(result["verification_instructions"])
        self.assertIsInstance(result["warning"], str)
        return result

    def test_preflight_uses_only_the_project_acceptora_env_token(self) -> None:
        opener = Opener(project_payload())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            write_project_token(root)
            application_env = root / ".env"
            application_env_contents = f"{TOKEN_ENV}={SECOND_TOKEN}\n"
            application_env.write_text(application_env_contents, encoding="utf-8")
            with (
                patch.dict(os.environ, {TOKEN_ENV: SECOND_TOKEN}, clear=True),
                patch.object(PROJECT_CONTEXT, "_project_opener", return_value=opener),
                patch.object(
                    PROJECT_CONTEXT,
                    "_npm_update_status",
                    return_value={"status": "current"},
                ),
            ):
                status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])
            self.assertEqual(application_env_contents, application_env.read_text(encoding="utf-8"))

        self.assertEqual(0, status, stderr)
        result = json.loads(stdout)
        self.assertEqual("ready", result["status"])
        self.assertEqual(PROJECT_ID, result["project_id"])
        self.assertEqual(TOKEN_ENV, result["environment_variable"])
        self.assertEqual(SCOPES, result["granted_scopes"])
        self.assertNotIn(TOKEN, stdout + stderr)
        self.assertNotIn(SECOND_TOKEN, stdout + stderr)
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
            write_project_token(root)
            with (
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
            write_project_token(root)
            with (
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

    def test_preflight_degrades_without_blocking_when_instructions_are_invalid(self) -> None:
        payload = project_payload()
        payload["verification_instructions"] = {"schema_version": "1.0"}
        opener = Opener(payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            write_project_token(root)
            with (
                patch.object(PROJECT_CONTEXT, "_project_opener", return_value=opener),
                patch.object(PROJECT_CONTEXT, "_npm_update_status", return_value={"status": "current"}),
            ):
                status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        result = self.assert_degraded_preflight(status, stdout, stderr)
        self.assertEqual(PROJECT_ID, result["project_id"])
        self.assertEqual(SCOPES, result["granted_scopes"])
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_preflight_degrades_for_rejected_key_foreign_project_and_missing_scopes(self) -> None:
        foreign_project = project_payload(project_id=SECOND_PROJECT_ID)
        missing_scopes = project_payload()
        missing_scopes["granted_scopes"] = SCOPES[1:]
        cases = (
            ("rejected-key", None, PROJECT_CONTEXT.ProjectContextError("Acceptora rejected the supplied project key.")),
            ("foreign-project", foreign_project, None),
            ("missing-scopes", missing_scopes, None),
        )

        for case, payload, error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_config(root)
                write_project_token(root)
                request = (
                    patch.object(PROJECT_CONTEXT, "_request_project", side_effect=error)
                    if error is not None
                    else patch.object(PROJECT_CONTEXT, "_request_project", return_value=payload)
                )
                with (
                    request,
                    patch.object(PROJECT_CONTEXT, "_npm_update_status", return_value={"status": "current"}),
                ):
                    status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

            self.assert_degraded_preflight(status, stdout, stderr)
            self.assertNotIn(TOKEN, stdout + stderr)

    def test_preflight_does_not_fall_back_to_application_env_or_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            (root / ".env").write_text(f"{TOKEN_ENV}={TOKEN}\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {TOKEN_ENV: TOKEN}, clear=True),
                patch.object(
                    PROJECT_CONTEXT,
                    "_project_opener",
                    side_effect=AssertionError("missing .acceptora-env reached network"),
                ),
            ):
                status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        self.assert_degraded_preflight(status, stdout, stderr)
        self.assertNotIn(TOKEN, stdout + stderr)

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

    def test_instruction_contract_accepts_effective_defaults_without_custom_configuration(self) -> None:
        context = {
            "schema_version": "1.0",
            "account_revision": 0,
            "project_revision": 0,
            "effective_digest": "sha256:" + ("b" * 64),
            "configured": False,
            "instructions": {
                "analysis_guidance": "Default analysis guidance.",
                "manual_verification_guidance": "Default manual verification guidance.",
                "test_data_guidance": "Default test data guidance.",
            },
            "sources": {
                "analysis_guidance": "default",
                "manual_verification_guidance": "default",
                "test_data_guidance": "default",
            },
        }

        self.assertIs(context, PROJECT_CONTEXT._validate_instructions(context))

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

    def test_preflight_rejects_duplicate_and_malformed_token_files_without_network(self) -> None:
        documents = (
            ("duplicate", f"{TOKEN_ENV}={TOKEN}\n{TOKEN_ENV}={SECOND_TOKEN}\n"),
            ("invalid-token", f"{TOKEN_ENV}=not-a-project-key\n"),
            ("unexpected-assignment", f"UNEXPECTED={TOKEN}\n"),
            ("nul-byte", f"{TOKEN_ENV}={TOKEN}\0\n"),
        )
        for case, document in documents:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_config(root)
                (root / ".acceptora-env").write_text(document, encoding="utf-8")
                with patch.object(
                    PROJECT_CONTEXT,
                    "_project_opener",
                    side_effect=AssertionError("invalid token file reached network"),
                ):
                    status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

            self.assert_degraded_preflight(status, stdout, stderr)
            self.assertNotIn(TOKEN, stdout + stderr)
            self.assertNotIn(SECOND_TOKEN, stdout + stderr)

    def test_preflight_rejects_non_regular_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            (root / ".acceptora-env").mkdir()

            status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        self.assert_degraded_preflight(status, stdout, stderr)

    def test_preflight_rejects_symlinked_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root)
            write_project_token(root)

            with patch.object(Path, "is_symlink", lambda path: path.name == ".acceptora-env"):
                status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        self.assert_degraded_preflight(status, stdout, stderr)
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_preflight_rejects_legacy_project_derived_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_config(root, token_env=f"ACCEPTORA_AGENT_TOKEN_{PROJECT_ID.upper()}")
            write_project_token(root)

            status, stdout, stderr = run_main(["preflight", "--project-root", str(root)])

        self.assert_degraded_preflight(status, stdout, stderr)
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_preflight_keeps_two_project_roots_and_keys_isolated(self) -> None:
        first_opener = Opener(project_payload())
        second_opener = Opener(project_payload(project_id=SECOND_PROJECT_ID))
        with tempfile.TemporaryDirectory() as first_temporary, tempfile.TemporaryDirectory() as second_temporary:
            first_root = Path(first_temporary)
            second_root = Path(second_temporary)
            write_config(first_root)
            write_project_token(first_root)
            write_config(second_root, project_id=SECOND_PROJECT_ID)
            write_project_token(second_root, SECOND_TOKEN)
            with patch.object(PROJECT_CONTEXT, "_npm_update_status", return_value={"status": "current"}):
                with patch.object(PROJECT_CONTEXT, "_project_opener", return_value=first_opener):
                    first = run_main(["preflight", "--project-root", str(first_root)])
                with patch.object(PROJECT_CONTEXT, "_project_opener", return_value=second_opener):
                    second = run_main(["preflight", "--project-root", str(second_root)])

        self.assertEqual(0, first[0], first[2])
        self.assertEqual(0, second[0], second[2])
        self.assertEqual(f"Bearer {TOKEN}", first_opener.requests[0].get_header("Authorization"))
        self.assertEqual(f"Bearer {SECOND_TOKEN}", second_opener.requests[0].get_header("Authorization"))
        combined_output = first[1] + first[2] + second[1] + second[2]
        self.assertNotIn(TOKEN, combined_output)
        self.assertNotIn(SECOND_TOKEN, combined_output)


if __name__ == "__main__":
    unittest.main()
