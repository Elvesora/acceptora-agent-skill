from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_ROOT / "adapters" / "hook_runtime.py"
SPEC = importlib.util.spec_from_file_location("acceptora_release_update_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK_RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOOK_RUNTIME
SPEC.loader.exec_module(HOOK_RUNTIME)


SOURCE_COMMIT = "c" * 40
BUNDLE_SHA256 = "sha256:" + ("d" * 64)
MANIFEST_URL = "https://acceptora.example/agent-skill/release-manifest.json"
BUNDLE_URL = "https://acceptora.example/agent-skill/verify-generated-work.zip"


def release_file(path: str, digest_character: str) -> dict[str, Any]:
    return {
        "path": path,
        "archive_path": f"verify-generated-work/{path}",
        "size": 123,
        "mode": "0755" if path.endswith(".py") else "0644",
        "sha256": "sha256:" + (digest_character * 64),
    }


def source_tree_sha256(files: list[dict[str, Any]]) -> str:
    body = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


INSTALLED_RELEASE_FILES = [
    release_file("SKILL.md", "a"),
    release_file("config/package-manifest.json", "b"),
    release_file("scripts/install.py", "c"),
]
PUBLISHED_RELEASE_FILES = [
    release_file("SKILL.md", "d"),
    release_file("config/package-manifest.json", "e"),
    release_file("scripts/install.py", "f"),
]
INSTALLED_SOURCE_TREE_SHA256 = source_tree_sha256(INSTALLED_RELEASE_FILES)
PUBLISHED_SOURCE_TREE_SHA256 = source_tree_sha256(PUBLISHED_RELEASE_FILES)


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        self.body = body
        self.headers = headers
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class FakeOpener:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[tuple[Any, float]] = []

    def open(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def release_manifest(
    *,
    version: str = "1.1.0",
    files: list[dict[str, Any]] | None = None,
    source_tree_digest: str | None = None,
    source_state: str = "clean",
) -> dict[str, Any]:
    selected_files = [dict(entry) for entry in (files if files is not None else PUBLISHED_RELEASE_FILES)]
    return {
        "schema_version": 1,
        "name": "verify-generated-work",
        "version": version,
        "integration_version": version,
        "contract_version": "1.0.0",
        "mcp_protocol_version": "2025-11-25",
        "source_commit": SOURCE_COMMIT,
        "source_state": source_state,
        "source_tree_sha256": source_tree_digest or source_tree_sha256(selected_files),
        "supported_clients": ["codex", "claude-code", "gemini-cli"],
        "reference_client_builds": {},
        "archive_prefix": "verify-generated-work",
        "files": selected_files,
        "artifacts": [
            {
                "filename": f"verify-generated-work-{version}.zip",
                "format": "zip",
                "size": 1234,
                "sha256": BUNDLE_SHA256,
            },
            {
                "filename": f"verify-generated-work-{version}.tar.gz",
                "format": "tar.gz",
                "size": 1235,
                "sha256": "sha256:" + ("e" * 64),
            },
        ],
    }


def encoded_response(
    manifest: dict[str, Any],
    *,
    digest: str | None = None,
    status: int = 200,
) -> FakeResponse:
    body = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    return raw_response(body, digest=digest, status=status)


def raw_response(
    body: bytes,
    *,
    content_type: str = "application/json; charset=UTF-8",
    digest: str | None = None,
    include_digest: bool = True,
    status: int = 200,
) -> FakeResponse:
    actual = "sha256:" + hashlib.sha256(body).hexdigest()
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    if include_digest:
        headers["X-Acceptora-Artifact-SHA256"] = digest or actual
    return FakeResponse(
        body,
        headers,
        status,
    )


def runtime_config(client: str = "codex") -> dict[str, Any]:
    return {
        "client": client,
        "release_manifest_url": MANIFEST_URL,
        "release_bundle_url": BUNDLE_URL,
        "release_update_timeout_seconds": 2,
        "installed_source_tree_sha256": INSTALLED_SOURCE_TREE_SHA256,
    }


class ReleaseUpdateCheckTest(unittest.TestCase):
    def test_verified_newer_release_returns_digest_bound_notice_without_token_or_bundle_request(self) -> None:
        opener = FakeOpener(encoded_response(release_manifest()))

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            HOOK_RUNTIME.urllib.request,
            "build_opener",
            return_value=opener,
        ):
            cache_path = Path(temporary) / "release-update.json"
            notice = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1000)

            self.assertIsInstance(notice, str)
            self.assertIn("1.0.0 -> 1.1.0", notice)
            self.assertIn(BUNDLE_SHA256, notice)
            self.assertIn("No bundle was downloaded", notice)
            self.assertEqual(1, len(opener.requests))
            request, timeout = opener.requests[0]
            self.assertEqual(MANIFEST_URL, request.full_url)
            self.assertEqual("GET", request.method)
            self.assertEqual(2, timeout)
            self.assertIsNone(request.get_header("Authorization"))
            self.assertNotIn("ACCEPTORA_AGENT_TOKEN", str(request.header_items()))

            record = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual("update_available", record["status"])
            self.assertEqual(0, record["setup_mutations_performed"])
            self.assertTrue(record["cache_written"])
            self.assertFalse(record["auto_apply"])
            self.assertEqual(BUNDLE_URL, record["published"]["bundle"]["download_url"])
            self.assertEqual(HOOK_RUNTIME._record_digest(record), record["record_sha256"])

    def test_fresh_verified_cache_avoids_network_and_expiry_rechecks(self) -> None:
        first_opener = FakeOpener(encoded_response(release_manifest()))
        second_opener = FakeOpener(encoded_response(release_manifest()))

        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "release-update.json"
            with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=first_opener):
                first = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1000)
            with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=second_opener):
                cached = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1299)
                refreshed = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1301)

            self.assertEqual(first, cached)
            self.assertIn("1.0.0 -> 1.1.0", refreshed)
            self.assertIn(BUNDLE_SHA256, refreshed)
            self.assertEqual(1, len(first_opener.requests))
            self.assertEqual(1, len(second_opener.requests))

    def test_verified_cache_is_bound_to_the_installed_client(self) -> None:
        codex_opener = FakeOpener(encoded_response(release_manifest()))
        claude_opener = FakeOpener(encoded_response(release_manifest()))

        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "release-update.json"
            with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=codex_opener):
                HOOK_RUNTIME._check_release_update(runtime_config("codex"), cache_path, now=1000)
            with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=claude_opener):
                HOOK_RUNTIME._check_release_update(runtime_config("claude-code"), cache_path, now=1001)

            self.assertEqual(1, len(codex_opener.requests))
            self.assertEqual(1, len(claude_opener.requests))
            self.assertEqual("claude-code", json.loads(cache_path.read_text(encoding="utf-8"))["client"])

    def test_current_identity_is_silent_and_identity_reuse_or_older_release_warns(self) -> None:
        cases = (
            (release_manifest(version="1.0.0", files=INSTALLED_RELEASE_FILES), None, "current"),
            (release_manifest(version="1.0.0+build.1", files=INSTALLED_RELEASE_FILES), None, "current"),
            (release_manifest(version="1.0.0"), "reused a different release identity", "identity_conflict"),
            (release_manifest(version="0.9.0"), "older than installed", "published_older"),
        )
        for manifest, expected_notice, expected_status in cases:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as temporary:
                opener = FakeOpener(encoded_response(manifest))
                cache_path = Path(temporary) / "release-update.json"
                with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=opener):
                    notice = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1000)

                if expected_notice is None:
                    self.assertIsNone(notice)
                else:
                    self.assertIn(expected_notice, notice)
                self.assertEqual(expected_status, json.loads(cache_path.read_text(encoding="utf-8"))["status"])

        self.assertEqual(1, HOOK_RUNTIME._compare_semantic_versions("1.10.0", "1.9.0"))
        self.assertEqual(0, HOOK_RUNTIME._compare_semantic_versions("1.0.0+build.2", "1.0.0+build.1"))
        self.assertEqual(1, HOOK_RUNTIME._compare_semantic_versions("1.0.0", "1.0.0-rc.1"))

    def test_unavailable_or_redirected_manifest_fails_open_and_is_cached_without_following(self) -> None:
        failures = (
            urllib.error.HTTPError(MANIFEST_URL, 503, "unavailable", {}, None),
            urllib.error.HTTPError(MANIFEST_URL, 302, "redirect", {"Location": "https://evil.example"}, None),
            urllib.error.URLError("offline"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                opener = FakeOpener(failure)
                cache_path = Path(temporary) / "release-update.json"
                with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=opener) as build_opener:
                    notice = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1000)

                self.assertIsNone(notice)
                self.assertEqual(1, len(opener.requests))
                self.assertIsInstance(build_opener.call_args.args[0], HOOK_RUNTIME._NoRedirect)
                self.assertEqual("unavailable", json.loads(cache_path.read_text(encoding="utf-8"))["status"])
                cached_opener = FakeOpener(AssertionError("fresh unavailable cache reached the network"))
                with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=cached_opener):
                    self.assertIsNone(HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1200))
                self.assertEqual([], cached_opener.requests)

    def test_untrusted_or_oversized_manifest_is_rejected_and_safely_cached(self) -> None:
        empty_inventory = release_manifest()
        empty_inventory["files"] = []
        boolean_schema = release_manifest()
        boolean_schema["schema_version"] = True
        unsafe_inventory = release_manifest(files=[release_file("../SKILL.md", "f")])
        dot_inventory = release_manifest(files=[release_file(".", "f")])
        duplicate_inventory = release_manifest(
            files=[release_file("SKILL.md", "a"), release_file("SKILL.md", "b")],
        )
        wrong_name = release_manifest()
        wrong_name["name"] = "attacker-controlled-package"
        invalid_version = release_manifest()
        invalid_version["version"] = "latest"
        invalid_commit = release_manifest()
        invalid_commit["source_commit"] = "not-a-commit"
        unsupported_client = release_manifest()
        unsupported_client["supported_clients"] = ["claude-code"]
        missing_zip = release_manifest()
        missing_zip["artifacts"] = []
        duplicate_zip = release_manifest()
        duplicate_zip["artifacts"] = [duplicate_zip["artifacts"][0], duplicate_zip["artifacts"][0]]
        invalid_cases = (
            encoded_response(release_manifest(), digest="sha256:" + ("0" * 64)),
            raw_response(b"not-json\n"),
            raw_response(b"{}\n", content_type="text/plain"),
            raw_response(b"{}\n", include_digest=False),
            encoded_response(release_manifest(source_state="dirty_allowed")),
            encoded_response(release_manifest(), status=206),
            encoded_response(empty_inventory),
            encoded_response(boolean_schema),
            encoded_response(unsafe_inventory),
            encoded_response(dot_inventory),
            encoded_response(duplicate_inventory),
            encoded_response(wrong_name),
            encoded_response(invalid_version),
            encoded_response(invalid_commit),
            encoded_response(unsupported_client),
            encoded_response(missing_zip),
            encoded_response(duplicate_zip),
            encoded_response(
                release_manifest(source_tree_digest="sha256:" + ("0" * 64)),
            ),
            FakeResponse(
                b"{}",
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(HOOK_RUNTIME.MAX_RELEASE_MANIFEST_BYTES + 1),
                    "X-Acceptora-Artifact-SHA256": "sha256:" + ("0" * 64),
                },
            ),
        )
        for response in invalid_cases:
            with self.subTest(length=response.headers["Content-Length"]), tempfile.TemporaryDirectory() as temporary:
                opener = FakeOpener(response)
                cache_path = Path(temporary) / "release-update.json"
                with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=opener):
                    notice = HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1000)

                self.assertIn("failed integrity checks", notice)
                record_text = cache_path.read_text(encoding="utf-8")
                record = json.loads(record_text)
                self.assertEqual("rejected", record["status"])
                self.assertIsNone(record["published"])
                self.assertNotIn("attacker-controlled-package", notice)
                self.assertNotIn("attacker-controlled-package", record_text)
                cached_opener = FakeOpener(AssertionError("fresh rejected cache reached the network"))
                with patch.object(HOOK_RUNTIME.urllib.request, "build_opener", return_value=cached_opener):
                    self.assertEqual(
                        notice,
                        HOOK_RUNTIME._check_release_update(runtime_config(), cache_path, now=1200),
                    )
                self.assertEqual([], cached_opener.requests)

    def test_only_session_start_uses_the_installer_owned_update_path(self) -> None:
        for event in (
            {"hook_event_name": "UserPromptSubmit"},
            {"hook_event_name": "BeforeAgent"},
            {"event_name": ""},
            {},
        ):
            with self.subTest(event=event), patch.object(
                HOOK_RUNTIME,
                "_project_root",
                side_effect=AssertionError("non-session event reached update check"),
            ):
                self.assertIsNone(HOOK_RUNTIME.check_for_skill_update(event))

        runtime_config_path = HOOK_RUNTIME.SKILL_ROOT / "config" / "runtime-config.json"
        root = Path("/workspace")
        with (
            patch.object(HOOK_RUNTIME, "_project_root", return_value=root),
            patch.object(
                HOOK_RUNTIME,
                "load_config",
                return_value={"config_source": "installer_owned_external_runtime"},
            ),
            patch.object(HOOK_RUNTIME, "_config_path", return_value=runtime_config_path),
            patch.object(HOOK_RUNTIME, "_release_update_cache_path", return_value=Path("/runtime/state/release-update.json")),
            patch.object(HOOK_RUNTIME, "_check_release_update", return_value="Update available.") as check,
        ):
            self.assertEqual(
                "Update available.",
                HOOK_RUNTIME.check_for_skill_update({"hook_event_name": "SessionStart"}),
            )
        check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
