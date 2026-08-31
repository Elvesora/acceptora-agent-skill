from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PACKAGE_ROOT / "scripts" / "install.py"
CANONICAL_REPOSITORY = "https://github.com/Elvesora/acceptora-agent-skill"
PROJECT_ID = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TOKEN_ENV = "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV"
SECRET = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)
PAYLOAD = {
    "SKILL.md": "---\nname: acceptora\ndescription: Test skill.\n---\n\nUse Acceptora.\n",
    "agents/openai.yaml": "interface:\n  display_name: Acceptora\n",
    "references/api-mcp.md": "# API and MCP\n",
    "scripts/project_context.py": """#!/usr/bin/env python3
import re

TOKEN = re.compile(r"^avt_(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})_[A-Za-z0-9]{48}$")

def _credential_identity(token):
    match = TOKEN.fullmatch(token)
    if match is None:
        raise RuntimeError("invalid project key")
    ulid = match.group("ulid")
    return f"proj_{ulid}", f"ACCEPTORA_AGENT_TOKEN_PROJ_{ulid}"

def _request_project(token):
    project_id, _ = _credential_identity(token)
    return {"project_id": project_id}

def _validate_project(payload, project_id):
    if payload.get("project_id") != project_id:
        raise RuntimeError("wrong project")
    return [], {}
""",
}
CLIENTS = {
    "codex": {
        "skill": ".agents/skills/acceptora",
        "instruction": "AGENTS.md",
        "mcp": ".codex/config.toml",
    },
    "claude-code": {
        "skill": ".claude/skills/acceptora",
        "instruction": "CLAUDE.md",
        "mcp": ".mcp.json",
    },
    "gemini-cli": {
        "skill": ".gemini/skills/acceptora",
        "instruction": "GEMINI.md",
        "mcp": ".gemini/settings.json",
    },
}


def run_git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr)
    return process.stdout.strip()


def initialize_repository(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "Acceptora Tests")
    run_git(root, "config", "user.email", "tests@acceptora.invalid")


def commit_all(root: Path, message: str) -> str:
    run_git(root, "add", "--all")
    run_git(root, "commit", "-m", message)
    return run_git(root, "rev-parse", "HEAD")


def write_text(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def build_source(root: Path) -> str:
    initialize_repository(root)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(INSTALLER, root / "scripts" / "install.py")
    for relative, content in PAYLOAD.items():
        write_text(root, relative, content)
    commit = commit_all(root, "source")
    run_git(root, "remote", "add", "origin", CANONICAL_REPOSITORY)
    return commit


def build_target(root: Path, files: dict[str, str] | None = None) -> None:
    initialize_repository(root)
    write_text(root, "README.md", "target\n")
    for relative, content in (files or {}).items():
        write_text(root, relative, content)
    commit_all(root, "target")


def installer_environment(*, with_token: bool) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ACCEPTORA_AGENT_TOKEN_")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if with_token:
        environment[TOKEN_ENV] = SECRET
    return environment


def run_installer(
    source: Path,
    command: str,
    target: Path,
    client: str,
    *,
    with_token: bool = True,
    selected_token_env: str | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        "-B",
        str(source / "scripts" / "install.py"),
        command,
        "--client",
        client,
        "--target-root",
        str(target),
    ]
    if command == "install" and selected_token_env is not None:
        arguments.extend(["--token-env", selected_token_env])
    return subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=installer_environment(with_token=with_token),
        check=False,
    )


def result_json(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if process.returncode != 0:
        raise AssertionError(process.stderr)
    return json.loads(process.stdout)


class InstallerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_temporary = tempfile.TemporaryDirectory(prefix="acceptora-source-")
        cls.source_template = Path(cls.source_temporary.name) / "source"
        cls.source_commit = build_source(cls.source_template)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.source_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="acceptora-installer-")
        self.workspace = Path(self.temporary.name)
        self.source = self.workspace / "source"
        shutil.copytree(self.source_template, self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_install_writes_only_explicit_project_local_artifacts_for_every_client(self) -> None:
        for client, layout in CLIENTS.items():
            with self.subTest(client=client):
                target = self.workspace / f"target-{client}"
                build_target(target, {layout["instruction"]: "Keep this instruction.\n"})
                result = result_json(run_installer(self.source, "install", target, client))

                self.assertEqual("installed", result["status"])
                skill_root = target / layout["skill"]
                installed_files = {
                    path.relative_to(skill_root).as_posix()
                    for path in skill_root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(set(PAYLOAD), installed_files)
                instruction = (target / layout["instruction"]).read_text(encoding="utf-8")
                self.assertIn("Keep this instruction.", instruction)
                self.assertEqual(1, instruction.count("<!-- acceptora:start -->"))
                managed = instruction[
                    instruction.index("<!-- acceptora:start -->") : instruction.index("<!-- acceptora:end -->")
                    + len("<!-- acceptora:end -->")
                ]
                self.assertNotIn("\n", managed)

                config = json.loads((target / ".acceptora/config.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    {
                        "project_id": PROJECT_ID,
                        "token_env": TOKEN_ENV,
                        "origin": "https://www.acceptora.com",
                        "installed_commit": self.source_commit,
                    },
                    config,
                )
                mcp = (target / layout["mcp"]).read_text(encoding="utf-8")
                self.assertIn(TOKEN_ENV, mcp)
                if client == "gemini-cli":
                    gemini = json.loads(mcp)
                    server = gemini["mcpServers"]["acceptora"]
                    self.assertEqual("http", server["type"])
                    self.assertEqual("https://www.acceptora.com/mcp", server["url"])
                    self.assertNotIn("httpUrl", server)
                    self.assertEqual(
                        [TOKEN_ENV],
                        gemini["security"]["environmentVariableRedaction"]["allowed"],
                    )
                created_text = instruction + mcp + json.dumps(config) + "".join(
                    path.read_text(encoding="utf-8") for path in skill_root.rglob("*") if path.is_file()
                )
                self.assertNotIn(SECRET, created_text)
                self.assertFalse((target / ".verification").exists())

    def test_missing_token_stops_at_safe_ignored_environment_store_without_writes(self) -> None:
        target = self.workspace / "target"
        build_target(target, {".gitignore": ".env\n"})
        write_text(target, ".env", "EXISTING=value\n")

        process = run_installer(
            self.source,
            "install",
            target,
            "codex",
            with_token=False,
            selected_token_env=TOKEN_ENV,
        )

        self.assertEqual(2, process.returncode)
        self.assertIn(TOKEN_ENV, process.stderr)
        self.assertIn(".env", process.stderr)
        self.assertEqual("EXISTING=value\n", (target / ".env").read_text(encoding="utf-8"))
        self.assertFalse((target / ".acceptora").exists())
        self.assertFalse((target / ".agents").exists())
        self.assertFalse((target / "AGENTS.md").exists())

    def test_update_replaces_payload_and_status_reports_current_then_drift(self) -> None:
        target = self.workspace / "target"
        build_target(target)
        result_json(run_installer(self.source, "install", target, "codex"))
        write_text(self.source, "SKILL.md", PAYLOAD["SKILL.md"] + "Updated.\n")
        second_commit = commit_all(self.source, "update payload")

        updated = result_json(run_installer(self.source, "update", target, "codex"))
        self.assertEqual(second_commit, updated["installed_commit"])
        self.assertIn("Updated.", (target / ".agents/skills/acceptora/SKILL.md").read_text(encoding="utf-8"))
        current = result_json(run_installer(self.source, "status", target, "codex"))
        self.assertEqual("current", current["status"])

        write_text(target, ".agents/skills/acceptora/SKILL.md", "local drift\n")
        drift = result_json(run_installer(self.source, "status", target, "codex"))
        self.assertEqual("drift", drift["status"])
        self.assertFalse(drift["payload_matches"])

    def test_status_reports_update_when_source_main_has_a_new_commit(self) -> None:
        target = self.workspace / "target"
        build_target(target)
        result_json(run_installer(self.source, "install", target, "gemini-cli"))
        write_text(self.source, "UNRELATED", "new commit\n")
        newest_commit = commit_all(self.source, "new main commit")

        status = result_json(run_installer(self.source, "status", target, "gemini-cli"))

        self.assertEqual("update_available", status["status"])
        self.assertEqual(self.source_commit, status["installed_commit"])
        self.assertEqual(newest_commit, status["source_commit"])

    def test_project_mcp_merge_preserves_unrelated_configuration_and_rejects_conflict(self) -> None:
        target = self.workspace / "preserve"
        build_target(
            target,
            {
                ".mcp.json": json.dumps(
                    {"setting": True, "mcpServers": {"other": {"url": "https://example.test/mcp"}}}
                )
            },
        )
        result_json(run_installer(self.source, "install", target, "claude-code"))
        merged = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
        self.assertTrue(merged["setting"])
        self.assertEqual("https://example.test/mcp", merged["mcpServers"]["other"]["url"])
        self.assertIn("acceptora", merged["mcpServers"])

        conflict = self.workspace / "conflict"
        build_target(
            conflict,
            {".mcp.json": json.dumps({"mcpServers": {"acceptora": {"url": "https://other.test"}}})},
        )
        process = run_installer(self.source, "install", conflict, "claude-code")
        self.assertEqual(2, process.returncode)
        self.assertIn("unmanaged acceptora", process.stderr)
        self.assertFalse((conflict / ".acceptora").exists())
        self.assertFalse((conflict / ".claude/skills/acceptora").exists())

    def test_gemini_allowlist_merge_and_uninstall_preserve_unrelated_project_settings(self) -> None:
        target = self.workspace / "gemini"
        build_target(
            target,
            {
                ".gemini/settings.json": json.dumps(
                    {
                        "mcpServers": {"other": {"url": "https://example.test/mcp"}},
                        "security": {
                            "environmentVariableRedaction": {
                                "allowed": ["UNRELATED_PROJECT_VARIABLE"],
                                "enabled": True,
                            },
                            "otherSetting": True,
                        },
                    }
                )
            },
        )

        result_json(run_installer(self.source, "install", target, "gemini-cli"))
        installed = json.loads((target / ".gemini/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ["UNRELATED_PROJECT_VARIABLE", TOKEN_ENV],
            installed["security"]["environmentVariableRedaction"]["allowed"],
        )
        self.assertTrue(installed["security"]["environmentVariableRedaction"]["enabled"])
        self.assertTrue(installed["security"]["otherSetting"])
        self.assertIn("other", installed["mcpServers"])

        result_json(run_installer(self.source, "uninstall", target, "gemini-cli"))
        removed = json.loads((target / ".gemini/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ["UNRELATED_PROJECT_VARIABLE"],
            removed["security"]["environmentVariableRedaction"]["allowed"],
        )
        self.assertTrue(removed["security"]["environmentVariableRedaction"]["enabled"])
        self.assertTrue(removed["security"]["otherSetting"])
        self.assertIn("other", removed["mcpServers"])

    def test_update_and_uninstall_reject_managed_mcp_drift_for_every_client(self) -> None:
        for client, layout in CLIENTS.items():
            with self.subTest(client=client):
                target = self.workspace / f"drift-{client}"
                build_target(target)
                result_json(run_installer(self.source, "install", target, client))
                mcp_path = target / layout["mcp"]
                current = mcp_path.read_text(encoding="utf-8")
                if client == "codex":
                    drifted = current.replace(TOKEN_ENV, "ACCEPTORA_AGENT_TOKEN_PROJ_01BX5ZZKBKACTAV9WEVGEMMVRZ")
                else:
                    document = json.loads(current)
                    document["mcpServers"]["acceptora"]["url"] = "https://example.test/drift"
                    drifted = json.dumps(document)
                mcp_path.write_text(drifted, encoding="utf-8")

                for command in ("update", "uninstall"):
                    rejected = run_installer(self.source, command, target, client)
                    self.assertEqual(2, rejected.returncode, rejected.stdout)
                    self.assertIn("Acceptora installer failed", rejected.stderr)
                    self.assertTrue((target / layout["skill"]).is_dir())
                    self.assertTrue((target / ".acceptora/config.json").is_file())

    def test_gemini_update_and_uninstall_reject_allowlist_drift(self) -> None:
        target = self.workspace / "gemini-allowlist-drift"
        build_target(target)
        result_json(run_installer(self.source, "install", target, "gemini-cli"))
        settings_path = target / ".gemini/settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["security"]["environmentVariableRedaction"]["allowed"].remove(TOKEN_ENV)
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        for command in ("update", "uninstall"):
            rejected = run_installer(self.source, command, target, "gemini-cli")
            self.assertEqual(2, rejected.returncode, rejected.stdout)
            self.assertIn("allowlist", rejected.stderr)
            self.assertTrue((target / ".gemini/skills/acceptora").is_dir())

    def test_codex_rejects_invalid_toml_and_every_unmanaged_acceptora_shape(self) -> None:
        configurations = {
            "invalid": "setting = [\n",
            "inline": '[mcp_servers]\nacceptora = { url = "https://example.test/mcp" }\n',
            "dotted": 'mcp_servers.acceptora.url = "https://example.test/mcp"\n',
        }
        for name, configuration in configurations.items():
            with self.subTest(name=name):
                target = self.workspace / f"codex-{name}"
                build_target(target, {".codex/config.toml": configuration})
                rejected = run_installer(self.source, "install", target, "codex")
                self.assertEqual(2, rejected.returncode, rejected.stdout)
                self.assertFalse((target / ".acceptora").exists())
                self.assertFalse((target / ".agents/skills/acceptora").exists())

    def test_update_and_uninstall_refuse_missing_or_extra_skill_tree_entries(self) -> None:
        for drift in ("missing", "extra"):
            with self.subTest(drift=drift):
                target = self.workspace / f"skill-{drift}"
                build_target(target)
                result_json(run_installer(self.source, "install", target, "codex"))
                skill_root = target / ".agents/skills/acceptora"
                if drift == "missing":
                    (skill_root / "SKILL.md").unlink()
                else:
                    write_text(skill_root, "user-notes.txt", "keep\n")

                status = result_json(run_installer(self.source, "status", target, "codex"))
                self.assertEqual("drift", status["status"])
                self.assertFalse(status["payload_matches"])
                for command in ("update", "uninstall"):
                    rejected = run_installer(self.source, command, target, "codex")
                    self.assertEqual(2, rejected.returncode, rejected.stdout)
                    self.assertIn("missing or extra", rejected.stderr)
                    self.assertTrue(skill_root.is_dir())
                    self.assertTrue((target / ".acceptora/config.json").is_file())

    def test_uninstall_removes_owned_artifacts_and_preserves_project_content(self) -> None:
        target = self.workspace / "target"
        build_target(
            target,
            {
                "CLAUDE.md": "User instruction.\n",
                ".mcp.json": json.dumps({"mcpServers": {"other": {"url": "https://example.test"}}}),
            },
        )
        result_json(run_installer(self.source, "install", target, "claude-code"))

        removed = result_json(run_installer(self.source, "uninstall", target, "claude-code"))

        self.assertEqual("uninstalled", removed["status"])
        self.assertFalse((target / ".claude/skills/acceptora").exists())
        self.assertFalse((target / ".acceptora/config.json").exists())
        instruction = (target / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual("User instruction.\n", instruction)
        mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual({"url": "https://example.test"}, mcp["mcpServers"]["other"])
        self.assertNotIn("acceptora", mcp["mcpServers"])

    def test_source_and_target_must_keep_exact_git_boundaries(self) -> None:
        target = self.workspace / "target"
        build_target(target)
        run_git(self.source, "remote", "set-url", "origin", "https://github.com/example/not-acceptora")
        wrong_source = run_installer(self.source, "install", target, "codex")
        self.assertEqual(2, wrong_source.returncode)
        self.assertIn("canonical Acceptora GitHub origin", wrong_source.stderr)
        self.assertFalse((target / ".acceptora").exists())

        run_git(self.source, "remote", "set-url", "origin", CANONICAL_REPOSITORY)
        subdirectory = target / "nested"
        subdirectory.mkdir()
        wrong_target = run_installer(self.source, "install", subdirectory, "codex")
        self.assertEqual(2, wrong_target.returncode)
        self.assertIn("exact Git worktree root", wrong_target.stderr)
        self.assertFalse((target / ".acceptora").exists())

    def test_two_projects_keep_distinct_derived_credential_names(self) -> None:
        second_project = "proj_01BX5ZZKBKACTAV9WEVGEMMVRZ"
        second_token_env = "ACCEPTORA_AGENT_TOKEN_PROJ_01BX5ZZKBKACTAV9WEVGEMMVRZ"
        first = self.workspace / "first"
        second = self.workspace / "second"
        build_target(first)
        build_target(second)
        result_json(run_installer(self.source, "install", first, "codex"))

        environment = installer_environment(with_token=False)
        environment[second_token_env] = "avt_01BX5ZZKBKACTAV9WEVGEMMVRZ_" + ("B" * 48)
        process = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.source / "scripts/install.py"),
                "install",
                "--client",
                "codex",
                "--target-root",
                str(second),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        result_json(process)
        first_config = json.loads((first / ".acceptora/config.json").read_text(encoding="utf-8"))
        second_config = json.loads((second / ".acceptora/config.json").read_text(encoding="utf-8"))
        self.assertEqual(TOKEN_ENV, first_config["token_env"])
        self.assertEqual(second_project, second_config["project_id"])
        self.assertEqual(second_token_env, second_config["token_env"])
        self.assertNotEqual(first_config["token_env"], second_config["token_env"])

    def test_multiple_project_variables_require_selection_and_invalid_key_never_writes(self) -> None:
        second_token_env = "ACCEPTORA_AGENT_TOKEN_PROJ_01BX5ZZKBKACTAV9WEVGEMMVRZ"
        target = self.workspace / "ambiguous"
        build_target(target)
        environment = installer_environment(with_token=True)
        environment[second_token_env] = "avt_01BX5ZZKBKACTAV9WEVGEMMVRZ_" + ("B" * 48)
        command = [
            sys.executable,
            "-B",
            str(self.source / "scripts/install.py"),
            "install",
            "--client",
            "codex",
            "--target-root",
            str(target),
        ]
        ambiguous = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        self.assertEqual(2, ambiguous.returncode)
        self.assertIn("--token-env", ambiguous.stderr)
        self.assertFalse((target / ".acceptora").exists())

        selected = subprocess.run(
            [*command, "--token-env", TOKEN_ENV],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        result_json(selected)

        invalid_target = self.workspace / "invalid"
        build_target(invalid_target)
        invalid_environment = installer_environment(with_token=False)
        invalid_environment[TOKEN_ENV] = "not-a-project-key"
        invalid = subprocess.run(
            [*command[:-1], str(invalid_target), "--token-env", TOKEN_ENV],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=invalid_environment,
            check=False,
        )
        self.assertEqual(2, invalid.returncode)
        self.assertNotIn("not-a-project-key", invalid.stdout + invalid.stderr)
        self.assertFalse((invalid_target / ".acceptora").exists())
        self.assertFalse((invalid_target / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
