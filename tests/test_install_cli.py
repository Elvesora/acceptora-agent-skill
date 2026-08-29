from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any



SKILL_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = SKILL_ROOT / "scripts" / "install.py"


def load_installer_module() -> Any:
    specification = importlib.util.spec_from_file_location("acceptora_install_cli", INSTALLER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def secure_test_workspace(installer: Any, path: Path) -> None:
    if os.name != "nt":
        installer._secure_private_directory(path)
        return

    installer._set_windows_owner_only_acl(path, directory=True)
    installer._assert_private_directory(
        path,
        "Test workspace",
        allowed_read_execute_sid=installer._windows_codex_sandbox_users_sid(),
    )


class InstallCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = load_installer_module()

    def test_explicit_client_wins_over_environment_and_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".claude").mkdir()
            detected = self.installer._detect_client(
                explicit="codex",
                target_root=root,
                environ={"CLAUDECODE": "1", "CODEX_HOME": str(root)},
            )
        self.assertEqual("codex", detected)

    def test_antigravity_uses_explicit_selection_for_the_shared_agents_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agents").mkdir()

            self.assertEqual(
                "codex",
                self.installer._detect_client(explicit=None, target_root=root, environ={}),
            )
            self.assertEqual(
                "antigravity-cli",
                self.installer._detect_client(
                    explicit="antigravity-cli",
                    target_root=root,
                    environ={"CODEX_HOME": str(root)},
                ),
            )

    def test_detects_unique_agent_environment(self) -> None:
        cases = (
            ("codex", {"CODEX_HOME": "C:/Users/example/.codex"}),
            ("codex", {"CODEX_THREAD_ID": "thread-1"}),
            ("claude-code", {"CLAUDECODE": "1"}),
            ("claude-code", {"CLAUDE_CODE": "1"}),
            ("gemini-cli", {"GEMINI_CLI": "1"}),
        )
        for client, environ in cases:
            with self.subTest(client=client, environ=tuple(environ)):
                self.assertEqual(
                    client,
                    self.installer._detect_client(explicit=None, target_root=None, environ=environ),
                )

    def test_conflicting_environment_signals_fail_closed(self) -> None:
        with self.assertRaises(self.installer.InstallError) as raised:
            self.installer._detect_client(
                explicit=None,
                target_root=None,
                environ={"CLAUDECODE": "1", "CODEX_HOME": "/tmp/codex"},
            )
        message = str(raised.exception)
        self.assertIn("--client", message)
        self.assertIn("codex", message)
        self.assertIn("claude-code", message)

    def test_detects_unique_project_marker_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gemini").mkdir()
            self.assertEqual(
                "gemini-cli",
                self.installer._detect_client(explicit=None, target_root=root, environ={}),
            )

    def test_codex_agents_and_codex_directories_are_the_same_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agents").mkdir()
            (root / ".codex").mkdir()
            self.assertEqual(
                "codex",
                self.installer._detect_client(explicit=None, target_root=root, environ={}),
            )

    def test_ambiguous_project_markers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".claude").mkdir()
            (root / ".agents").mkdir()
            with self.assertRaises(self.installer.InstallError) as raised:
                self.installer._detect_client(explicit=None, target_root=root, environ={})
        self.assertIn("--client", str(raised.exception))

    def test_missing_signals_fail_closed_with_supported_clients(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(self.installer.InstallError) as raised:
                self.installer._detect_client(explicit=None, target_root=Path(temporary), environ={})
        message = str(raised.exception)
        self.assertIn("--client", message)
        for client in ("codex", "claude-code", "gemini-cli", "antigravity-cli"):
            self.assertIn(client, message)

    def test_falsey_environment_values_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(self.installer.InstallError):
                self.installer._detect_client(
                    explicit=None,
                    target_root=Path(temporary),
                    environ={"CLAUDECODE": "0", "CODEX_HOME": "", "GEMINI_CLI": "false"},
                )

    def test_plan_parser_allows_omitting_client(self) -> None:
        parsed = self.installer._parser().parse_args(
            [
                "plan",
                "--target-root",
                "/tmp/target",
                "--project-id",
                "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "--api-base-url",
                "https://acceptora.example",
            ]
        )
        self.assertIsNone(parsed.client)
        self.assertEqual("json", parsed.format)

    def test_antigravity_new_preview_plan_and_saved_plan_apply_fail_closed(self) -> None:
        profile = self.installer._client_profile("antigravity-cli")
        self.assertIs(False, profile["install_supported"])
        self.assertIsInstance(profile["unsupported_reason"], str)
        with self.assertRaisesRegex(self.installer.InstallError, "not supported for new preview"):
            self.installer.build_preview(SimpleNamespace(client="antigravity-cli"))

        installer_tests = importlib.util.spec_from_file_location(
            "acceptora_installer_unsupported_fixture_helpers",
            Path(__file__).with_name("test_installer.py"),
        )
        assert installer_tests is not None and installer_tests.loader is not None
        helpers = importlib.util.module_from_spec(installer_tests)
        installer_tests.loader.exec_module(helpers)

        with tempfile.TemporaryDirectory(prefix=".acceptora-unsupported-client-", dir=Path.home()) as temporary:
            workspace = Path(temporary)
            secure_test_workspace(self.installer, workspace)
            target = workspace / "target"
            target.mkdir()
            helpers.initialize_git_repository(target)
            runtime = helpers.runtime_base(workspace)
            client_config = helpers.client_config_dir(workspace, "antigravity-cli")
            rejected_path = workspace / "rejected-plan.json"
            rejected = helpers.run_installer(
                "plan",
                "--client",
                "antigravity-cli",
                "--target-root",
                str(target),
                "--runtime-base",
                str(runtime),
                "--client-config-dir",
                str(client_config),
                "--project-id",
                "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "--api-base-url",
                "https://acceptora.example",
                "--output",
                str(rejected_path),
            )
            self.assertEqual(2, rejected.returncode)
            self.assertIn("not supported for new preview, plan, apply", rejected.stderr)
            self.assertFalse(rejected_path.exists())

            saved_path = workspace / "historical-plan.json"
            historical = helpers.write_plan(
                workspace,
                target,
                saved_path,
                "antigravity-cli",
            )
            before = helpers.snapshot(target)
            rejected_apply = helpers.run_installer(
                "apply",
                "--plan",
                str(saved_path),
                "--accept-plan-sha256",
                historical["plan_sha256"],
            )
            self.assertEqual(2, rejected_apply.returncode)
            self.assertIn("not supported for new preview, plan, apply", rejected_apply.stderr)
            self.assertEqual(before, helpers.snapshot(target))
            self.assertFalse(Path(historical["runtime_root"]).exists())

    def test_human_plan_text_lists_digest_source_and_apply_command_without_applying(self) -> None:
        plan = {
            "preview_only": True,
            "mutations_performed": 0,
            "client": "codex",
            "target_root": "/work/app",
            "runtime_root": "/home/user/.acceptora/runtime",
            "plan_sha256": "sha256:" + ("ab" * 32),
            "package": {
                "source": {
                    "repository_url": "https://github.com/Elvesora/acceptora-agent-skill",
                    "branch": "main",
                    "commit_sha": "abc123def456",
                }
            },
            "inputs": {
                "project_id": "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "api_base_url": "https://acceptora.example",
                "python_executable": "/usr/bin/python3",
                "token_env": "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            },
            "operations": [
                {"action": "create", "target": ".agents/skills/acceptora"},
                {"action": "merge", "target": "/home/user/.codex/config.toml"},
            ],
            "conflicts": [],
            "warnings": ["No files were changed. Review this complete plan before apply."],
        }
        text = self.installer._text_result(plan, plan_path="/tmp/acceptora-install-plan.json")
        self.assertIn("INSTALL PLAN - 0 mutations performed", text)
        self.assertIn("Plan SHA-256: sha256:" + ("ab" * 32), text)
        self.assertIn("abc123def456", text)
        self.assertIn("https://github.com/Elvesora/acceptora-agent-skill", text)
        self.assertIn("proj_01ARZ3NDEKTSV4RRFFQ69G5FAV", text)
        self.assertIn(
            "Credential environment variable: ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            text,
        )
        self.assertIn("https://acceptora.example", text)
        self.assertIn("- create: .agents/skills/acceptora", text)
        self.assertIn("--accept-plan-sha256", text)
        self.assertIn("/tmp/acceptora-install-plan.json", text)
        self.assertIn("No files were changed.", text)
        self.assertNotIn("apply this plan now", text.casefold())
        self.assertNotIn("npx ", text)

    def test_text_plan_output_writes_json_plan_file_and_prints_human_summary(self) -> None:
        installer_tests = importlib.util.spec_from_file_location(
            "acceptora_installer_test_helpers",
            Path(__file__).with_name("test_installer.py"),
        )
        assert installer_tests is not None and installer_tests.loader is not None
        helpers = importlib.util.module_from_spec(installer_tests)
        installer_tests.loader.exec_module(helpers)
        client_config_dir = helpers.client_config_dir
        initialize_git_repository = helpers.initialize_git_repository
        runtime_base = helpers.runtime_base
        run_installer = helpers.run_installer

        secret = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("n" * 48)
        with tempfile.TemporaryDirectory(prefix=".acceptora-cli-tests-", dir=Path.home()) as temporary:
            workspace = Path(temporary)
            secure_test_workspace(self.installer, workspace)
            target = workspace / "target"
            target.mkdir()
            initialize_git_repository(target)
            plan_path = workspace / "acceptora-install-plan.json"
            result = run_installer(
                "plan",
                "--target-root",
                str(target),
                "--runtime-base",
                str(runtime_base(workspace)),
                "--client-config-dir",
                str(client_config_dir(workspace, "claude-code")),
                "--project-id",
                "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "--api-base-url",
                "https://acceptora.example",
                "--format",
                "text",
                "--output",
                str(plan_path),
                environment={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key
                        not in {
                            "CODEX_HOME",
                            "CODEX_THREAD_ID",
                            "CLAUDECODE",
                            "CLAUDE_CODE",
                            "GEMINI_CLI",
                        }
                    },
                    "CLAUDECODE": "1",
                    "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV": secret,
                },
            )
            self.assertEqual(0, result.returncode, result.stderr)
            saved = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["preview_only"])
            self.assertEqual(0, saved["mutations_performed"])
            self.assertEqual("claude-code", saved["client"])
            self.assertIn(saved["plan_sha256"], result.stdout)
            self.assertIn("INSTALL PLAN - 0 mutations performed", result.stdout)
            self.assertIn("--accept-plan-sha256", result.stdout)
            self.assertIn(plan_path.resolve().as_posix(), result.stdout)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, plan_path.read_text(encoding="utf-8"))
            self.assertFalse(result.stdout.lstrip().startswith("{"))
            self.assertNotIn("npx ", result.stdout)
            after = {path.name for path in target.iterdir()}
            self.assertEqual({".git"}, after)


if __name__ == "__main__":
    unittest.main()
