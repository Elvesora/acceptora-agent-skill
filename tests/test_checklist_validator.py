from __future__ import annotations

import importlib.util
import json
import sys
import unittest
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


class ChecklistValidatorContractTest(unittest.TestCase):
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
