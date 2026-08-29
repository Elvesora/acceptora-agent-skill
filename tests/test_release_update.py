from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_ROOT / "adapters" / "hook_runtime.py"
SPEC = importlib.util.spec_from_file_location("acceptora_skill_update_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK_RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOOK_RUNTIME
SPEC.loader.exec_module(HOOK_RUNTIME)

REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
BRANCH = "main"
INSTALLED_COMMIT = "a" * 40
CURRENT_COMMIT = "b" * 40
GIT_EXECUTABLE = str(Path(sys.executable).resolve())


def runtime_config(client: str = "codex") -> dict[str, object]:
    return {
        "client": client,
        "skill_repository_url": REPOSITORY_URL,
        "skill_repository_branch": BRANCH,
        "installed_commit_sha": INSTALLED_COMMIT,
        "git_executable": GIT_EXECUTABLE,
        "skill_update_timeout_seconds": 2,
    }


def git_result(
    commit_sha: str = CURRENT_COMMIT,
    *,
    returncode: int = 0,
    stdout: bytes | None = None,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    body = stdout if stdout is not None else f"{commit_sha}\trefs/heads/main\n".encode("ascii")
    return subprocess.CompletedProcess([GIT_EXECUTABLE], returncode, stdout=body, stderr=stderr)


def run_git(git_executable: str, working_directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        [git_executable, *arguments],
        cwd=working_directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr)
    return process


def create_local_remote(git_executable: str, root: Path, name: str) -> tuple[str, str]:
    worktree = root / f"{name}-worktree"
    remote = root / f"{name}.git"
    worktree.mkdir()
    run_git(git_executable, root, "init", "--initial-branch=main", str(worktree))
    run_git(git_executable, worktree, "config", "user.name", "Acceptora Test")
    run_git(git_executable, worktree, "config", "user.email", "acceptora-test@example.invalid")
    (worktree / "identity.txt").write_text(name, encoding="utf-8")
    run_git(git_executable, worktree, "add", "identity.txt")
    run_git(git_executable, worktree, "commit", "-m", f"Create {name} identity")
    commit_sha = run_git(git_executable, worktree, "rev-parse", "HEAD").stdout.strip().lower()
    run_git(git_executable, root, "init", "--bare", str(remote))
    run_git(git_executable, worktree, "remote", "add", "origin", remote.as_uri())
    run_git(git_executable, worktree, "push", "origin", "main")
    return remote.as_uri(), commit_sha


class GitMainUpdateCheckTest(unittest.TestCase):
    def test_different_main_commit_returns_agent_driven_notice_without_credentials_or_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "ACCEPTORA_AGENT_TOKEN": "must-not-reach-git",
                "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV": "must-not-reach-git",
                "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAA": "must-not-reach-git",
                "GIT_ASKPASS": "must-not-run",
                "SSH_ASKPASS": "must-not-run",
            },
            clear=False,
        ), patch.object(HOOK_RUNTIME.subprocess, "run", return_value=git_result()) as run:
            cache_path = Path(temporary) / "skill-update.json"
            notice = HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1000)
            record = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertIsInstance(notice, str)
        assert notice is not None
        self.assertIn(INSTALLED_COMMIT[:12], notice)
        self.assertIn(CURRENT_COMMIT[:12], notice)
        self.assertIn(REPOSITORY_URL, notice)
        self.assertIn("clone a fresh main checkout", notice)
        self.assertIn("read SETUP.md completely from that checkout", notice)
        self.assertIn('"Coding-agent install or update" procedure', notice)
        self.assertIn("printed cache path identifies the installed runtime", notice)
        self.assertIn(str(cache_path), notice)
        self.assertIn("No source was fetched and no update was applied", notice)

        command = run.call_args.args[0]
        self.assertEqual(
            [
                GIT_EXECUTABLE,
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
                "refs/heads/main",
            ],
            command,
        )
        self.assertEqual(2, run.call_args.kwargs["timeout"])
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("ACCEPTORA_AGENT_TOKEN", environment)
        self.assertFalse(any(key.startswith("ACCEPTORA_AGENT_TOKEN_PROJ_") for key in environment))
        self.assertNotIn("GIT_ASKPASS", environment)
        self.assertNotIn("SSH_ASKPASS", environment)
        self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])
        isolated_worktree = Path(run.call_args.kwargs["cwd"])
        self.assertTrue(isolated_worktree.name.startswith("acceptora-skill-update-"))
        self.assertEqual(str(isolated_worktree.resolve()), environment["GIT_CEILING_DIRECTORIES"])

        self.assertEqual("acceptora_git_main_update_check", record["kind"])
        self.assertEqual("update_available", record["status"])
        self.assertEqual(CURRENT_COMMIT, record["current_commit_sha"])
        self.assertEqual(INSTALLED_COMMIT, record["installed_commit_sha"])
        self.assertEqual(0, record["setup_mutations_performed"])
        self.assertTrue(record["cache_written"])
        self.assertFalse(record["auto_apply"])
        self.assertEqual(HOOK_RUNTIME._record_digest(record), record["record_sha256"])

    def test_equal_main_commit_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            HOOK_RUNTIME.subprocess,
            "run",
            return_value=git_result(INSTALLED_COMMIT),
        ):
            cache_path = Path(temporary) / "skill-update.json"
            notice = HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1000)
            record = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertIsNone(notice)
        self.assertEqual("current", record["status"])
        self.assertEqual(INSTALLED_COMMIT, record["current_commit_sha"])

    def test_five_minute_cache_avoids_network_and_rechecks_after_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "skill-update.json"
            with patch.object(HOOK_RUNTIME.subprocess, "run", return_value=git_result()) as first_run:
                first = HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1000)
            with patch.object(HOOK_RUNTIME.subprocess, "run", return_value=git_result()) as second_run:
                cached = HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1299)
                refreshed = HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1301)

        self.assertEqual(first, cached)
        self.assertIn(CURRENT_COMMIT[:12], refreshed)
        first_run.assert_called_once()
        second_run.assert_called_once()

    def test_cache_is_bound_to_client_source_and_installed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "skill-update.json"
            with patch.object(HOOK_RUNTIME.subprocess, "run", return_value=git_result()) as run:
                HOOK_RUNTIME._check_skill_update(runtime_config("codex"), cache_path, now=1000)
                HOOK_RUNTIME._check_skill_update(runtime_config("claude-code"), cache_path, now=1001)
                record = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(2, run.call_count)
        self.assertEqual("claude-code", record["client"])

    def test_unavailable_main_fails_open_and_malformed_output_warns_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "skill-update.json"
            with patch.object(HOOK_RUNTIME.subprocess, "run", return_value=git_result(returncode=2)):
                self.assertIsNone(HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1000))
            self.assertEqual("unavailable", json.loads(cache_path.read_text(encoding="utf-8"))["status"])

            malformed = b"attacker-controlled-output\n"
            with patch.object(
                HOOK_RUNTIME.subprocess,
                "run",
                return_value=git_result(stdout=malformed),
            ):
                notice = HOOK_RUNTIME._check_skill_update(runtime_config(), cache_path, now=1301)
            record = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertIn("production branch response was invalid", notice)
        self.assertNotIn("attacker-controlled-output", notice)
        self.assertEqual("rejected", record["status"])

    def test_invalid_or_oversized_ls_remote_output_is_rejected(self) -> None:
        invalid_outputs = (
            b"",
            b"not-a-commit\trefs/heads/main\n",
            f"{CURRENT_COMMIT}\trefs/heads/other\n".encode("ascii"),
            (
                f"{CURRENT_COMMIT}\trefs/heads/main\n"
                f"{INSTALLED_COMMIT}\trefs/heads/main\n"
            ).encode("ascii"),
            b"x" * (HOOK_RUNTIME.MAX_GIT_LS_REMOTE_BYTES + 1),
        )
        for output in invalid_outputs:
            with self.subTest(output_length=len(output)), self.assertRaises(HOOK_RUNTIME.SkillUpdateRejected), patch.object(
                HOOK_RUNTIME.subprocess,
                "run",
                return_value=git_result(stdout=output),
            ):
                HOOK_RUNTIME._remote_main_commit(GIT_EXECUTABLE, REPOSITORY_URL, BRANCH, 2)

    def test_local_repository_url_rewrite_cannot_redirect_the_production_lookup(self) -> None:
        discovered_git = shutil.which("git")
        if discovered_git is None:
            self.skipTest("Git is required for the real update lookup regression test")
        git_executable = str(Path(discovered_git).resolve())

        with tempfile.TemporaryDirectory(prefix="acceptora-skill-update-regression-") as temporary:
            root = Path(temporary)
            canonical_url, canonical_commit = create_local_remote(git_executable, root, "canonical")
            redirected_url, redirected_commit = create_local_remote(git_executable, root, "redirected")
            target_repository = root / "target"
            target_repository.mkdir()
            run_git(git_executable, target_repository, "init", "--initial-branch=main")
            run_git(
                git_executable,
                target_repository,
                "config",
                f"url.{redirected_url}.insteadOf",
                canonical_url,
            )

            redirected_lookup = run_git(
                git_executable,
                target_repository,
                "ls-remote",
                "--exit-code",
                "--heads",
                canonical_url,
                "refs/heads/main",
            )
            self.assertEqual(redirected_commit, redirected_lookup.stdout.partition("\t")[0].lower())

            original_working_directory = Path.cwd()
            try:
                os.chdir(target_repository)
                observed_commit = HOOK_RUNTIME._remote_main_commit(
                    git_executable,
                    canonical_url,
                    BRANCH,
                    5,
                )
            finally:
                os.chdir(original_working_directory)

        self.assertNotEqual(canonical_commit, redirected_commit)
        self.assertEqual(canonical_commit, observed_commit)

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
        cache_path = Path("/runtime/state/skill-update.json")
        config = {
            "config_source": "installer_owned_external_runtime",
            "client": "codex",
        }
        with (
            patch.object(HOOK_RUNTIME, "_project_root", return_value=root),
            patch.object(HOOK_RUNTIME, "load_config", return_value=config),
            patch.object(HOOK_RUNTIME, "_config_path", return_value=runtime_config_path),
            patch.object(HOOK_RUNTIME, "_skill_update_cache_path", return_value=cache_path),
            patch.object(HOOK_RUNTIME, "_check_skill_update", return_value="Update available.") as check,
        ):
            self.assertEqual(
                "Update available.",
                HOOK_RUNTIME.check_for_skill_update({"hook_event_name": "SessionStart"}),
            )
        check.assert_called_once_with(config, cache_path)


if __name__ == "__main__":
    unittest.main()
