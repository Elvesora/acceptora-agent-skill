from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = PACKAGE_ROOT / "tests" / "fixtures"
MODULE_PATH = PACKAGE_ROOT / "scripts" / "validate_checklist_payload.py"
SPEC = importlib.util.spec_from_file_location("acceptora_checklist_validator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


def fixture_request(name: str) -> dict:
    fixture = json.loads((FIXTURES_ROOT / name).read_text(encoding="utf-8"))
    return fixture["request"]


def set_nested(payload: dict, path: tuple[str | int, ...], value: object) -> None:
    parent: object = payload
    for segment in path[:-1]:
        assert isinstance(parent, (dict, list))
        parent = parent[segment]
    assert isinstance(parent, (dict, list))
    parent[path[-1]] = value


def known_limit() -> dict:
    return {
        "semantic_key": "known.limit.contract",
        "description": "A documented constraint.",
        "reason": "The synthetic fixture keeps one explicit limit.",
        "affected_coverage_anchors": ["file:src/Responses/ValidationResult.php"],
        "severity": "normal",
        "blocks_acceptance": False,
        "status": "open",
        "mitigation": "Keep the limitation visible.",
        "resolution_evidence": [
            {
                "kind": "test",
                "value": "tests/ValidationResultContractTest.php",
                "summary": "Synthetic contract coverage.",
            }
        ],
    }


class ChecklistValidatorContractTest(unittest.TestCase):
    def assert_invalid(self, payload: dict, expected_code: str) -> None:
        result = VALIDATOR.validate_payload(payload)

        self.assertFalse(result["valid"], result)
        self.assertIn(expected_code, {error["code"] for error in result["errors"]}, result["errors"])

    def test_accepts_the_authoritative_sdk_validation_initial_request(self) -> None:
        result = VALIDATOR.validate_payload(fixture_request("sdk-validation-initial.json"))

        self.assertTrue(result["valid"], result.get("errors"))
        self.assertEqual(3, result["item_count"])
        self.assertEqual(3, result["manifest_anchor_count"])
        self.assertEqual(3, result["covered_anchor_count"])

    def test_accepts_the_authoritative_incremental_sdk_validation_request(self) -> None:
        result = VALIDATOR.validate_payload(fixture_request("sdk-validation-revision-2.json"))

        self.assertTrue(result["valid"], result.get("errors"))
        self.assertEqual(4, result["item_count"])
        self.assertEqual(2, result["manifest_anchor_count"])

    def test_accepts_equivalent_checklists_for_different_programming_stacks(self) -> None:
        substitutions = {
            "python": {
                "src/Responses/ValidationResult.php": "src/validation_result.py",
                "src/ValidationClient.php": "src/validation_client.py",
                "tests/ValidationResultContractTest.php": "tests/test_validation_result.py",
            },
            "typescript": {
                "src/Responses/ValidationResult.php": "src/validation-result.ts",
                "src/ValidationClient.php": "src/validation-client.ts",
                "tests/ValidationResultContractTest.php": "tests/validation-result.test.ts",
            },
            "go": {
                "src/Responses/ValidationResult.php": "validation/result.go",
                "src/ValidationClient.php": "validation/client.go",
                "tests/ValidationResultContractTest.php": "validation/result_test.go",
            },
            "rust": {
                "src/Responses/ValidationResult.php": "src/validation/result.rs",
                "src/ValidationClient.php": "src/validation/client.rs",
                "tests/ValidationResultContractTest.php": "tests/validation_result.rs",
            },
            "dotnet": {
                "src/Responses/ValidationResult.php": "src/Validation/ValidationResult.cs",
                "src/ValidationClient.php": "src/Validation/ValidationClient.cs",
                "tests/ValidationResultContractTest.php": "tests/ValidationResultContractTests.cs",
            },
        }

        for stack, replacements in substitutions.items():
            with self.subTest(stack=stack):
                encoded = json.dumps(fixture_request("sdk-validation-initial.json"))
                for original, replacement in replacements.items():
                    encoded = encoded.replace(original, replacement)
                request = json.loads(encoded)
                request["source_descriptor"]["source_locator"] = f"example/{stack}-validation"
                request["scope_summary"] = f"{stack} validation contract and documentation."
                request["preconditions"][1] = f"Run commands from the repository root with its {stack} toolchain."
                request["automated_evidence"][0]["semantic_key"] = f"baseline.{stack}.contract_tests"
                request["automated_evidence"][0]["name"] = f"Synthetic {stack} contract test fixture"
                for item in request["items"]:
                    item["semantic_key"] = item["semantic_key"].replace("sdk.php.", f"sdk.{stack}.")

                result = VALIDATOR.validate_payload(request)

                self.assertTrue(result["valid"], result.get("errors"))
                self.assertEqual(3, result["manifest_anchor_count"])
                self.assertEqual(3, result["covered_anchor_count"])

    def test_enum_values_match_the_schema_exactly_without_whitespace_normalization(self) -> None:
        base = fixture_request("sdk-validation-initial.json")
        base["known_limits"] = [known_limit()]
        cases = [
            (("source_descriptor", "source_kind"), " git ", "INVALID_SOURCE_KIND"),
            (("source_manifest", "entries", 0, "change_kind"), " added ", "INVALID_CHANGE_KIND"),
            (("source_manifest", "entries", 0, "observed_by"), " adapter ", "INVALID_OBSERVED_BY"),
            (("automated_evidence", 0, "kind"), " test ", "INVALID_EVIDENCE_KIND"),
            (("automated_evidence", 0, "outcome"), " passed ", "INVALID_EVIDENCE_OUTCOME"),
            (
                ("automated_evidence", 0, "evidence_sufficiency"),
                " sufficient ",
                "INVALID_EVIDENCE_SUFFICIENCY",
            ),
            (
                ("automated_evidence", 1, "blocker_reason"),
                " missing_credentials ",
                "INVALID_BLOCKER_REASON",
            ),
            (("items", 0, "operation"), " add ", "INVALID_OPERATION"),
            (("items", 0, "risk"), " critical ", "INVALID_RISK"),
            (("known_limits", 0, "severity"), " normal ", "INVALID_SEVERITY"),
            (("known_limits", 0, "status"), " open ", "INVALID_STATUS"),
            (
                ("known_limits", 0, "resolution_evidence", 0, "kind"),
                " test ",
                "INVALID_EVIDENCE_REFERENCE_KIND",
            ),
        ]

        for path, value, expected_code in cases:
            with self.subTest(path=path):
                request = deepcopy(base)
                set_nested(request, path, value)

                self.assert_invalid(request, expected_code)

    def test_string_lengths_match_the_published_inclusive_maximums(self) -> None:
        cases = [
            (("implementation_change_summary",), 5000, "MAX_LENGTH"),
            (("automated_evidence", 0, "name"), 200, "MAX_LENGTH"),
            (("automated_evidence", 0, "target"), 500, "MAX_LENGTH"),
            (("automated_evidence", 0, "summary"), 2000, "MAX_LENGTH"),
            (("automated_evidence", 0, "source_revision"), 500, "MAX_LENGTH"),
            (("sections", 0, "title"), 200, "MAX_LENGTH"),
            (("items", 0, "title"), 300, "MAX_LENGTH"),
        ]

        for path, maximum, expected_code in cases:
            with self.subTest(path=path, boundary="accepted"):
                request = fixture_request("sdk-validation-initial.json")
                set_nested(request, path, "x" * maximum)
                result = VALIDATOR.validate_payload(request)
                self.assertTrue(result["valid"], result.get("errors"))

            with self.subTest(path=path, boundary="rejected"):
                request = fixture_request("sdk-validation-initial.json")
                set_nested(request, path, "x" * (maximum + 1))
                self.assert_invalid(request, expected_code)

    def test_array_counts_match_the_published_inclusive_maximums(self) -> None:
        request = fixture_request("sdk-validation-initial.json")
        request["preconditions"] = [f"precondition {index}" for index in range(100)]
        result = VALIDATOR.validate_payload(request)
        self.assertTrue(result["valid"], result.get("errors"))

        request["preconditions"].append("one too many")
        self.assert_invalid(request, "MAX_ITEMS")

        evidence_template = fixture_request("sdk-validation-initial.json")["automated_evidence"][0]
        request = fixture_request("sdk-validation-initial.json")
        request["automated_evidence"] = []
        for index in range(200):
            evidence = deepcopy(evidence_template)
            evidence["semantic_key"] = f"contract.evidence.{index:03d}"
            request["automated_evidence"].append(evidence)
        result = VALIDATOR.validate_payload(request)
        self.assertTrue(result["valid"], result.get("errors"))

        extra_evidence = deepcopy(evidence_template)
        extra_evidence["semantic_key"] = "contract.evidence.overflow"
        request["automated_evidence"].append(extra_evidence)
        self.assert_invalid(request, "MAX_ITEMS")

    def test_coverage_anchor_length_matches_the_published_inclusive_maximum(self) -> None:
        request = fixture_request("sdk-validation-initial.json")
        maximum_anchor = "file:" + ("x" * 495)
        request["source_manifest"]["entries"][0]["anchor"] = maximum_anchor
        request["items"][0]["coverage_anchors"] = [maximum_anchor]

        result = VALIDATOR.validate_payload(request)

        self.assertTrue(result["valid"], result.get("errors"))

        oversized_anchor = maximum_anchor + "x"
        request["source_manifest"]["entries"][0]["anchor"] = oversized_anchor
        request["items"][0]["coverage_anchors"] = [oversized_anchor]
        self.assert_invalid(request, "INVALID_ANCHOR")

    def test_executed_at_requires_a_strict_rfc3339_date_time(self) -> None:
        valid_values = [
            "2026-08-22T09:00:00Z",
            "2026-08-22t09:00:00.123z",
            "2026-08-22T09:00:00+02:30",
            "1990-12-31T23:59:60Z",
            "1990-12-31T15:59:60-08:00",
        ]
        invalid_values = [
            "2026-02-30T09:00:00Z",
            "2026-08-22 09:00:00Z",
            "2026-08-22T09:00:00",
            "2026-08-22T09:00:00+0230",
            "2026-08-22T24:00:00Z",
            "2026-08-22T09:00:60Z",
            "2026-08-22T09:00:00+24:00",
        ]

        for executed_at in valid_values:
            with self.subTest(executed_at=executed_at, valid=True):
                request = fixture_request("sdk-validation-initial.json")
                request["automated_evidence"][0]["executed_at"] = executed_at
                result = VALIDATOR.validate_payload(request)
                self.assertTrue(result["valid"], result.get("errors"))

        for executed_at in invalid_values:
            with self.subTest(executed_at=executed_at, valid=False):
                request = fixture_request("sdk-validation-initial.json")
                request["automated_evidence"][0]["executed_at"] = executed_at
                self.assert_invalid(request, "INVALID_DATE_TIME")

    def test_accepts_structured_evidence_sufficiency_and_missing_credentials(self) -> None:
        not_run = fixture_request("sdk-validation-initial.json")
        not_run["automated_evidence"][0].update({
            "outcome": "not_run",
            "exit_status": None,
            "evidence_sufficiency": "insufficient",
            "blocker_reason": "missing_credentials",
        })

        not_run_result = VALIDATOR.validate_payload(not_run)

        self.assertTrue(not_run_result["valid"], not_run_result.get("errors"))

        passed_but_insufficient = fixture_request("sdk-validation-initial.json")
        passed_but_insufficient["automated_evidence"][0]["evidence_sufficiency"] = "insufficient"

        insufficient_result = VALIDATOR.validate_payload(passed_but_insufficient)

        self.assertTrue(insufficient_result["valid"], insufficient_result.get("errors"))

    def test_evidence_conditionals_do_not_make_optional_fields_required(self) -> None:
        not_run_without_optional_state = fixture_request("sdk-validation-initial.json")
        evidence = not_run_without_optional_state["automated_evidence"][0]
        evidence["outcome"] = "not_run"
        evidence.pop("evidence_sufficiency")

        result = VALIDATOR.validate_payload(not_run_without_optional_state)

        self.assertTrue(result["valid"], result.get("errors"))

        blocker_without_optional_sufficiency = fixture_request("sdk-validation-initial.json")
        evidence = blocker_without_optional_sufficiency["automated_evidence"][0]
        evidence["outcome"] = "not_run"
        evidence["blocker_reason"] = "missing_credentials"
        evidence.pop("evidence_sufficiency")

        result = VALIDATOR.validate_payload(blocker_without_optional_sufficiency)

        self.assertTrue(result["valid"], result.get("errors"))

    def test_rejects_contradictory_or_unknown_structured_evidence_state(self) -> None:
        cases = [
            (
                {"outcome": "not_run", "evidence_sufficiency": "sufficient"},
                "INVALID_EVIDENCE_SUFFICIENCY",
            ),
            (
                {"outcome": "passed", "blocker_reason": "missing_credentials"},
                "INVALID_BLOCKER_REASON_OUTCOME",
            ),
            (
                {"outcome": "not_run", "blocker_reason": "unknown_reason"},
                "INVALID_BLOCKER_REASON",
            ),
            (
                {"outcome": "passed", "evidence_sufficiency": None},
                "INVALID_EVIDENCE_SUFFICIENCY",
            ),
            (
                {"outcome": "passed", "evidence_sufficiency": ["sufficient"]},
                "INVALID_EVIDENCE_SUFFICIENCY",
            ),
            (
                {"outcome": "not_run", "blocker_reason": None},
                "INVALID_BLOCKER_REASON",
            ),
            (
                {"outcome": "not_run", "blocker_reason": {"reason": "missing_credentials"}},
                "INVALID_BLOCKER_REASON",
            ),
            (
                {"outcome": "passed", "unsupported_proof_state": "green"},
                "ADDITIONAL_PROPERTY",
            ),
        ]

        for evidence_changes, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                request = fixture_request("sdk-validation-initial.json")
                request["automated_evidence"][0].update(evidence_changes)

                result = VALIDATOR.validate_payload(request)

                self.assertFalse(result["valid"])
                self.assertIn(expected_code, {error["code"] for error in result["errors"]})

    def test_rejects_the_authoritative_secret_fixture_without_echoing_the_value(self) -> None:
        request = fixture_request("secret-rejection.json")
        synthetic_secret = "ghp_FAKE00000000000000000000000000000000"

        result = VALIDATOR.validate_payload(request)
        encoded = json.dumps(result)

        self.assertFalse(result["valid"])
        self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
        self.assertNotIn(synthetic_secret, encoded)

    def test_rejects_a_raw_acceptora_bearer_token_without_echoing_the_value(self) -> None:
        request = fixture_request("sdk-validation-initial.json")
        synthetic_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)
        request["implementation_change_summary"] = f"Unsafe credential: {synthetic_token}"

        result = VALIDATOR.validate_payload(request)
        encoded = json.dumps(result)

        self.assertFalse(result["valid"])
        self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
        self.assertNotIn(synthetic_token, encoded)

    def test_rejects_acceptora_token_substrings_without_boundary_bypasses(self) -> None:
        synthetic_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("B" * 48)

        for candidate in (f"prefix{synthetic_token}", f"{synthetic_token}suffix", f"prefix{synthetic_token}suffix"):
            with self.subTest(position=candidate.startswith("prefix"), suffix=candidate.endswith("suffix")):
                request = fixture_request("sdk-validation-initial.json")
                request["implementation_change_summary"] = candidate
                result = VALIDATOR.validate_payload(request)

                self.assertFalse(result["valid"])
                self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
                self.assertNotIn(synthetic_token, json.dumps(result))

    def test_rejects_sensitive_credential_labels_and_encrypted_private_keys(self) -> None:
        metadata_cases = {
            "password": "correct-horse-battery-staple",
            "passphrase": "release archive password",
            "api_key": "abcdefghijklmnopqrstuvwx",
            "api-key": "abcdefghijklmnopqrstuvwx",
            "apiKey": "abcdefghijklmnopqrstuvwx",
            "access_token": "opaquecredentialvalue12345",
            "clientSecret": "opaquecredentialvalue12345",
            "Authorization": "Basic dXNlcjpwYXNz",
            "session_cookie": "session-cookie-value",
            "credential": "opaquecredentialvalue12345",
        }

        for key, value in metadata_cases.items():
            with self.subTest(key=key):
                request = fixture_request("sdk-validation-initial.json")
                request["source_descriptor"].setdefault("metadata", {})[key] = value
                result = VALIDATOR.validate_payload(request)
                encoded = json.dumps(result)

                self.assertFalse(result["valid"])
                self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
                self.assertNotIn(value, encoded)

        private_key = "-----BEGIN ENCRYPTED PRIVATE KEY-----\nsynthetic\n-----END ENCRYPTED PRIVATE KEY-----"
        request = fixture_request("sdk-validation-initial.json")
        request["implementation_change_summary"] = private_key
        result = VALIDATOR.validate_payload(request)
        self.assertFalse(result["valid"])
        self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
        self.assertNotIn(private_key, json.dumps(result))

    def test_rejects_a_secret_in_a_permitted_metadata_key_without_echoing_it(self) -> None:
        request = fixture_request("sdk-validation-initial.json")
        synthetic_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("K" * 48)
        request["source_descriptor"].setdefault("metadata", {})[synthetic_token] = "safe value"

        result = VALIDATOR.validate_payload(request)
        encoded = json.dumps(result)

        self.assertFalse(result["valid"])
        self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
        self.assertNotIn(synthetic_token, encoded)

    def test_placeholder_wrappers_do_not_hide_real_tokens(self) -> None:
        synthetic_token = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("P" * 48)

        for wrapped in (f"[REDACTED {synthetic_token}]", f"<{synthetic_token}>"):
            with self.subTest(wrapper=wrapped[0]):
                request = fixture_request("sdk-validation-initial.json")
                request["source_descriptor"].setdefault("metadata", {})["note"] = wrapped
                result = VALIDATOR.validate_payload(request)
                encoded = json.dumps(result)

                self.assertFalse(result["valid"])
                self.assertIn("SECRET_REJECTED", {error["code"] for error in result["errors"]})
                self.assertNotIn(synthetic_token, encoded)

    def test_symbolic_environment_placeholder_remains_safe(self) -> None:
        request = fixture_request("sdk-validation-initial.json")
        metadata = request["source_descriptor"].setdefault("metadata", {})
        metadata["token_reference"] = "${ACCEPTORA_AGENT_TOKEN}"
        metadata["Authorization"] = "Bearer ${ACCEPTORA_AGENT_TOKEN}"
        metadata["clientSecret"] = "[REDACTED]"

        result = VALIDATOR.validate_payload(request)

        self.assertTrue(result["valid"], result.get("errors"))

    def test_rejects_the_authoritative_uncovered_surface_fixture(self) -> None:
        result = VALIDATOR.validate_payload(fixture_request("uncovered-surface.json"))

        self.assertFalse(result["valid"])
        errors = [error for error in result["errors"] if error["code"] == "UNCOVERED_CHANGED_SURFACE"]
        self.assertEqual(1, len(errors))
        self.assertIn("route:POST:/api/v1/subscriptions/cancel", errors[0]["message"])

    def test_rejects_the_legacy_nested_items_and_top_level_versions_shape(self) -> None:
        payload = fixture_request("sdk-validation-initial.json")
        payload["skill_version"] = payload["versions"]["skill_version"]
        payload["contract_version"] = payload["versions"]["contract_version"]
        payload["sections"][0]["items"] = [payload["items"][0]]
        payload.pop("items")

        result = VALIDATOR.validate_payload(payload)
        codes = {error["code"] for error in result["errors"]}

        self.assertFalse(result["valid"])
        self.assertIn("ADDITIONAL_PROPERTY", codes)
        self.assertIn("ITEMS_REQUIRED", codes)

    def test_resolution_public_id_matches_the_authoritative_common_schema(self) -> None:
        payload = fixture_request("sdk-validation-initial.json")
        payload["addressed_resolution_ids"] = ["resolution_01J00000000000000000000001"]

        valid = VALIDATOR.validate_payload(payload)

        self.assertTrue(valid["valid"], valid.get("errors"))

        payload["addressed_resolution_ids"] = ["res_01J00000000000000000000001"]
        invalid = VALIDATOR.validate_payload(payload)
        self.assertIn("INVALID_RESOLUTION_ID", {error["code"] for error in invalid["errors"]})


if __name__ == "__main__":
    unittest.main()
