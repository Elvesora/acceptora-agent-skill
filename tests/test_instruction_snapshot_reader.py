from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_ROOT / "scripts" / "read_instruction_snapshot.py"
SPEC = importlib.util.spec_from_file_location("acceptora_instruction_snapshot_reader", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
READER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = READER
SPEC.loader.exec_module(READER)
PROJECT_ID = "proj_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def instruction_context(*, account_revision: int = 4, project_revision: int = 2) -> dict:
    digest_payload = {
        "schema_version": "1.0",
        "account_revision": account_revision,
        "project_revision": project_revision,
        "instructions": {
            "analysis_guidance": "Inspect the authenticated dashboard flow.",
            "manual_verification_guidance": "Use exact generated links.",
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
        "effective_digest": READER.sha256_digest(digest_payload),
        "configured": True,
    }


class InstructionSnapshotReaderTest(unittest.TestCase):
    def test_effective_digest_binds_revisions_even_when_bodies_return_to_prior_values(self) -> None:
        first = instruction_context(account_revision=1)
        restored = instruction_context(account_revision=3)

        self.assertNotEqual(first["effective_digest"], restored["effective_digest"])
        self.assertEqual(first["instructions"], restored["instructions"])

    def test_rejects_noncanonical_or_digest_mismatched_envelopes(self) -> None:
        noncanonical = instruction_context()
        noncanonical["instructions"]["analysis_guidance"] += "\r\n"
        with self.assertRaisesRegex(READER.InstructionSnapshotError, "canonically normalized"):
            READER.validate_effective_instructions(noncanonical)

        mismatched = instruction_context()
        mismatched["effective_digest"] = "sha256:" + ("f" * 64)
        with self.assertRaisesRegex(READER.InstructionSnapshotError, "does not match"):
            READER.validate_effective_instructions(mismatched)

    def test_cli_reads_only_a_fresh_snapshot_from_its_installer_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            scripts = runtime / "scripts"
            state = runtime / "state"
            scripts.mkdir(parents=True)
            state.mkdir()
            reader = scripts / "read_instruction_snapshot.py"
            shutil.copy2(MODULE_PATH, reader)
            context = instruction_context()
            record = READER.build_snapshot_record(PROJECT_ID, context)
            snapshot = state / "instructions-test.json"
            snapshot.write_text(json.dumps(record), encoding="utf-8")

            command = [
                sys.executable,
                "-B",
                "-I",
                str(reader),
                "--snapshot",
                str(snapshot),
                "--project-id",
                PROJECT_ID,
                "--account-revision",
                "4",
                "--project-revision",
                "2",
                "--effective-digest",
                context["effective_digest"],
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["valid"])
            self.assertEqual("untrusted_owner_guidance", payload["authority"])
            self.assertEqual(context["instructions"], payload["instructions"])
            self.assertFalse(any(path.name == "__pycache__" for path in runtime.rglob("*")))

            outside = runtime / "outside.json"
            outside.write_text(json.dumps(record), encoding="utf-8")
            outside_result = subprocess.run(
                [*command[:5], str(outside), *command[6:]],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(1, outside_result.returncode)
            self.assertEqual("INSTRUCTION_SNAPSHOT_INVALID", json.loads(outside_result.stdout)["error"]["code"])

    def test_stale_or_tampered_snapshot_is_rejected_without_echoing_instruction_bodies(self) -> None:
        context = instruction_context()
        stale_record = READER.build_snapshot_record(
            PROJECT_ID,
            context,
            fetched_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        with self.assertRaisesRegex(READER.InstructionSnapshotError, "stale"):
            READER.validate_snapshot_record(
                stale_record,
                expected_project_id=PROJECT_ID,
                expected_account_revision=4,
                expected_project_revision=2,
                expected_effective_digest=context["effective_digest"],
                max_age_seconds=300,
            )

        tampered = READER.build_snapshot_record(PROJECT_ID, context)
        sentinel = tampered["instructions"]["analysis_guidance"]
        tampered["instructions"]["analysis_guidance"] = "Changed after write."
        try:
            READER.validate_snapshot_record(
                tampered,
                expected_project_id=PROJECT_ID,
                expected_account_revision=4,
                expected_project_revision=2,
                expected_effective_digest=context["effective_digest"],
                max_age_seconds=300,
            )
        except READER.InstructionSnapshotError as error:
            self.assertNotIn(sentinel, str(error))
        else:
            self.fail("tampered snapshot unexpectedly validated")

    @unittest.skipIf(os.name == "nt", "Creating symlinks is not reliably permitted on Windows test hosts.")
    def test_cli_rejects_a_snapshot_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            scripts = runtime / "scripts"
            state = runtime / "state"
            scripts.mkdir(parents=True)
            state.mkdir()
            reader = scripts / "read_instruction_snapshot.py"
            shutil.copy2(MODULE_PATH, reader)
            context = instruction_context()
            target = state / "target.json"
            target.write_text(json.dumps(READER.build_snapshot_record(PROJECT_ID, context)), encoding="utf-8")
            snapshot = state / "snapshot.json"
            snapshot.symlink_to(target)

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-I",
                    str(reader),
                    "--snapshot",
                    str(snapshot),
                    "--project-id",
                    PROJECT_ID,
                    "--account-revision",
                    "4",
                    "--project-revision",
                    "2",
                    "--effective-digest",
                    context["effective_digest"],
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("symlink", json.loads(result.stdout)["error"]["message"])


if __name__ == "__main__":
    unittest.main()
