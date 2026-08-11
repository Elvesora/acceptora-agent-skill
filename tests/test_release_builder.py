from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
BUILDER = SKILL_ROOT / "scripts" / "build_release.py"
INSTALLER = SKILL_ROOT / "scripts" / "install.py"


def load_builder_module() -> object:
    specification = importlib.util.spec_from_file_location("acceptora_release_builder", BUILDER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def load_installer_module() -> object:
    specification = importlib.util.spec_from_file_location("acceptora_release_identity_installer", INSTALLER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def run_builder(
    dist: Path,
    source: Path = SKILL_ROOT,
    *,
    source_commit: str | None = "UNVERSIONED",
    allow_dirty: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        str(BUILDER),
        "--source-root",
        str(source),
        "--dist-dir",
        str(dist),
        "--format",
        "json",
    ]
    if source_commit is not None:
        arguments.extend(["--source-commit", source_commit])
    if allow_dirty:
        arguments.append("--allow-dirty")

    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseBuilderTest(unittest.TestCase):
    def test_release_versions_use_strict_semver_with_build_metadata(self) -> None:
        builder = load_builder_module()

        for version in ("1.0.0", "1.0.0-rc.1", "1.0.0+build.1", "1.0.0-rc.1+build.1"):
            with self.subTest(version=version):
                self.assertTrue(builder._is_semantic_version(version))
        for version in ("01.0.0", "1.0", "1.0.0-01", "1.0.0+", "1.0.0+build..1"):
            with self.subTest(version=version):
                self.assertFalse(builder._is_semantic_version(version))

    def test_builds_byte_identical_archives_manifest_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first_dist = workspace / "first"
            second_dist = workspace / "second"
            first = run_builder(first_dist)
            second = run_builder(second_dist)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            first_result = json.loads(first.stdout)
            second_result = json.loads(second.stdout)
            self.assertEqual("built", first_result["status"])
            self.assertEqual(first_result["source_tree_sha256"], second_result["source_tree_sha256"])
            expected_files = {
                "verify-generated-work-1.0.0.zip",
                "verify-generated-work-1.0.0.tar.gz",
                "release-manifest.json",
                "SHA256SUMS",
            }
            self.assertEqual(expected_files, {path.name for path in first_dist.iterdir() if path.is_file()})
            self.assertEqual(expected_files, {path.name for path in second_dist.iterdir() if path.is_file()})
            for name in expected_files:
                self.assertEqual((first_dist / name).read_bytes(), (second_dist / name).read_bytes(), name)

            manifest = json.loads((first_dist / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("verify-generated-work", manifest["name"])
            self.assertEqual("1.0.0", manifest["version"])
            self.assertEqual("UNVERSIONED", manifest["source_commit"])
            self.assertIn(manifest["source_state"], {"dirty_allowed", "unversioned", "unversioned_requested"})
            self.assertEqual(["codex", "claude-code", "gemini-cli"], manifest["supported_clients"])
            installer = load_installer_module()
            self.assertEqual(
                manifest["source_tree_sha256"],
                installer._package_source_tree_sha256(installer._iter_release_identity_files()),
            )
            self.assertEqual(
                {
                    "codex": "codex-cli 0.147.0",
                    "claude-code": "2.1.114",
                    "gemini-cli": "0.24.5",
                },
                manifest["reference_client_builds"],
            )
            self.assertEqual(
                {"codex": "codex-cli 0.144.0"},
                manifest["minimum_client_builds"],
            )
            paths = [entry["path"] for entry in manifest["files"]]
            self.assertEqual(sorted(paths), paths)
            self.assertIn("SKILL.md", paths)
            self.assertIn("SETUP.md", paths)
            self.assertIn("LICENSE", paths)
            self.assertIn("CHANGELOG.md", paths)
            self.assertNotIn("README.md", paths)
            self.assertNotIn("CONTRIBUTING.md", paths)
            self.assertNotIn("SECURITY.md", paths)
            self.assertNotIn("CODE_OF_CONDUCT.md", paths)
            self.assertNotIn("SUPPORT.md", paths)
            self.assertNotIn(".gitattributes", paths)
            self.assertNotIn(".gitignore", paths)
            self.assertFalse(any(path.startswith(".github/") for path in paths))
            self.assertNotIn("scripts/preview_install.py", paths)
            self.assertIn("scripts/install.py", paths)
            self.assertIn("adapters/gemini/after_agent.py", paths)
            self.assertNotIn("scripts/build_release.py", paths)
            self.assertFalse(any(path.startswith("tests/") for path in paths))
            self.assertFalse(any(path.endswith(".deferred") for path in paths))
            self.assertFalse(any("__pycache__" in path for path in paths))
            with zipfile.ZipFile(first_dist / "verify-generated-work-1.0.0.zip") as archive:
                for path in paths:
                    if path.endswith(".md"):
                        body = archive.read(f"verify-generated-work/{path}").decode("utf-8")
                        self.assertNotIn("[Unreleased]", body, path)

            artifacts = {entry["filename"]: entry for entry in manifest["artifacts"]}
            for name, entry in artifacts.items():
                artifact = first_dist / name
                self.assertEqual(f"sha256:{sha256(artifact)}", entry["sha256"])
                self.assertEqual(artifact.stat().st_size, entry["size"])
            checksums = {}
            for line in (first_dist / "SHA256SUMS").read_text(encoding="ascii").splitlines():
                digest, name = line.split("  ", 1)
                checksums[name] = digest
            self.assertEqual(sha256(first_dist / "verify-generated-work-1.0.0.zip"), checksums["verify-generated-work-1.0.0.zip"])
            self.assertEqual(sha256(first_dist / "verify-generated-work-1.0.0.tar.gz"), checksums["verify-generated-work-1.0.0.tar.gz"])
            self.assertEqual(sha256(first_dist / "release-manifest.json"), checksums["release-manifest.json"])

    def test_archive_members_are_safe_sorted_and_have_normalized_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist"
            result = run_builder(dist)
            self.assertEqual(0, result.returncode, result.stderr)
            zip_path = dist / "verify-generated-work-1.0.0.zip"
            tar_path = dist / "verify-generated-work-1.0.0.tar.gz"

            with zipfile.ZipFile(zip_path) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                self.assertEqual(sorted(names), names)
                self.assertTrue(all(name.startswith("verify-generated-work/") for name in names))
                self.assertTrue(all(".." not in Path(name).parts for name in names))
                self.assertTrue(all(not Path(name).is_absolute() for name in names))
                self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos))
                self.assertTrue(all(info.create_system == 3 for info in infos))

            with tarfile.open(tar_path, "r:gz") as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
                self.assertEqual(sorted(names), names)
                self.assertTrue(all(member.isfile() for member in members))
                self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in members))
                self.assertTrue(all(member.uname == "" and member.gname == "" for member in members))
                self.assertTrue(all(member.mtime == 0 for member in members))
                self.assertTrue(all(member.mode in {0o644, 0o755} for member in members))

    def test_rejects_secret_files_and_symlinks_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repository"
            source = repository / "packages" / "verify-generated-work"
            (source / "config").mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: verify-generated-work\ndescription: Test.\n---\n", encoding="utf-8")
            (source / "config" / "package-manifest.json").write_text(
                json.dumps(
                    {
                        "skill": {"name": "verify-generated-work", "version": "1.0.0"},
                        "integration": {"version": "1.0.0"},
                        "contract": {"version": "1.0.0", "mcp_protocol_version": "2025-11-25"},
                    }
                ),
                encoding="utf-8",
            )
            (source / ".env").write_text("ACCEPTORA_AGENT_TOKEN=secret\n", encoding="utf-8")
            dist = workspace / "secret-dist"
            secret_result = run_builder(dist, source)
            self.assertEqual(2, secret_result.returncode)
            self.assertIn("credential", secret_result.stderr)
            self.assertFalse(dist.exists())

            (source / ".env").unlink()
            synthetic_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("Z" * 48)
            secret_bodies = (
                b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nsynthetic\n-----END ENCRYPTED PRIVATE KEY-----\n",
                f"prefix{synthetic_token}".encode(),
                f"{synthetic_token}suffix".encode(),
                f"prefix{synthetic_token}suffix".encode(),
                b'{"password":"correct-horse-battery-staple"}\n',
                b'{"apiKey":"abcdefghijklmnopqrstuvwx"}\n',
                b'{"Authorization":"Basic dXNlcjpwYXNz"}\n',
            )
            credential_file = source / "config" / "backup.json"
            for index, body in enumerate(secret_bodies):
                with self.subTest(secret_body=index):
                    credential_file.write_bytes(body)
                    credential_dist = workspace / f"credential-dist-{index}"
                    credential_result = run_builder(credential_dist, source)
                    self.assertEqual(2, credential_result.returncode)
                    self.assertIn("credential", credential_result.stderr)
                    self.assertFalse(credential_dist.exists())
            credential_file.unlink()

            placeholder_file = source / "config" / "placeholder.json"
            placeholder_file.write_text(
                json.dumps({"Authorization": "Bearer ${ACCEPTORA_AGENT_TOKEN}", "clientSecret": "[REDACTED]"}),
                encoding="utf-8",
            )
            placeholder_dist = workspace / "placeholder-dist"
            placeholder_result = run_builder(placeholder_dist, source)
            self.assertEqual(0, placeholder_result.returncode, placeholder_result.stderr)
            placeholder_file.unlink()

            outside = workspace / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            try:
                (source / "linked.txt").symlink_to(outside)
            except OSError as error:
                self.skipTest(f"File symlinks are unavailable: {error}")
            linked_dist = workspace / "linked-dist"
            linked_result = run_builder(linked_dist, source)
            self.assertEqual(2, linked_result.returncode)
            self.assertIn("symlink", linked_result.stderr.lower())
            self.assertFalse(linked_dist.exists())

    def test_clean_git_provenance_is_recorded_and_dirty_or_existing_destinations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repository"
            source = repository / "packages" / "verify-generated-work"
            (source / "config").mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: verify-generated-work\ndescription: Test release source.\n---\n",
                encoding="utf-8",
            )
            (source / "config" / "package-manifest.json").write_text(
                json.dumps(
                    {
                        "skill": {"name": "verify-generated-work", "version": "1.0.0"},
                        "integration": {"version": "1.0.0"},
                        "contract": {"version": "1.0.0", "mcp_protocol_version": "2025-11-25"},
                    }
                ),
                encoding="utf-8",
            )
            git_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Release Test",
                "GIT_AUTHOR_EMAIL": "release@example.test",
                "GIT_COMMITTER_NAME": "Release Test",
                "GIT_COMMITTER_EMAIL": "release@example.test",
            }
            for command in (
                ["init"],
                ["config", "core.autocrlf", "false"],
                ["add", "."],
                ["commit", "-m", "test source"],
            ):
                completed = subprocess.run(
                    ["git", "-C", str(repository), *command],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=git_environment,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

            clean_dist = workspace / "clean-dist"
            clean = run_builder(clean_dist, source, source_commit=None, allow_dirty=False)
            self.assertEqual(0, clean.returncode, clean.stderr)
            clean_manifest = json.loads((clean_dist / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("clean", clean_manifest["source_state"])
            self.assertRegex(clean_manifest["source_commit"], r"^[a-f0-9]{40,64}$")

            ignored_payload = source / "ignored_payload.py"
            (repository / ".git" / "info" / "exclude").write_text(
                "packages/verify-generated-work/ignored_payload.py\n",
                encoding="utf-8",
            )
            ignored_payload.write_text("print('must not ship')\n", encoding="utf-8")
            ignored_dist = workspace / "ignored-dist"
            ignored = run_builder(ignored_dist, source, source_commit=None, allow_dirty=False)
            self.assertEqual(0, ignored.returncode, ignored.stderr)
            ignored_manifest = json.loads((ignored_dist / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("clean", ignored_manifest["source_state"])
            self.assertNotIn("ignored_payload.py", [entry["path"] for entry in ignored_manifest["files"]])
            with zipfile.ZipFile(ignored_dist / "verify-generated-work-1.0.0.zip") as archive:
                self.assertNotIn("verify-generated-work/ignored_payload.py", archive.namelist())

            original_blob = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD:packages/verify-generated-work/SKILL.md"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            replacement_blob = subprocess.run(
                ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
                input="---\nname: verify-generated-work\ndescription: Replaced malicious source.\n---\n",
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(repository), "replace", original_blob, replacement_blob],
                capture_output=True,
                text=True,
                check=True,
            )
            replaced_dist = workspace / "replace-ref-dist"
            replaced = run_builder(replaced_dist, source, source_commit=None, allow_dirty=False)
            self.assertEqual(0, replaced.returncode, replaced.stderr)
            with zipfile.ZipFile(replaced_dist / "verify-generated-work-1.0.0.zip") as archive:
                skill_body = archive.read("verify-generated-work/SKILL.md").decode("utf-8")
            self.assertIn("description: Test release source.", skill_body)
            self.assertNotIn("Replaced malicious source", skill_body)

            (source / "SKILL.md").write_text(
                "---\nname: verify-generated-work\ndescription: Dirty release source.\n---\n",
                encoding="utf-8",
            )
            refused_dist = workspace / "refused-dist"
            refused = run_builder(refused_dist, source, source_commit=None, allow_dirty=False)
            self.assertEqual(2, refused.returncode)
            self.assertIn("clean checkout", refused.stderr)
            self.assertFalse(refused_dist.exists())

            dirty_dist = workspace / "dirty-dist"
            dirty = run_builder(dirty_dist, source)
            self.assertEqual(0, dirty.returncode, dirty.stderr)
            dirty_manifest = json.loads((dirty_dist / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("dirty_allowed", dirty_manifest["source_state"])
            self.assertEqual("UNVERSIONED", dirty_manifest["source_commit"])

            existing_dist = workspace / "existing-dist"
            existing_dist.mkdir()
            sentinel = existing_dist / "keep.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            existing = run_builder(existing_dist, source)
            self.assertEqual(2, existing.returncode)
            self.assertIn("must not already exist", existing.stderr)
            self.assertEqual("preserve\n", sentinel.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows line-ending release gate")
    def test_publishable_windows_build_requires_repository_local_autocrlf_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repository"
            source = repository / "package"
            (source / "config").mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: verify-generated-work\ndescription: Test release source.\n---\n",
                encoding="utf-8",
            )
            (source / "config" / "package-manifest.json").write_text(
                json.dumps({"skill": {"name": "verify-generated-work", "version": "1.0.0"}}),
                encoding="utf-8",
            )
            git_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Release Test",
                "GIT_AUTHOR_EMAIL": "release@example.test",
                "GIT_COMMITTER_NAME": "Release Test",
                "GIT_COMMITTER_EMAIL": "release@example.test",
            }
            for command in (
                ["init"],
                ["config", "core.autocrlf", "false"],
                ["add", "."],
                ["commit", "-m", "test source"],
            ):
                completed = subprocess.run(
                    ["git", "-C", str(repository), *command],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=git_environment,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

            misconfigured = subprocess.run(
                ["git", "-C", str(repository), "config", "--local", "core.autocrlf", "true"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, misconfigured.returncode, misconfigured.stderr)

            refused = run_builder(workspace / "refused-dist", source, source_commit=None, allow_dirty=False)

            self.assertEqual(2, refused.returncode)
            self.assertIn("core.autocrlf=false", refused.stderr)
            self.assertFalse((workspace / "refused-dist").exists())

            configured = subprocess.run(
                ["git", "-C", str(repository), "config", "--local", "core.autocrlf", "false"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, configured.returncode, configured.stderr)

            accepted = run_builder(workspace / "accepted-dist", source, source_commit=None, allow_dirty=False)

            self.assertEqual(0, accepted.returncode, accepted.stderr)
            manifest = json.loads((workspace / "accepted-dist" / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("clean", manifest["source_state"])

    def test_publishable_build_requires_git_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source"
            (source / "config").mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: verify-generated-work\ndescription: Test release source.\n---\n",
                encoding="utf-8",
            )
            (source / "config" / "package-manifest.json").write_text(
                json.dumps({"skill": {"name": "verify-generated-work", "version": "1.0.0"}}),
                encoding="utf-8",
            )
            empty_path = workspace / "empty-path"
            empty_path.mkdir()
            result = run_builder(
                workspace / "dist",
                source,
                source_commit=None,
                allow_dirty=False,
                environment={**os.environ, "PATH": str(empty_path)},
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("Git is required", result.stderr)
            self.assertFalse((workspace / "dist").exists())

    def test_release_provenance_never_executes_git_from_the_release_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repository"
            source = repository / "package"
            (source / "config").mkdir(parents=True)
            (repository / ".git").mkdir()
            (source / "SKILL.md").write_text(
                "---\nname: verify-generated-work\ndescription: Test release source.\n---\n",
                encoding="utf-8",
            )
            (source / "config" / "package-manifest.json").write_text(
                json.dumps({"skill": {"name": "verify-generated-work", "version": "1.0.0"}}),
                encoding="utf-8",
            )
            executable_name = "git.exe" if os.name == "nt" else "git"
            fake_git = repository / executable_name
            fake_git.write_text("not an executable\n", encoding="utf-8")
            fake_git.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": str(repository),
            }
            if os.name == "nt":
                environment["PATHEXT"] = ".EXE"

            dist = workspace / "dist"
            result = run_builder(dist, source, environment=environment)

            self.assertEqual(2, result.returncode)
            self.assertIn("outside the release worktree", result.stderr)
            self.assertFalse(dist.exists())


if __name__ == "__main__":
    unittest.main()
