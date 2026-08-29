from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_ROOT / "adapters" / "hook_runtime.py"
SPEC = importlib.util.spec_from_file_location("acceptora_hook_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK_RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOOK_RUNTIME
SPEC.loader.exec_module(HOOK_RUNTIME)
SOURCE_MANIFEST = sys.modules["build_source_manifest"]
INSTRUCTION_READER = sys.modules["read_instruction_snapshot"]
FEATURE_ID = "feat_01J00000000000000000000001"
VALID_TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)
PROJECT_ID = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def verification_instructions(*, account_revision: int = 4, project_revision: int = 2) -> dict[str, object]:
    digest_payload: dict[str, object] = {
        "schema_version": "1.0",
        "account_revision": account_revision,
        "project_revision": project_revision,
        "instructions": {
            "analysis_guidance": "OWNER-ANALYSIS-GUIDANCE",
            "manual_verification_guidance": "Use the exact generated feature URL.",
            "test_data_guidance": None,
        },
        "sources": {
            "analysis_guidance": "account",
            "manual_verification_guidance": "project",
            "test_data_guidance": "default",
        },
    }
    return {
        **digest_payload,
        "effective_digest": INSTRUCTION_READER.sha256_digest(digest_payload),
        "configured": True,
    }


def gate_response(payload: dict[str, object], **overrides: object) -> dict[str, object]:
    response: dict[str, object] = {
        "outcome": "pass",
        "feature_id": payload.get("feature_id") or FEATURE_ID,
        "reason_code": "VERIFICATION_DOCUMENT_CURRENT",
        "reason": "Current source is synchronized.",
        "last_synchronized_digest": payload["current_source_digest"],
        "last_synchronized_checklist_revision": 1,
        "recovery_instruction": None,
        "correlation_id": "corr-hook-gate",
    }
    response.update(overrides)
    return response


def run_git(root: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Manifest Test",
        "GIT_AUTHOR_EMAIL": "manifest@example.test",
        "GIT_COMMITTER_NAME": "Manifest Test",
        "GIT_COMMITTER_EMAIL": "manifest@example.test",
    }
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def initialize_git(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "config", "core.autocrlf", "false")


class RedirectServer(ThreadingHTTPServer):
    state: dict


class RedirectSourceHandler(BaseHTTPRequestHandler):
    server: RedirectServer

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        self.server.state["authorizations"].append(self.headers.get("Authorization"))
        self.send_response(302)
        self.send_header("Location", self.server.state["destination"])
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.do_POST()

    def log_message(self, format: str, *args: object) -> None:
        return


class RedirectDestinationHandler(BaseHTTPRequestHandler):
    server: RedirectServer

    def do_GET(self) -> None:  # noqa: N802
        self.server.state["authorizations"].append(self.headers.get("Authorization"))
        encoded = b'{}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class OversizedGateHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b'{"padding":"' + (b"x" * (HOOK_RUNTIME.MAX_GATE_RESPONSE_BYTES + 1)) + b'"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class TokenReasonServer(ThreadingHTTPServer):
    token: str
    request_count: int


class TokenReasonGateHandler(BaseHTTPRequestHandler):
    server: TokenReasonServer

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.request_count += 1
        self.send_response(503, self.server.token)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class ProjectMetadataServer(ThreadingHTTPServer):
    response: dict[str, object]
    requests: list[dict[str, str | None]]


class ProjectMetadataHandler(BaseHTTPRequestHandler):
    server: ProjectMetadataServer

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        encoded = json.dumps(self.server.response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def redirect_servers() -> Iterator[tuple[str, RedirectServer, RedirectServer]]:
    destination = RedirectServer(("127.0.0.1", 0), RedirectDestinationHandler)
    destination.state = {"authorizations": []}
    source = RedirectServer(("127.0.0.1", 0), RedirectSourceHandler)
    source.state = {
        "authorizations": [],
        "destination": f"http://127.0.0.1:{destination.server_port}/capture",
    }
    threads = [
        threading.Thread(target=destination.serve_forever, daemon=True),
        threading.Thread(target=source.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{source.server_port}/gate", source, destination
    finally:
        source.shutdown()
        destination.shutdown()
        source.server_close()
        destination.server_close()
        for thread in threads:
            thread.join(timeout=2)


@contextlib.contextmanager
def oversized_gate_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), OversizedGateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/gate"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def token_reason_gate_server(token: str) -> Iterator[tuple[str, TokenReasonServer]]:
    server = TokenReasonServer(("127.0.0.1", 0), TokenReasonGateHandler)
    server.token = token
    server.request_count = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/gate", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def project_metadata_server() -> Iterator[tuple[str, ProjectMetadataServer]]:
    server = ProjectMetadataServer(("127.0.0.1", 0), ProjectMetadataHandler)
    server.response = {
        "project_id": PROJECT_ID,
        "verification_instructions": verification_instructions(),
    }
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class CompletionGatePayloadTest(unittest.TestCase):
    def test_instruction_preflight_writes_and_rereads_one_atomic_external_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, project_metadata_server() as (base_url, server):
            workspace = Path(temporary)
            target = workspace / "target"
            target.mkdir()
            runtime = workspace / "external-runtime"
            config_path = runtime / "config" / "runtime-config.json"
            reader_path = runtime / "scripts" / "read_instruction_snapshot.py"
            config_path.parent.mkdir(parents=True)
            reader_path.parent.mkdir(parents=True)
            config_path.write_text("{}\n", encoding="utf-8")
            shutil.copy2(PACKAGE_ROOT / "scripts" / "read_instruction_snapshot.py", reader_path)
            token_env = f"ACCEPTORA_AGENT_TOKEN_{PROJECT_ID.upper()}"
            config = {
                "enabled": True,
                "config_source": "installer_owned_external_runtime",
                "client": "codex",
                "project_id": PROJECT_ID,
                "token_env": token_env,
                "rest_base_url": f"{base_url}/api/v1/integrations",
                "timeout_seconds": 2,
                "retry_attempts": 1,
                "python_executable": str(Path(sys.executable).resolve()),
            }
            event = {
                "cwd": str(target),
                "session_id": "instruction-preflight",
                "hook_event_name": "UserPromptSubmit",
            }

            with (
                patch.dict(os.environ, {token_env: VALID_TOKEN}, clear=False),
                patch.object(HOOK_RUNTIME, "_project_root", return_value=target),
                patch.object(HOOK_RUNTIME, "load_config", return_value=config),
                patch.object(HOOK_RUNTIME, "_config_path", return_value=config_path),
            ):
                first = HOOK_RUNTIME.prepare_verification_instructions(event, "codex")
                assert first is not None
                first_directive = HOOK_RUNTIME.instruction_additional_context(first)

                self.assertTrue(first.path.is_file())
                self.assertIn(PROJECT_ID, first.path.name)
                self.assertNotIn(VALID_TOKEN, first.path.name)
                self.assertNotIn("OWNER-ANALYSIS-GUIDANCE", first_directive)
                self.assertIn("-B", first_directive)
                self.assertIn("-I", first_directive)
                self.assertIn("get_feature_context", first_directive)
                if os.name != "nt":
                    self.assertEqual(0o600, first.path.stat().st_mode & 0o777)

                read_first = subprocess.run(
                    list(first.reader_argv),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, read_first.returncode, read_first.stdout + read_first.stderr)
                self.assertEqual(
                    "OWNER-ANALYSIS-GUIDANCE",
                    json.loads(read_first.stdout)["instructions"]["analysis_guidance"],
                )

                server.response["verification_instructions"] = verification_instructions(account_revision=5)
                second = HOOK_RUNTIME.prepare_verification_instructions(event, "codex")
                assert second is not None

                self.assertEqual(first.path, second.path)
                self.assertEqual(5, second.account_revision)
                stale_read = subprocess.run(
                    list(first.reader_argv),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(1, stale_read.returncode)
                fresh_read = subprocess.run(
                    list(second.reader_argv),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, fresh_read.returncode, fresh_read.stdout + fresh_read.stderr)

                reader_path.unlink()
                with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "reader is unavailable"):
                    HOOK_RUNTIME.prepare_verification_instructions(event, "codex")
                self.assertFalse(second.path.exists())
                shutil.copy2(PACKAGE_ROOT / "scripts" / "read_instruction_snapshot.py", reader_path)

                server.response["verification_instructions"] = {
                    **verification_instructions(account_revision=6),
                    "effective_digest": "sha256:" + ("f" * 64),
                }
                with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "effective_digest"):
                    HOOK_RUNTIME.prepare_verification_instructions(event, "codex")
                self.assertFalse(second.path.exists())

            self.assertEqual(3, len(server.requests))
            self.assertTrue(
                all(request["path"] == "/api/v1/integrations/project" for request in server.requests)
            )
            self.assertTrue(
                all(request["authorization"] == f"Bearer {VALID_TOKEN}" for request in server.requests)
            )
            bytecode = [
                path for path in runtime.rglob("*")
                if path.name == "__pycache__" or path.suffix.lower() in {".pyc", ".pyo"}
            ]
            self.assertEqual([], bytecode)

    def test_instruction_project_metadata_redirect_is_not_followed_with_authorization(self) -> None:
        token_env = f"ACCEPTORA_AGENT_TOKEN_{PROJECT_ID.upper()}"
        with redirect_servers() as (_, source, destination), patch.dict(
            os.environ,
            {token_env: VALID_TOKEN},
            clear=False,
        ):
            config = {
                "token_env": token_env,
                "rest_base_url": f"http://127.0.0.1:{source.server_port}/api/v1/integrations",
                "timeout_seconds": 2,
                "retry_attempts": 1,
            }

            with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "HTTP 302"):
                HOOK_RUNTIME._fetch_project_metadata(config)

        self.assertEqual([f"Bearer {VALID_TOKEN}"], source.state["authorizations"])
        self.assertEqual([], destination.state["authorizations"])

    def test_source_paths_preserve_leading_dots_and_reject_unsafe_segments(self) -> None:
        self.assertEqual(
            ".github/workflows/verify.yml",
            SOURCE_MANIFEST._normalise_relative("./.github/workflows/verify.yml"),
        )
        for path in ("../escape", "nested/../escape", "/absolute", "C:/absolute"):
            with self.subTest(path=path), self.assertRaises(SOURCE_MANIFEST.ManifestError):
                SOURCE_MANIFEST._normalise_relative(path)

    def test_windows_local_repository_locators_use_forward_slashes(self) -> None:
        cases = {
            r"C:\laragon\www\example": "C:/laragon/www/example",
            r"c:\laragon\www\example": "C:/laragon/www/example",
            "D:/work/example": "D:/work/example",
            r"\\server\share\example": "//server/share/example",
            "//server/share/example": "//server/share/example",
        }

        for locator, expected in cases.items():
            with self.subTest(locator=locator):
                self.assertEqual(expected, SOURCE_MANIFEST.canonicalize_repository_locator(locator))

    def test_repository_locator_canonicalization_preserves_urls_scp_and_nonabsolute_paths(self) -> None:
        self.assertEqual(
            "https://example.test:8443/org/repository.git",
            SOURCE_MANIFEST.canonicalize_repository_locator(
                "https://owner:secret@example.test:8443/org/repository.git?token=secret#fragment"
            ),
        )
        self.assertEqual(
            r"git@example.test:org\repository.git",
            SOURCE_MANIFEST.canonicalize_repository_locator(r"git@example.test:org\repository.git"),
        )

        for locator in (r"C:relative\repository", r"relative\repository", r"\server\share"):
            with self.subTest(locator=locator):
                self.assertEqual(locator, SOURCE_MANIFEST.canonicalize_repository_locator(locator))

    def test_git_capture_canonicalizes_windows_local_remote_without_digest_drift(self) -> None:
        for raw_locator, canonical_locator in (
            (r"C:\laragon\www\example", "C:/laragon/www/example"),
            (r"c:\laragon\www\example", "C:/laragon/www/example"),
            (r"\\server\share\example", "//server/share/example"),
        ):
            with self.subTest(locator=raw_locator), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "README.md").write_text("stable\n", encoding="utf-8")
                initialize_git(root)
                run_git(root, "add", ".")
                run_git(root, "commit", "-m", "baseline")
                run_git(root, "remote", "add", "origin", raw_locator)

                baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
                run_git(root, "remote", "set-url", "origin", canonical_locator)
                current = SOURCE_MANIFEST.capture_snapshot(root, "git")
                comparison = SOURCE_MANIFEST.compare_with_baseline(baseline, root)

                self.assertEqual(canonical_locator, baseline["repository"])
                self.assertEqual(canonical_locator, current["repository"])
                self.assertEqual(baseline["source_digest"], current["source_digest"])
                self.assertEqual([], comparison["entries"])
                self.assertEqual(
                    SOURCE_MANIFEST.digest_value(
                        {
                            "repository": canonical_locator,
                            "base_source_digest": baseline["source_digest"],
                            "current_source_digest": current["source_digest"],
                            "entries": [],
                        }
                    ),
                    comparison["changed_surface_digest"],
                )

    def test_git_dotfile_changes_preserve_anchor_and_bind_exact_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "verify.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_bytes(b"name: baseline\n")
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Manifest Test",
                "GIT_AUTHOR_EMAIL": "manifest@example.test",
                "GIT_COMMITTER_NAME": "Manifest Test",
                "GIT_COMMITTER_EMAIL": "manifest@example.test",
            }
            for arguments in (("init",), ("add", "."), ("commit", "-m", "baseline")):
                completed = subprocess.run(
                    ["git", "-C", str(root), *arguments],
                    capture_output=True,
                    text=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            workflow.write_bytes(b"name: first\n")
            first = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            workflow.write_bytes(b"name: second\n")
            second = SOURCE_MANIFEST.compare_with_baseline(baseline, root)

            self.assertEqual(["file:.github/workflows/verify.yml"], [entry["anchor"] for entry in first["entries"]])
            self.assertEqual(".github/workflows/verify.yml", first["entries"][0]["path"])
            self.assertEqual(hashlib.sha256(b"name: first\n").hexdigest(), first["entries"][0]["current_sha256"])
            self.assertEqual(hashlib.sha256(b"name: second\n").hexdigest(), second["entries"][0]["current_sha256"])
            self.assertNotEqual(first["current"]["source_digest"], second["current"]["source_digest"])
            self.assertNotEqual(first["changed_surface_digest"], second["changed_surface_digest"])

    def test_strict_git_captures_polyglot_sources_and_nonignored_framework_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracked_sources = (
                "python/main.py",
                "typescript/app.ts",
                "go/main.go",
                "rust/lib.rs",
                "dotnet/Program.cs",
                "java/Main.java",
                "ruby/task.rb",
                "assets/module.wasm",
                "Makefile",
            )
            for relative in tracked_sources:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\x00baseline:" + relative.encode("utf-8") + b"\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            self.assertTrue(set(tracked_sources) <= {entry["path"] for entry in baseline["entries"]})

            for relative in tracked_sources:
                (root / relative).write_bytes(b"\x00changed:" + relative.encode("utf-8") + b"\n")
            newly_created_sources = (
                "build/generated.custom",
                "dist/bundle.custom",
                "vendor/local/source.custom",
                "node_modules/local/source.custom",
                "storage/framework/custom/source.custom",
                "bootstrap/cache/source.custom",
                "public/build/source.custom",
            )
            for relative in newly_created_sources:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"source:{relative}\n", encoding="utf-8")

            comparison = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            self.assertEqual(
                set(tracked_sources) | set(newly_created_sources),
                {entry["path"] for entry in comparison["entries"]},
            )

    def test_strict_git_uses_repository_and_explicit_ignores_instead_of_stack_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gitignore = root / ".gitignore"
            gitignore.write_text(".gradle/\n.dart_tool/\n.venv/\nDerivedData/\nPods/\ntarget/\n", encoding="utf-8")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")

            git_ignored = (
                ".gradle/cache/state",
                ".dart_tool/cache/state",
                ".venv/lib/module.py",
                "DerivedData/output/app",
                "Pods/Library/source.m",
                "target/debug/app",
            )
            for relative in git_ignored:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ignored by repository\n", encoding="utf-8")

            real_scandir = os.scandir
            ignored_roots = {Path(relative).parts[0] for relative in git_ignored}
            scanned_directories = []

            def guarded_scandir(directory: str | os.PathLike[str]):
                relative = Path(directory).resolve().relative_to(root.resolve())
                if relative.parts and relative.parts[0] in ignored_roots:
                    raise AssertionError(f"Git ignored tree scanned: {relative}")
                scanned_directories.append(relative.as_posix())
                return real_scandir(directory)

            with patch.object(SOURCE_MANIFEST.os, "scandir", side_effect=guarded_scandir):
                comparison = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            self.assertEqual([], comparison["entries"])
            self.assertIn(".", scanned_directories)

            explicit_paths = ("build/generated.js", "cache/generated.bin")
            for relative in explicit_paths:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("explicitly ignored\n", encoding="utf-8")

            included = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            excluded = SOURCE_MANIFEST.compare_with_baseline(
                baseline,
                root,
                extra_ignores=("build/**", "cache/**"),
            )
            self.assertEqual(set(explicit_paths), {entry["path"] for entry in included["entries"]})
            self.assertEqual([], excluded["entries"])
            self.assertEqual((".git/**", ".verification/**"), SOURCE_MANIFEST.DEFAULT_IGNORES)

    def test_strict_git_traverses_directories_with_reincluded_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitignore").write_text(
                "foo/**\n!foo/bar/\n!foo/bar/keep.txt\n",
                encoding="utf-8",
            )
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            keep = root / "foo" / "bar" / "keep.txt"
            keep.parent.mkdir(parents=True)
            keep.write_text("included\n", encoding="utf-8")
            real_scandir = os.scandir
            scanned_directories = []

            def recording_scandir(directory: str | os.PathLike[str]):
                relative = Path(directory).resolve().relative_to(root.resolve())
                scanned_directories.append(relative.as_posix())
                return real_scandir(directory)

            with patch.object(SOURCE_MANIFEST.os, "scandir", side_effect=recording_scandir):
                snapshot = SOURCE_MANIFEST.capture_snapshot(root, "git")

            self.assertIn("foo/bar", scanned_directories)
            self.assertEqual([".gitignore", "foo/bar/keep.txt"], [entry["path"] for entry in snapshot["entries"]])

    def test_staged_rename_and_copy_have_distinct_content_bound_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.txt"
            new = root / "new.txt"
            old.write_bytes(b"same content\n")
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Manifest Test",
                "GIT_AUTHOR_EMAIL": "manifest@example.test",
                "GIT_COMMITTER_NAME": "Manifest Test",
                "GIT_COMMITTER_EMAIL": "manifest@example.test",
            }

            def git(*arguments: str) -> None:
                completed = subprocess.run(
                    ["git", "-C", str(root), *arguments],
                    capture_output=True,
                    text=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

            git("init")
            git("config", "core.autocrlf", "false")
            git("add", ".")
            git("commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")

            old.rename(new)
            git("add", "-A")
            renamed = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            self.assertEqual(
                [
                    ("file:new.txt", "added"),
                    ("file:old.txt", "deleted"),
                ],
                [(entry["anchor"], entry["change"]) for entry in renamed["entries"]],
            )

            git("reset", "--hard", "HEAD")
            new.write_bytes(old.read_bytes())
            git("add", "new.txt")
            copied = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            self.assertEqual([("file:new.txt", "added")], [(entry["anchor"], entry["change"]) for entry in copied["entries"]])
            self.assertNotEqual(renamed["current"]["source_digest"], copied["current"]["source_digest"])
            self.assertNotEqual(renamed["changed_surface_digest"], copied["changed_surface_digest"])

    def test_full_git_capture_binds_same_size_bytes_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.txt"
            source.write_bytes(b"AAAA\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            original_stat = source.stat()

            source.write_bytes(b"BBBB\n")
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            changed = SOURCE_MANIFEST.compare_with_baseline(baseline, root)

            self.assertEqual([("file:a.txt", "modified")], [(entry["anchor"], entry["change"]) for entry in changed["entries"]])
            self.assertEqual(hashlib.sha256(b"BBBB\n").hexdigest(), changed["entries"][0]["current_sha256"])
            self.assertNotEqual(changed["base"]["source_digest"], changed["current"]["source_digest"])

    def test_file_mutation_during_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repository"
            root.mkdir()
            source = root / "source.txt"
            source.write_bytes(b"AAAA\n")
            original_stat = source.stat()
            real_sha256 = hashlib.sha256

            class MutatingDigest:
                def __init__(self) -> None:
                    self.delegate = real_sha256()
                    self.mutated = False

                def update(self, body: bytes) -> None:
                    if not self.mutated:
                        source.write_bytes(b"BBBB\n")
                        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                        self.mutated = True
                    self.delegate.update(body)

                def hexdigest(self) -> str:
                    return self.delegate.hexdigest()

            with patch.object(SOURCE_MANIFEST.hashlib, "sha256", side_effect=MutatingDigest), self.assertRaisesRegex(
                SOURCE_MANIFEST.ManifestError, "changed during capture"
            ):
                SOURCE_MANIFEST._hash_file(source, root)

    def test_index_object_id_binds_staged_content_when_worktree_bytes_match_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.txt"
            source.write_bytes(b"AAAA\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")

            source.write_bytes(b"BBBB\n")
            run_git(root, "add", "a.txt")
            source.write_bytes(b"AAAA\n")
            changed = SOURCE_MANIFEST.compare_with_baseline(baseline, root)

            self.assertEqual([("file:a.txt", "modified")], [(entry["anchor"], entry["change"]) for entry in changed["entries"]])
            self.assertEqual(hashlib.sha256(b"AAAA\n").hexdigest(), changed["entries"][0]["current_sha256"])
            self.assertNotEqual(changed["base"]["source_digest"], changed["current"]["source_digest"])

    def test_git_capture_ignores_poisoned_environment_and_disables_fsmonitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "actual"
            evil = workspace / "evil"
            root.mkdir()
            evil.mkdir()
            (root / "actual.txt").write_bytes(b"actual\n")
            (evil / "evil.txt").write_bytes(b"evil\n")
            for repository in (root, evil):
                initialize_git(repository)
                run_git(repository, "add", ".")
                run_git(repository, "commit", "-m", "baseline")

            marker = workspace / "fsmonitor-ran"
            monitor = workspace / "monitor.py"
            monitor.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            run_git(root, "config", "core.fsmonitor", f'"{Path(sys.executable).resolve()}" "{monitor}"')
            poison = {
                "GIT_DIR": str(evil / ".git"),
                "GIT_WORK_TREE": str(evil),
                "GIT_INDEX_FILE": str(evil / ".git" / "index"),
                "GIT_CONFIG_GLOBAL": str(workspace / "attacker.gitconfig"),
            }
            with patch.dict(os.environ, poison, clear=False):
                snapshot = SOURCE_MANIFEST.capture_snapshot(root, "git")

            self.assertEqual(["actual.txt"], [entry["path"] for entry in snapshot["entries"]])
            self.assertFalse(marker.exists())

    def test_assume_unchanged_and_skip_worktree_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.txt"
            source.write_bytes(b"AAAA\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")

            for enable, disable in (
                ("--assume-unchanged", "--no-assume-unchanged"),
                ("--skip-worktree", "--no-skip-worktree"),
            ):
                with self.subTest(flag=enable):
                    run_git(root, "update-index", enable, "a.txt")
                    source.write_bytes(b"BBBB\n")
                    with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "assume-unchanged or skip-worktree"):
                        SOURCE_MANIFEST.compare_with_baseline(baseline, root)
                    run_git(root, "update-index", disable, "a.txt")
                    run_git(root, "reset", "--hard", "HEAD")

    def test_corrupt_index_fails_closed_instead_of_returning_an_empty_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_bytes(b"AAAA\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            index = Path(run_git(root, "rev-parse", "--git-path", "index"))
            if not index.is_absolute():
                index = root / index
            index.write_bytes(b"broken-index")

            with self.assertRaises(SOURCE_MANIFEST.ManifestError):
                SOURCE_MANIFEST.compare_with_baseline(baseline, root)

    def test_gitlinks_fail_closed_for_distinct_staged_values_and_legacy_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_bytes(b"baseline\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")

            for object_id in ("1" * 40, "2" * 40):
                with self.subTest(object_id=object_id):
                    run_git(root, "update-index", "--add", "--cacheinfo", f"160000,{object_id},deps/sub")
                    with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "does not support Git submodules"):
                        SOURCE_MANIFEST.compare_with_baseline(baseline, root)
                    run_git(root, "reset", "--hard", "HEAD")

            legacy_baseline = json.loads(json.dumps(baseline))
            legacy_baseline["entries"].append(
                {
                    "path": "deps/sub",
                    "exists": True,
                    "sha256": None,
                    "size": 0,
                    "kind": "gitlink",
                    "git_mode": "160000",
                    "index_mode": "160000",
                    "index_object_id": "3" * 40,
                }
            )
            legacy_baseline["source_digest"] = SOURCE_MANIFEST._snapshot_digest(legacy_baseline)
            with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "does not support Git submodules"):
                SOURCE_MANIFEST.compare_with_baseline(legacy_baseline, root)

    def test_full_snapshot_detects_changes_even_when_baseline_commit_object_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.txt"
            old.write_bytes(b"old\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            baseline["head"] = "f" * 40
            baseline["source_digest"] = SOURCE_MANIFEST._snapshot_digest(baseline)

            old.unlink()
            (root / "new.txt").write_bytes(b"new\n")
            run_git(root, "add", "-A")
            changed = SOURCE_MANIFEST.compare_with_baseline(baseline, root)

            self.assertEqual(
                [("file:new.txt", "added"), ("file:old.txt", "deleted")],
                [(entry["anchor"], entry["change"]) for entry in changed["entries"]],
            )

    @unittest.skipIf(os.name == "nt", "Windows path separators cannot be literal filename characters")
    def test_literal_backslash_git_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a\\b.txt").write_bytes(b"content\n")
            initialize_git(root)
            run_git(root, "add", ".")
            with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "unsafe or non-portable"):
                SOURCE_MANIFEST.capture_snapshot(root, "git")

    @unittest.skipIf(os.name == "nt", "POSIX special-file regression")
    def test_special_files_fail_before_blocking_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fifo = root / "source.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "not a regular file or symlink"):
                SOURCE_MANIFEST.capture_snapshot(root, "filesystem")

    @unittest.skipIf(os.name == "nt", "POSIX special-file regression")
    def test_strict_git_rejects_special_files_omitted_by_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_git(root)
            os.mkfifo(root / "source.fifo")

            with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "not a regular file or symlink"):
                SOURCE_MANIFEST.capture_snapshot(root, "git")

    @unittest.skipIf(os.name == "nt", "POSIX special-file regression")
    def test_strict_git_allows_git_ignored_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitignore").write_text("*.fifo\nignored-specials/\n", encoding="utf-8")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            os.mkfifo(root / "root.fifo")
            ignored_directory = root / "ignored-specials"
            ignored_directory.mkdir()
            os.mkfifo(ignored_directory / "nested.fifo")

            snapshot = SOURCE_MANIFEST.capture_snapshot(root, "git")

            self.assertEqual([".gitignore"], [entry["path"] for entry in snapshot["entries"]])

    @unittest.skipIf(os.name == "nt", "POSIX special-file regression")
    def test_strict_git_rejects_special_files_reincluded_by_negated_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitignore").write_text(
                "foo/**\n!foo/bar/\n!foo/bar/keep.txt\n",
                encoding="utf-8",
            )
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            fifo = root / "foo" / "bar" / "keep.txt"
            fifo.parent.mkdir(parents=True)
            os.mkfifo(fifo)

            with self.assertRaisesRegex(SOURCE_MANIFEST.ManifestError, "not a regular file or symlink"):
                SOURCE_MANIFEST.capture_snapshot(root, "git")

    def test_strict_git_fails_closed_when_ignore_query_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_git(root)
            (root / "empty-directory").mkdir()
            real_run_git = SOURCE_MANIFEST._run_git

            def fail_ignore_query(repository: Path, *arguments: str, **options: object):
                if arguments and arguments[0] == "check-ignore":
                    return subprocess.CompletedProcess(arguments, 128, b"", b"fatal: ignore failure")
                return real_run_git(repository, *arguments, **options)

            with patch.object(SOURCE_MANIFEST, "_run_git", side_effect=fail_ignore_query), self.assertRaisesRegex(
                SOURCE_MANIFEST.ManifestError, "Git ignore query failed"
            ):
                SOURCE_MANIFEST.capture_snapshot(root, "git")

    def test_runtime_versions_are_loaded_from_the_package_manifest(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8"))
        client_registry = json.loads(
            (PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["skill"]["version"], HOOK_RUNTIME.SKILL_VERSION)
        self.assertEqual(manifest["integration"]["version"], HOOK_RUNTIME.INTEGRATION_VERSION)
        self.assertEqual(manifest["contract"]["version"], HOOK_RUNTIME.CONTRACT_VERSION)
        self.assertEqual(
            [profile["id"] for profile in client_registry["clients"]],
            list(HOOK_RUNTIME.CLIENT_PROFILES),
        )

    def test_maps_deterministic_manifest_to_exact_v1_gate_shape(self) -> None:
        base_digest = "a" * 64
        current_digest = "b" * 64
        content_digest = "c" * 64
        manifest = {
            "schema_version": "1.0",
            "adapter": "git-v1",
            "guarantee": "strict",
            "repository": "example/sdk-validation",
            "base": {
                "adapter": "git-v1",
                "guarantee": "strict",
                "repository": "example/sdk-validation",
                "head": "base-revision",
                "branch": "main",
                "source_digest": base_digest,
            },
            "current": {
                "adapter": "git-v1",
                "guarantee": "strict",
                "repository": "example/sdk-validation",
                "head": "current-revision",
                "branch": "main",
                "source_digest": current_digest,
            },
            "entries": [
                {
                    "anchor": "file:app/Example.php",
                    "path": "app/Example.php",
                    "change": "modified",
                    "base_sha256": "d" * 64,
                    "current_sha256": content_digest,
                    "current_size": 123,
                }
            ],
            "changed_surface_digest": "e" * 64,
        }

        payload = HOOK_RUNTIME.build_completion_gate_payload(
            {
                "project_id": "proj_01J00000000000000000000001",
                "feature_id": None,
            },
            manifest,
            {"session_id": "session-1", "turn_id": "turn-1"},
            "codex",
        )

        fixture = json.loads(
            (PACKAGE_ROOT / "tests" / "fixtures" / "hook-gate-payload.json").read_text(encoding="utf-8")
        )
        fixture["versions"]["skill_version"] = "1.2.3"

        self.assertEqual(fixture, payload)
        self.assertEqual(
            {
                "project_id",
                "source_identity",
                "adapter_kind",
                "adapter_version",
                "baseline_source_descriptor",
                "baseline_source_digest",
                "current_source_descriptor",
                "current_source_digest",
                "source_manifest",
                "task_session_correlation_id",
                "feature_id",
                "versions",
            },
            set(payload),
        )
        self.assertEqual("git:example/sdk-validation", payload["source_identity"])
        self.assertEqual(f"sha256:{base_digest}", payload["baseline_source_digest"])
        self.assertEqual(f"sha256:{current_digest}", payload["current_source_digest"])
        self.assertEqual("git-v1", payload["adapter_kind"])
        self.assertEqual("1.0.1", payload["adapter_version"])
        self.assertEqual("base-revision", payload["current_source_descriptor"]["base_revision"])
        self.assertEqual("modified", payload["source_manifest"]["entries"][0]["change_kind"])
        self.assertEqual(f"sha256:{content_digest}", payload["source_manifest"]["entries"][0]["content_digest"])
        self.assertEqual("1.0.0", payload["versions"]["contract_version"])
        self.assertNotIn("changed_surface_manifest", payload)
        self.assertNotIn("explicit_feature_id", payload)

    def test_gate_mapping_canonicalizes_equivalent_windows_repository_locators(self) -> None:
        manifest = {
            "repository": r"C:\laragon\www\example",
            "base": {
                "adapter": "git-v1",
                "repository": r"C:\laragon\www\example",
                "head": "base-revision",
                "source_digest": "a" * 64,
            },
            "current": {
                "adapter": "git-v1",
                "repository": "C:/laragon/www/example",
                "head": "current-revision",
                "source_digest": "b" * 64,
            },
            "entries": [],
        }

        payload = HOOK_RUNTIME.build_completion_gate_payload(
            {"project_id": PROJECT_ID},
            manifest,
            {"session_id": "windows-locator"},
            "codex",
        )

        self.assertEqual("git:C:/laragon/www/example", payload["source_identity"])
        self.assertEqual(
            "C:/laragon/www/example",
            payload["baseline_source_descriptor"]["source_locator"],
        )
        self.assertEqual(
            "C:/laragon/www/example",
            payload["current_source_descriptor"]["source_locator"],
        )
        self.assertEqual("1.0.1", payload["adapter_version"])

    def test_gate_mapping_rejects_distinct_repository_locators(self) -> None:
        manifest = {
            "repository": r"C:\laragon\www\example",
            "base": {
                "adapter": "git-v1",
                "repository": r"C:\laragon\www\example",
                "head": "base-revision",
                "source_digest": "a" * 64,
            },
            "current": {
                "adapter": "git-v1",
                "repository": r"D:\laragon\www\example",
                "head": "current-revision",
                "source_digest": "b" * 64,
            },
            "entries": [],
        }

        with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "same repository"):
            HOOK_RUNTIME.build_completion_gate_payload(
                {"project_id": PROJECT_ID},
                manifest,
                {"session_id": "windows-locator-mismatch"},
                "codex",
            )

        manifest["current"]["repository"] = "C:/laragon/www/example"
        manifest["repository"] = r"D:\laragon\www\example"
        with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "manifest repository"):
            HOOK_RUNTIME.build_completion_gate_payload(
                {"project_id": PROJECT_ID},
                manifest,
                {"session_id": "windows-manifest-locator-mismatch"},
                "codex",
            )

    def test_gate_mapping_requires_the_authoritative_manifest_repository(self) -> None:
        manifest = {
            "base": {
                "adapter": "git-v1",
                "repository": "example/sdk-validation",
                "head": "base-revision",
                "source_digest": "a" * 64,
            },
            "current": {
                "adapter": "git-v1",
                "repository": "example/sdk-validation",
                "head": "current-revision",
                "source_digest": "b" * 64,
            },
            "entries": [],
        }

        for repository in (None, "", "   "):
            candidate = dict(manifest)
            if repository is not None:
                candidate["repository"] = repository
            with self.subTest(repository=repository), self.assertRaisesRegex(
                HOOK_RUNTIME.HookRuntimeError,
                "manifest repository is missing or empty",
            ):
                HOOK_RUNTIME.build_completion_gate_payload(
                    {"project_id": PROJECT_ID},
                    candidate,
                    {"session_id": "missing-manifest-locator"},
                    "codex",
                )

    def test_rejects_an_invalid_adapter_digest_before_network_io(self) -> None:
        manifest = {
            "repository": "example/sdk-validation",
            "base": {
                "adapter": "git-v1",
                "repository": "example/sdk-validation",
                "head": "base",
                "source_digest": "not-a-digest",
            },
            "current": {
                "adapter": "git-v1",
                "repository": "example/sdk-validation",
                "head": "current",
                "source_digest": "b" * 64,
            },
            "entries": [],
        }

        with self.assertRaises(HOOK_RUNTIME.HookRuntimeError):
            HOOK_RUNTIME.build_completion_gate_payload(
                {"project_id": "proj_01J00000000000000000000001"},
                manifest,
                {"session_id": "session-1"},
                "claude-code",
            )

    def test_completion_gate_endpoint_rejects_unsafe_url_shapes(self) -> None:
        invalid = [
            "http://acceptora.example/api/integrations/completion-gate",
            "https://user:password@acceptora.example/api/integrations/completion-gate",
            "https://acceptora.example/api/integrations/completion-gate?token=value",
            "https://acceptora.example/api/integrations/completion-gate#fragment",
        ]

        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(HOOK_RUNTIME.HookRuntimeError):
                HOOK_RUNTIME._validate_endpoint(endpoint, "completion_gate_url")

        self.assertEqual(
            "http://127.0.0.1:8000/api/integrations/completion-gate",
            HOOK_RUNTIME._validate_endpoint(
                "http://127.0.0.1:8000/api/integrations/completion-gate",
                "completion_gate_url",
            ),
        )

    def test_completion_gate_redirect_is_not_followed_with_authorization(self) -> None:
        token = VALID_TOKEN

        with redirect_servers() as (source_url, source, destination), patch.dict(
            os.environ,
            {"ACCEPTORA_REDIRECT_TEST_TOKEN": token},
        ):
            with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "HTTP 302"):
                HOOK_RUNTIME._post_gate(
                    {
                        "completion_gate_url": source_url,
                        "token_env": "ACCEPTORA_REDIRECT_TEST_TOKEN",
                        "retry_attempts": 1,
                    },
                    {"probe": True},
                )

        self.assertEqual([f"Bearer {token}"], source.state["authorizations"])
        self.assertEqual([], destination.state["authorizations"])

    def test_oversized_completion_response_is_bounded_and_blocks_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, oversized_gate_server() as gate_url:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            baseline_path = state / "baseline.json"
            loop_path = state / "loop.json"
            pending_path = state / "pending.json"
            baseline_path.write_text(json.dumps({"snapshot": {"adapter": "git-v1"}}), encoding="utf-8")
            config = {
                "completion_gate_url": gate_url,
                "token_env": "ACCEPTORA_AGENT_TOKEN",
                "retry_attempts": 1,
                "timeout_seconds": 3,
                "max_stop_blocks": 2,
                "offline_outbox": ".verification/outbox",
            }
            event = {"cwd": str(root), "session_id": "oversized-response"}
            manifest = {"entries": [{"path": "a.txt"}], "changed_surface_digest": "a" * 64}
            with patch.dict(os.environ, {"ACCEPTORA_AGENT_TOKEN": VALID_TOKEN}, clear=False), patch.object(
                HOOK_RUNTIME, "_project_root", return_value=root
            ), patch.object(HOOK_RUNTIME, "load_config", return_value=config), patch.object(
                HOOK_RUNTIME, "_state_paths", return_value=(baseline_path, loop_path)
            ), patch.object(HOOK_RUNTIME, "_pending_path", return_value=pending_path), patch.object(
                HOOK_RUNTIME, "compare_with_baseline", return_value=manifest
            ), patch.object(HOOK_RUNTIME, "build_completion_gate_payload", return_value={}):
                decision = HOOK_RUNTIME.evaluate_completion_gate(event, "codex")

            self.assertTrue(decision.block)
            self.assertEqual("unavailable", decision.outcome)
            self.assertIn("response exceeds the 4 MiB limit", decision.message or "")

    def test_token_valued_http_reason_never_reaches_gate_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, token_reason_gate_server(VALID_TOKEN) as (gate_url, server):
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            baseline_path = state / "baseline.json"
            loop_path = state / "loop.json"
            pending_path = state / "pending.json"
            baseline_path.write_text(json.dumps({"snapshot": {"adapter": "git-v1"}}), encoding="utf-8")
            config = {
                "completion_gate_url": gate_url,
                "token_env": "ACCEPTORA_AGENT_TOKEN",
                "retry_attempts": 1,
                "timeout_seconds": 3,
                "max_stop_blocks": 2,
                "offline_outbox": ".verification/outbox",
            }
            event = {"cwd": str(root), "session_id": "token-reason"}
            manifest = {"entries": [{"path": "a.txt"}], "changed_surface_digest": "a" * 64}
            with patch.dict(os.environ, {"ACCEPTORA_AGENT_TOKEN": VALID_TOKEN}, clear=False), patch.object(
                HOOK_RUNTIME, "_project_root", return_value=root
            ), patch.object(HOOK_RUNTIME, "load_config", return_value=config), patch.object(
                HOOK_RUNTIME, "_state_paths", return_value=(baseline_path, loop_path)
            ), patch.object(HOOK_RUNTIME, "_pending_path", return_value=pending_path), patch.object(
                HOOK_RUNTIME, "compare_with_baseline", return_value=manifest
            ), patch.object(HOOK_RUNTIME, "build_completion_gate_payload", return_value={}):
                decision = HOOK_RUNTIME.evaluate_completion_gate(event, "codex")

            self.assertEqual(1, server.request_count)
            self.assertTrue(decision.block)
            self.assertEqual("unavailable", decision.outcome)
            self.assertIn("HTTP 503", decision.message or "")
            self.assertNotIn(VALID_TOKEN, decision.message or "")

    def test_malformed_gate_token_is_rejected_before_header_or_network_use(self) -> None:
        malformed_token = f"{VALID_TOKEN}\r\nX-Injected: value"
        with token_reason_gate_server(VALID_TOKEN) as (gate_url, server), patch.dict(
            os.environ,
            {"ACCEPTORA_MALFORMED_GATE_TOKEN": malformed_token},
            clear=False,
        ):
            with self.assertRaisesRegex(HOOK_RUNTIME.HookRuntimeError, "missing or malformed") as raised:
                HOOK_RUNTIME._post_gate(
                    {
                        "completion_gate_url": gate_url,
                        "token_env": "ACCEPTORA_MALFORMED_GATE_TOKEN",
                        "retry_attempts": 1,
                    },
                    {},
                )

        self.assertEqual(0, server.request_count)
        self.assertNotIn(malformed_token, str(raised.exception))

    def test_source_change_during_gate_response_is_recaptured_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repository"
            root.mkdir()
            source = root / "source.txt"
            source.write_bytes(b"baseline\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            state = workspace / "state"
            state.mkdir()
            baseline_path = state / "baseline.json"
            loop_path = state / "loop.json"
            pending_path = state / "pending.json"
            baseline_path.write_text(json.dumps({"snapshot": baseline}), encoding="utf-8")
            source.write_bytes(b"before request\n")
            config = {
                "project_id": "proj_01J00000000000000000000001",
                "feature_id": FEATURE_ID,
                "max_stop_blocks": 2,
                "offline_outbox": ".verification/outbox",
                "ignored_paths": [],
            }
            event = {"cwd": str(root), "session_id": "gate-race"}

            def pass_after_mutation(_config: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
                source.write_bytes(b"during request\n")
                return gate_response(payload)

            with patch.object(HOOK_RUNTIME, "_project_root", return_value=root), patch.object(
                HOOK_RUNTIME, "load_config", return_value=config
            ), patch.object(HOOK_RUNTIME, "_state_paths", return_value=(baseline_path, loop_path)), patch.object(
                HOOK_RUNTIME, "_pending_path", return_value=pending_path
            ), patch.object(HOOK_RUNTIME, "_post_gate", side_effect=pass_after_mutation):
                decision = HOOK_RUNTIME.evaluate_completion_gate(event, "codex")

            self.assertTrue(decision.block)
            self.assertEqual("unavailable", decision.outcome)
            self.assertIn("source changed during the completion gate request", decision.message or "")
            self.assertTrue(baseline_path.exists())

    def test_gate_success_identity_mismatches_never_cleanup_normal_hook_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repository"
            root.mkdir()
            source = root / "source.txt"
            source.write_bytes(b"baseline\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            state = workspace / "state"
            state.mkdir()
            baseline_path = state / "baseline.json"
            loop_path = state / "loop.json"
            pending_path = state / "pending.json"
            baseline_path.write_text(json.dumps({"snapshot": baseline}), encoding="utf-8")
            source.write_bytes(b"changed\n")
            config = {
                "project_id": "proj_01J00000000000000000000001",
                "feature_id": FEATURE_ID,
                "max_stop_blocks": 5,
                "offline_outbox": ".verification/outbox",
                "ignored_paths": [],
            }
            event = {"cwd": str(root), "session_id": "gate-identity"}
            overrides = {
                "feature": {"feature_id": "feat_01J00000000000000000000002"},
                "digest": {"last_synchronized_digest": "sha256:" + ("d" * 64)},
                "revision": {"last_synchronized_checklist_revision": None},
            }

            with patch.object(HOOK_RUNTIME, "_project_root", return_value=root), patch.object(
                HOOK_RUNTIME, "load_config", return_value=config
            ), patch.object(HOOK_RUNTIME, "_state_paths", return_value=(baseline_path, loop_path)), patch.object(
                HOOK_RUNTIME, "_pending_path", return_value=pending_path
            ):
                for label, changed_fields in overrides.items():
                    with self.subTest(label=label), patch.object(
                        HOOK_RUNTIME,
                        "_post_gate",
                        side_effect=lambda _config, payload, values=changed_fields: gate_response(payload, **values),
                    ):
                        decision = HOOK_RUNTIME.evaluate_completion_gate(event, "codex")

                    self.assertTrue(decision.block)
                    self.assertEqual("unavailable", decision.outcome)
                    self.assertTrue(baseline_path.exists())
                    self.assertTrue(pending_path.exists())

    def test_gate_output_redacts_bearer_and_token_shaped_values_before_hook_message(self) -> None:
        configured_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("T" * 48)
        foreign_token = "ghp_" + ("G" * 36)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repository"
            root.mkdir()
            source = root / "source.txt"
            source.write_bytes(b"baseline\n")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            state = workspace / "state"
            state.mkdir()
            baseline_path = state / "baseline.json"
            loop_path = state / "loop.json"
            pending_path = state / "pending.json"
            baseline_path.write_text(json.dumps({"snapshot": baseline}), encoding="utf-8")
            source.write_bytes(b"changed\n")
            config = {
                "project_id": "proj_01J00000000000000000000001",
                "feature_id": FEATURE_ID,
                "token_env": "ACCEPTORA_GATE_OUTPUT_TEST_TOKEN",
                "max_stop_blocks": 2,
                "offline_outbox": ".verification/outbox",
                "ignored_paths": [],
            }
            event = {"cwd": str(root), "session_id": "gate-redaction"}

            def malicious_response(_config: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
                return gate_response(
                    payload,
                    outcome="continue_sync",
                    reason_code="CONTINUE_SYNC",
                    reason=f"Endpoint echoed {configured_token}",
                    recovery_instruction=f"Use {foreign_token}",
                    correlation_id=foreign_token,
                )

            with patch.dict(
                os.environ, {"ACCEPTORA_GATE_OUTPUT_TEST_TOKEN": configured_token}, clear=False
            ), patch.object(HOOK_RUNTIME, "_project_root", return_value=root), patch.object(
                HOOK_RUNTIME, "load_config", return_value=config
            ), patch.object(HOOK_RUNTIME, "_state_paths", return_value=(baseline_path, loop_path)), patch.object(
                HOOK_RUNTIME, "_pending_path", return_value=pending_path
            ), patch.object(HOOK_RUNTIME, "_post_gate", side_effect=malicious_response):
                decision = HOOK_RUNTIME.evaluate_completion_gate(event, "codex")

            self.assertTrue(decision.block)
            self.assertEqual("continue_sync", decision.outcome)
            self.assertNotIn(configured_token, decision.message or "")
            self.assertNotIn(foreign_token, decision.message or "")
            self.assertIn("[REDACTED]", decision.message or "")

            payload = HOOK_RUNTIME.build_completion_gate_payload(
                config,
                SOURCE_MANIFEST.compare_with_baseline(baseline, root),
                event,
                "codex",
            )
            sanitized = HOOK_RUNTIME.validate_gate_response(
                malicious_response(config, payload),
                payload,
                token=configured_token,
                expected_feature_id=FEATURE_ID,
            )
            self.assertEqual("redacted", sanitized["correlation_id"])
            self.assertNotIn(foreign_token, sanitized["recovery_instruction"] or "")

    def test_prompt_baseline_survives_follow_up_prompt_for_all_clients(self) -> None:
        cases = {
            "codex": "UserPromptSubmit",
            "claude-code": "UserPromptSubmit",
            "gemini-cli": "BeforeAgent",
            "antigravity-cli": "PreInvocation",
        }
        for integration, prompt_event in cases.items():
            with self.subTest(integration=integration), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                root = workspace / "repository"
                state = workspace / "state"
                root.mkdir()
                state.mkdir()
                source = root / "source.txt"
                source.write_bytes(b"baseline\n")
                initialize_git(root)
                run_git(root, "add", ".")
                run_git(root, "commit", "-m", "baseline")
                baseline_path = state / "session.baseline.json"
                loop_path = state / "session.loop.json"
                pending_path = state / "pending.json"
                config = {"source_adapter": "git", "ignored_paths": [], "enabled": True}

                with patch.object(HOOK_RUNTIME, "_project_root", return_value=root), patch.object(
                    HOOK_RUNTIME, "load_config", return_value=config
                ), patch.object(HOOK_RUNTIME, "_state_paths", return_value=(baseline_path, loop_path)), patch.object(
                    HOOK_RUNTIME, "_pending_path", return_value=pending_path
                ):
                    HOOK_RUNTIME.capture_task_baseline(
                        {"cwd": str(root), "session_id": "session", "hook_event_name": "SessionStart"},
                        integration,
                    )
                    HOOK_RUNTIME.capture_task_baseline(
                        {"cwd": str(root), "session_id": "session", "hook_event_name": prompt_event},
                        integration,
                    )
                    source.write_bytes(b"changed after first prompt\n")
                    HOOK_RUNTIME.capture_task_baseline(
                        {"cwd": str(root), "session_id": "session", "hook_event_name": "SessionStart"},
                        integration,
                    )
                    HOOK_RUNTIME.capture_task_baseline(
                        {"cwd": str(root), "session_id": "session", "hook_event_name": prompt_event},
                        integration,
                    )

                stored = json.loads(baseline_path.read_text(encoding="utf-8"))
                self.assertEqual("prompt", stored["baseline_kind"])
                changed = SOURCE_MANIFEST.compare_with_baseline(stored["snapshot"], root)
                self.assertEqual([("file:source.txt", "modified")], [(entry["anchor"], entry["change"]) for entry in changed["entries"]])


if __name__ == "__main__":
    unittest.main()
