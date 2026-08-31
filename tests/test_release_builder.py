from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BUILDER = PACKAGE_ROOT / "scripts" / "build_release.py"
CANONICAL_REPOSITORY_URL = "https://github.com/Elvesora/acceptora-agent-skill"
PAYLOAD = {
    "SKILL.md": b"skill\n",
    "agents/openai.yaml": b"interface:\n  display_name: Acceptora\n",
    "references/api-mcp.md": b"api reference\n",
    "scripts/project_context.py": b"print('context')\n",
    "LICENSE": b"license\n",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_source(root: Path, version: str = "1.0.0") -> None:
    for relative, body in PAYLOAD.items():
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    manifest = {
        "schema_version": 2,
        "distribution": {
            "repository_url": CANONICAL_REPOSITORY_URL,
            "branch": "main",
        },
        "skill": {"name": "acceptora", "version": version},
        "integration": {"version": "1.0.0"},
        "contract": {"version": "1.0.0"},
    }
    manifest_path = root / "config" / "package-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "not-in-release.txt").write_text("excluded\n", encoding="utf-8")


def run_builder(
    source: Path,
    dist: Path,
    *,
    source_commit: str | None = None,
    allow_dirty: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-B",
        str(BUILDER),
        "--source-root",
        str(source),
        "--dist-dir",
        str(dist),
    ]
    if source_commit is not None:
        command.extend(("--source-commit", source_commit))
    if allow_dirty:
        command.append("--allow-dirty")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def initialize_canonical_repository(source: Path) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release@example.test",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release@example.test",
    }
    for arguments in (
        ("init", "--initial-branch=main"),
        ("config", "core.autocrlf", "false"),
        ("add", "--all"),
        ("commit", "-m", "release fixture"),
        ("remote", "add", "origin", CANONICAL_REPOSITORY_URL),
    ):
        completed = subprocess.run(
            ["git", "-C", str(source), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
    return subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    ).stdout.strip()


class ReleaseBuilderTest(unittest.TestCase):
    def test_local_candidate_is_deterministic_and_contains_only_the_install_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source"
            source.mkdir()
            create_source(source)

            first_dist = workspace / "first"
            second_dist = workspace / "second"
            first = run_builder(source, first_dist, source_commit="UNVERSIONED", allow_dirty=True)
            second = run_builder(source, second_dist, source_commit="UNVERSIONED", allow_dirty=True)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            expected_outputs = {"acceptora-1.0.0.zip", "release-manifest.json", "SHA256SUMS"}
            self.assertEqual(expected_outputs, {path.name for path in first_dist.iterdir()})
            self.assertEqual(expected_outputs, {path.name for path in second_dist.iterdir()})
            for filename in expected_outputs:
                self.assertEqual((first_dist / filename).read_bytes(), (second_dist / filename).read_bytes())

            manifest = json.loads((first_dist / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("unversioned", manifest["source_state"])
            self.assertEqual("UNVERSIONED", manifest["source_commit"])
            self.assertEqual(1, len(manifest["artifacts"]))
            self.assertEqual("zip", manifest["artifacts"][0]["format"])
            self.assertEqual(
                sorted(PAYLOAD),
                [entry["path"] for entry in manifest["files"]],
            )

            with zipfile.ZipFile(first_dist / "acceptora-1.0.0.zip") as archive:
                self.assertEqual(
                    sorted(
                        [
                            *(f"acceptora/{relative}" for relative in PAYLOAD),
                            "acceptora-agent-skill-provenance.json",
                        ]
                    ),
                    archive.namelist(),
                )
                self.assertEqual(
                    PAYLOAD["scripts/project_context.py"],
                    archive.read("acceptora/scripts/project_context.py"),
                )

            checksums = {
                filename: digest
                for digest, filename in (
                    line.split("  ", 1)
                    for line in (first_dist / "SHA256SUMS").read_text(encoding="ascii").splitlines()
                )
            }
            self.assertEqual(sha256(first_dist / "acceptora-1.0.0.zip"), checksums["acceptora-1.0.0.zip"])
            self.assertEqual(sha256(first_dist / "release-manifest.json"), checksums["release-manifest.json"])

    def test_clean_canonical_main_build_preserves_the_github_workflow_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source"
            source.mkdir()
            create_source(source)
            commit = initialize_canonical_repository(source)
            dist = workspace / "dist"

            completed = run_builder(source, dist, source_commit=commit)

            self.assertEqual(0, completed.returncode, completed.stderr)
            manifest = json.loads((dist / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {"repository_url": CANONICAL_REPOSITORY_URL, "branch": "main"},
                manifest["distribution"],
            )
            self.assertEqual("clean", manifest["source_state"])
            self.assertEqual(commit, manifest["source_commit"])
            self.assertEqual(
                "acceptora-agent-skill-provenance.json",
                manifest["embedded_provenance"]["filename"],
            )
            self.assertEqual(1, len([item for item in manifest["artifacts"] if item["format"] == "zip"]))

            with zipfile.ZipFile(dist / manifest["artifacts"][0]["filename"]) as archive:
                provenance_body = archive.read("acceptora-agent-skill-provenance.json")
            provenance = json.loads(provenance_body)
            self.assertEqual(commit, provenance["commit_sha"])
            self.assertEqual("clean", provenance["source_state"])
            self.assertEqual(
                "sha256:" + hashlib.sha256(provenance_body).hexdigest(),
                manifest["embedded_provenance"]["sha256"],
            )

    def test_publishable_build_rejects_dirty_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source"
            source.mkdir()
            create_source(source)
            commit = initialize_canonical_repository(source)
            (source / "scripts" / "project_context.py").write_bytes(b"changed\n")
            dist = workspace / "dist"

            completed = run_builder(source, dist, source_commit=commit)

            self.assertEqual(2, completed.returncode)
            self.assertIn("clean checkout", completed.stderr)
            self.assertFalse(dist.exists())

    def test_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source"
            source.mkdir()
            create_source(source)
            dist = workspace / "dist"
            dist.mkdir()
            sentinel = dist / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")

            completed = run_builder(source, dist, source_commit="UNVERSIONED", allow_dirty=True)

            self.assertEqual(2, completed.returncode)
            self.assertIn("must not already exist", completed.stderr)
            self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
