from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
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
FEATURE_ID = "feat_01J00000000000000000000001"
VALID_TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)


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


class CompletionGatePayloadTest(unittest.TestCase):
    def test_source_paths_preserve_leading_dots_and_reject_unsafe_segments(self) -> None:
        self.assertEqual(
            ".github/workflows/verify.yml",
            SOURCE_MANIFEST._normalise_relative("./.github/workflows/verify.yml"),
        )
        for path in ("../escape", "nested/../escape", "/absolute", "C:/absolute"):
            with self.subTest(path=path), self.assertRaises(SOURCE_MANIFEST.ManifestError):
                SOURCE_MANIFEST._normalise_relative(path)

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

    def test_strict_git_binds_tracked_ignored_paths_but_omits_ignored_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracked_paths = (
                "public/build/app.js",
                "dist/bundle.js",
                "vendor/package/source.php",
                "node_modules/package/index.js",
            )
            for relative in tracked_paths:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"baseline:{relative}\n", encoding="utf-8")
            initialize_git(root)
            run_git(root, "add", ".")
            run_git(root, "commit", "-m", "baseline")
            baseline = SOURCE_MANIFEST.capture_snapshot(root, "git")
            self.assertTrue(set(tracked_paths) <= {entry["path"] for entry in baseline["entries"]})

            for relative in tracked_paths:
                (root / relative).write_text(f"changed:{relative}\n", encoding="utf-8")
            ignored_untracked = root / "build" / "untracked.js"
            ignored_untracked.parent.mkdir(parents=True, exist_ok=True)
            ignored_untracked.write_text("ignored\n", encoding="utf-8")
            comparison = SOURCE_MANIFEST.compare_with_baseline(baseline, root)
            self.assertEqual(set(tracked_paths), {entry["path"] for entry in comparison["entries"]})
            self.assertNotIn("build/untracked.js", {entry["path"] for entry in comparison["entries"]})

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

    def test_runtime_versions_are_loaded_from_the_package_manifest(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["skill"]["version"], HOOK_RUNTIME.SKILL_VERSION)
        self.assertEqual(manifest["integration"]["version"], HOOK_RUNTIME.INTEGRATION_VERSION)
        self.assertEqual(manifest["contract"]["version"], HOOK_RUNTIME.CONTRACT_VERSION)

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
        self.assertEqual("1.0.0", payload["adapter_version"])
        self.assertEqual("base-revision", payload["current_source_descriptor"]["base_revision"])
        self.assertEqual("modified", payload["source_manifest"]["entries"][0]["change_kind"])
        self.assertEqual(f"sha256:{content_digest}", payload["source_manifest"]["entries"][0]["content_digest"])
        self.assertEqual("1.0.0", payload["versions"]["contract_version"])
        self.assertNotIn("changed_surface_manifest", payload)
        self.assertNotIn("explicit_feature_id", payload)

    def test_rejects_an_invalid_adapter_digest_before_network_io(self) -> None:
        manifest = {
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
