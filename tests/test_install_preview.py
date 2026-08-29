from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
PREVIEW = SKILL_ROOT / "scripts" / "preview_install.py"
INSTALLER = SKILL_ROOT / "scripts" / "install.py"


def secure_test_workspace() -> tempfile.TemporaryDirectory[str]:
    holder = tempfile.TemporaryDirectory(prefix=".acceptora-preview-tests-", dir=Path.home())
    spec = importlib.util.spec_from_file_location("acceptora_preview_installer", INSTALLER)
    if spec is None or spec.loader is None:
        holder.cleanup()
        raise AssertionError("Unable to load the installer for private test-directory setup")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._secure_private_directory(Path(holder.name))
    return holder


def tree_snapshot(root: Path) -> dict[str, tuple[str, str, int]]:
    snapshot: dict[str, tuple[str, str, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            snapshot[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
        elif path.is_dir():
            snapshot[relative] = ("directory", "", path.stat().st_mtime_ns)
    return snapshot


class InstallPreviewTest(unittest.TestCase):
    def test_preview_is_deterministic_non_mutating_exact_and_secret_free(self) -> None:
        secret = "avt_preview_secret_that_must_not_appear"
        with secure_test_workspace() as temporary:
            workspace = Path(temporary)
            target = workspace / "target"
            runtime_base = workspace / "runtime"
            client_config = workspace / "clients" / "codex"
            client_config.mkdir(parents=True)
            (target / ".codex").mkdir(parents=True)
            (target / "AGENTS.md").write_text(f"Existing instructions.\nPrivate note: {secret}\n", encoding="utf-8")
            (target / ".codex" / "config.toml").write_text(f'token = "{secret}"\n', encoding="utf-8")
            (client_config / "config.toml").write_text(f'token = "{secret}"\n', encoding="utf-8")
            before = tree_snapshot(target)
            command = [
                sys.executable,
                "-B",
                "-I",
                str(PREVIEW),
                "--client",
                "codex",
                "--platform",
                "windows",
                "--target-root",
                str(target),
                "--runtime-base",
                str(runtime_base),
                "--client-config-dir",
                str(client_config),
                "--project-id",
                "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "--api-base-url",
                "https://acceptora.example",
                "--format",
                "json",
            ]
            token_env = "ACCEPTORA_AGENT_TOKEN_PROJ_01ARZ3NDEKTSV4RRFFQ69G5FAV"
            environment = {**os.environ, token_env: secret}

            first = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
            between = tree_snapshot(target)
            second = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
            after = tree_snapshot(target)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(before, between)
            self.assertEqual(before, after)
            self.assertNotIn(secret, first.stdout + first.stderr + second.stdout + second.stderr)

            preview = json.loads(first.stdout)
            self.assertTrue(preview["preview_only"])
            self.assertEqual(0, preview["mutations_performed"])
            self.assertEqual("append_managed_block", preview["managed_blocks"][0]["action"])
            self.assertIn("<!-- agent-verification:start -->", preview["managed_blocks"][0]["content"])
            self.assertIn(".verification/session-state/", preview["managed_blocks"][1]["content"])
            self.assertGreater(len(preview["skill_copy"]["files"]), 20)
            destinations = [entry["destination"] for entry in preview["skill_copy"]["files"]]
            self.assertEqual(len(destinations), len(set(destinations)))
            self.assertTrue(all(entry["sha256"].startswith("sha256:") for entry in preview["skill_copy"]["files"]))

            merges = {Path(entry["target"]).name: entry for entry in preview["manual_merges"]}
            self.assertEqual("manual_merge_required", merges["config.toml"]["action"])
            self.assertEqual(token_env, preview["token_env"])
            self.assertIn(token_env, merges["config.toml"]["content"])
            self.assertIn("non_authoritative_project_hints", merges["config.json"]["content"])
            self.assertNotIn("_url", merges["config.json"]["content"])
            self.assertNotIn("{{SKILL_ROOT_WINDOWS}}", merges["hooks.json"]["content"])
            hooks = json.loads(merges["hooks.json"]["content"])
            windows_command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
            self.assertIn(str(Path(sys.executable).resolve()).replace("\\", "/"), windows_command)
            self.assertIn(" -B -I ", windows_command)
            self.assertTrue(windows_command.startswith('& "'))
            self.assertNotIn("cmd.exe", windows_command.lower())
            self.assertIn(preview["external_runtime"]["destination"], windows_command)
            self.assertNotIn(str(target.resolve()), windows_command)
            self.assertNotIn("token_env", merges["config.json"]["content"])

    def test_claude_preview_uses_only_claude_destinations(self) -> None:
        with secure_test_workspace() as temporary:
            workspace = Path(temporary)
            target = workspace / "target"
            target.mkdir()
            client_config = workspace / "clients" / "claude-code"
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-I",
                    str(PREVIEW),
                    "--client",
                    "claude-code",
                    "--platform",
                    "posix",
                    "--target-root",
                    str(target),
                    "--runtime-base",
                    str(workspace / "runtime"),
                    "--client-config-dir",
                    str(client_config),
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            preview = json.loads(result.stdout)
            self.assertTrue(preview["skill_copy"]["destination"].endswith("/.claude/skills/acceptora"))
            self.assertTrue(preview["managed_blocks"][0]["target"].endswith("/CLAUDE.md"))
            self.assertTrue(any(entry["target"].endswith("/.claude.json") for entry in preview["manual_merges"]))
            self.assertFalse(any(entry["target"].endswith("/.codex/hooks.json") for entry in preview["manual_merges"]))


if __name__ == "__main__":
    unittest.main()
