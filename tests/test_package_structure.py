from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class PackageStructureTest(unittest.TestCase):
    def test_skill_frontmatter_matches_the_agent_skills_contract(self) -> None:
        body = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", body, re.DOTALL)

        self.assertIsNotNone(match)
        assert match is not None

        metadata: dict[str, str] = {}
        for line in match.group("frontmatter").splitlines():
            key, separator, value = line.partition(":")
            self.assertEqual(":", separator)
            metadata[key.strip()] = value.strip()

        self.assertEqual({"name", "description"}, set(metadata))
        self.assertEqual("acceptora", metadata["name"])
        self.assertRegex(metadata["name"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertGreater(len(metadata["description"]), 20)
        self.assertLessEqual(len(metadata["description"]), 1024)

    def test_required_cross_client_assets_and_license_are_present(self) -> None:
        required = {
            ".github/dependabot.yml",
            ".github/actions/prepare-python/action.yml",
            ".github/workflows/release.yml",
            ".github/workflows/tests.yml",
            ".gitattributes",
            ".gitignore",
            "LICENSE",
            "SETUP.md",
            "SETUP-CODEX.md",
            "SETUP-CLAUDE-CODE.md",
            "SETUP-GEMINI-CLI.md",
            "agents/openai.yaml",
            "adapters/codex/hooks.json.example",
            "adapters/claude/settings.json.example",
            "adapters/claude/settings.windows.json.example",
            "adapters/gemini/hooks.json.example",
            "adapters/antigravity/hooks.json.example",
            "adapters/antigravity/antigravity_event.py",
            "adapters/antigravity/task_start.py",
            "adapters/antigravity/stop.py",
            "adapters/antigravity/mcp_stdio_bridge.py",
            "config/codex-mcp.example.toml",
            "config/claude-mcp.example.json",
            "config/gemini-mcp.example.json",
            "config/antigravity-mcp.example.json",
            "config/client-profiles.json",
            "config/package-manifest.json",
            "references/client-capabilities.md",
            "references/init.md",
            "references/doctor.md",
            "snippets/AGENTS.md.block",
            "snippets/CLAUDE.md.block",
            "snippets/GEMINI.md.block",
            "scripts/install.py",
            "scripts/build_release.py",
        }

        self.assertEqual([], sorted(path for path in required if not (PACKAGE_ROOT / path).is_file()))

    def test_openai_interface_metadata_matches_the_skill_identity(self) -> None:
        metadata = (PACKAGE_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn('display_name: "Acceptora"', metadata)
        self.assertIn('short_description: "Verify changes in any software stack"', metadata)
        self.assertIn("Use $acceptora", metadata)

    def test_target_project_defaults_do_not_ignore_paths(self) -> None:
        project = json.loads((PACKAGE_ROOT / "config" / "project.example.json").read_text(encoding="utf-8"))

        self.assertEqual([], project["ignored_paths"])

    def test_python_cache_files_are_ignored(self) -> None:
        ignored = {
            line.strip()
            for line in (PACKAGE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue({"__pycache__/", "*.py[cod]", ".pytest_cache/"}.issubset(ignored))

    def test_checkout_contains_no_python_bytecode(self) -> None:
        bytecode_paths = sorted(
            path.relative_to(PACKAGE_ROOT).as_posix()
            for path in PACKAGE_ROOT.rglob("*")
            if path.name == "__pycache__" or path.suffix.lower() in {".pyc", ".pyo"}
        )

        self.assertEqual([], bytecode_paths)

    def test_current_public_fixtures_match_the_package_skill_version(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8"))
        expected = manifest["skill"]["version"]
        fixture_names = (
            "hook-gate-payload.json",
            "sdk-validation-initial.json",
            "sdk-validation-revision-2.json",
            "secret-rejection.json",
            "uncovered-surface.json",
        )

        for fixture_name in fixture_names:
            with self.subTest(fixture=fixture_name):
                fixture = json.loads(
                    (PACKAGE_ROOT / "tests" / "fixtures" / fixture_name).read_text(encoding="utf-8")
                )
                versions = fixture.get("versions", fixture.get("request", {}).get("versions"))
                self.assertIsInstance(versions, dict)
                assert isinstance(versions, dict)
                self.assertEqual(expected, versions.get("skill_version"))

    def test_github_workflows_use_an_owner_controlled_real_python_copy(self) -> None:
        workflows = {
            name: (PACKAGE_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            for name in ("tests.yml", "release.yml")
        }
        action = (PACKAGE_ROOT / ".github" / "actions" / "prepare-python" / "action.yml").read_text(
            encoding="utf-8"
        )

        for name, workflow in workflows.items():
            with self.subTest(workflow=name):
                self.assertIn("Prepare owner-only Python runtime", workflow)
                self.assertEqual(1, workflow.count("uses: ./.github/actions/prepare-python"))
                self.assertIn('"$SAFE_PYTHON" -m unittest', workflow)
                self.assertNotIn("--allow-dirty", workflow)
                self.assertIn("scripts/build_release.py", workflow)
                self.assertNotIn("gh release create", workflow)
                self.assertNotRegex(workflow, r"chmod[^\n]*(?:hostedtoolcache|source_python)")

        self.assertIn('"$SAFE_PYTHON" -m compileall', workflows["tests.yml"])
        self.assertIn("PYTHONPYCACHEPREFIX: ${{ runner.temp }}/acceptora-pycache-", workflows["tests.yml"])
        self.assertIn("Reject Python bytecode in the checkout", workflows["tests.yml"])
        self.assertIn(r"find . \( -type d -name __pycache__", workflows["tests.yml"])
        self.assertIn("workflow_dispatch", workflows["release.yml"])
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', workflows["release.yml"])
        self.assertIn("sha256sum --check SHA256SUMS", workflows["release.yml"])
        self.assertIn('unzip -t', workflows["release.yml"])

        self.assertIn("os.path.realpath(sys.executable)", action)
        self.assertIn("test ! -L \"$source_python\"", action)
        self.assertIn("= '7f454c46'", action)
        self.assertIn('install -d -m 700 -- "$safe_root"', action)
        self.assertIn('install -m 700 -- "$source_python" "$safe_python"', action)
        self.assertIn('cmp -s -- "$source_python" "$safe_python"', action)
        self.assertIn('test "$(realpath -e -- "$safe_python")" = "$safe_python"', action)
        self.assertIn("8#$mode & 0022", action)
        self.assertIn("SAFE_PYTHON=%s", action)
        self.assertNotRegex(action, r"chmod[^\n]*(?:hostedtoolcache|source_python)")

        installer = (PACKAGE_ROOT / "scripts" / "install.py").read_text(encoding="utf-8")
        self.assertIn("_assert_safe_executable_ancestor_chain(resolved, label)", installer)
        self.assertNotIn("GITHUB_ACTIONS", installer)

    def test_required_ci_builds_publishable_bundle_only_on_main(self) -> None:
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        marker = "      - name: Build the production-main ZIP bundle\n"

        self.assertIn("\n  push:\n", workflow)
        self.assertIn("\n  pull_request:\n", workflow)
        self.assertEqual(1, workflow.count(marker))
        production_step = workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("if: matrix.python == '3.14' && github.ref == 'refs/heads/main'", production_step)
        self.assertIn('--source-commit "$GITHUB_SHA"', production_step)
        self.assertNotIn("--allow-dirty", production_step)
        self.assertNotIn("UNVERSIONED", production_step)

    def test_public_fixtures_use_neutral_identifiers(self) -> None:
        fixtures = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((PACKAGE_ROOT / "tests" / "fixtures").rglob("*.json"))
        )
        normalized = fixtures.casefold()

        for forbidden in (
            "sor" + "yxa",
            "acceptora." + "inter" + "nal",
            "acceptora." + "lo" + "cal",
            "example/" + "inter" + "nal",
            "inter" + "nal skill evaluation",
            "inter" + "nal evaluation scenario",
            "lara" + "gon",
        ):
            self.assertNotIn(forbidden, normalized)
        self.assertNotRegex(fixtures, r"[A-Za-z]:\\")

    def test_contract_schema_identifiers_use_the_public_origin(self) -> None:
        contract_root = PACKAGE_ROOT / "tests" / "fixtures" / "contracts" / "v1"

        for path in sorted(contract_root.rglob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if "$id" not in document:
                continue
            self.assertTrue(document["$id"].startswith("https://www.acceptora.com/contracts/v1/"), path)

    def test_feature_context_include_projection_covers_provider_evidence(self) -> None:
        expected_include = [
            "checklist_definitions",
            "decisions",
            "comments",
            "attachments",
            "history_summary",
            "source_revisions",
            "automated_evidence",
        ]
        schema = json.loads(
            (
                PACKAGE_ROOT
                / "tests"
                / "fixtures"
                / "contracts"
                / "v1"
                / "tools"
                / "get-feature-context.input.schema.json"
            ).read_text(encoding="utf-8")
        )
        include = schema["properties"]["include"]

        self.assertEqual(7, include["maxItems"])
        self.assertTrue(include["uniqueItems"])
        self.assertEqual(expected_include, include["items"]["enum"])

        output_schema = json.loads(
            (
                PACKAGE_ROOT
                / "tests"
                / "fixtures"
                / "contracts"
                / "v1"
                / "tools"
                / "get-feature-context.output.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("#/$defs/checklistSections", output_schema["properties"]["checklist_sections"]["$ref"])
        item_properties = output_schema["$defs"]["itemContexts"]["items"]["properties"]
        self.assertEqual("#/$defs/checklistItemDefinition", item_properties["definition"]["$ref"])
        definition = output_schema["$defs"]["checklistItemDefinition"]
        self.assertTrue({"target", "test_data"}.issubset(definition["required"]))
        self.assertTrue({"target", "test_data"}.issubset(definition["properties"]))

    def test_distribution_manifest_is_bound_to_production_main(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(
            {
                "repository_url": "https://github.com/Elvesora/acceptora-agent-skill",
                "branch": "main",
            },
            manifest["distribution"],
        )
        self.assertEqual("acceptora", manifest["skill"]["name"])
        self.assertEqual("1.2.3", manifest["skill"]["version"])
        self.assertEqual("1.0.0", manifest["integration"]["version"])
        self.assertEqual("1.0.0", manifest["contract"]["version"])
        self.assertEqual("1.0.0", manifest["server"]["version"])

    def test_codex_profile_and_template_match_the_approval_policy(self) -> None:
        registry = json.loads((PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8"))
        clients = {profile["id"]: profile for profile in registry["clients"]}
        template = (PACKAGE_ROOT / "config" / "codex-mcp.example.toml").read_text(encoding="utf-8")

        self.assertEqual("codex-cli 0.144.0", clients["codex"]["minimum_build"])
        self.assertEqual("codex-cli 0.150.1", clients["codex"]["reference_build"])
        self.assertIn('default_tools_approval_mode = "writes"', template)

    def test_client_provider_registry_is_canonical_and_matches_known_profiles(self) -> None:
        registry = json.loads((PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8"))
        package_manifest = json.loads(
            (PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8")
        )
        profiles = registry["clients"]

        self.assertEqual(1, registry["schema_version"])
        self.assertEqual("2026-08-29", registry["capabilities_reviewed_on"])
        self.assertEqual(
            ["codex", "claude-code", "gemini-cli", "antigravity-cli"],
            [profile["id"] for profile in profiles],
        )
        self.assertTrue(
            {"supported_clients", "reference_client_builds", "minimum_client_builds"}.isdisjoint(package_manifest)
        )
        self.assertEqual(
            ["codex", "claude-code", "gemini-cli"],
            [profile["id"] for profile in profiles if profile["install_supported"]],
        )

        expected = {
            "codex": {
                "project_layout": {
                    "skill_directory": ".agents/skills/acceptora",
                    "instruction_file": "AGENTS.md",
                    "instruction_source": "snippets/AGENTS.md.block",
                },
                "default_directory": ".codex",
                "settings": {"base": "client_config", "path": "hooks.json"},
                "mcp": {"base": "client_config", "path": "config.toml"},
                "events": {"SessionStart", "UserPromptSubmit", "Stop"},
            },
            "claude-code": {
                "project_layout": {
                    "skill_directory": ".claude/skills/acceptora",
                    "instruction_file": "CLAUDE.md",
                    "instruction_source": "snippets/CLAUDE.md.block",
                },
                "default_directory": ".claude",
                "settings": {"base": "client_config", "path": "settings.json"},
                "mcp": {"base": "client_config_parent", "path": ".claude.json"},
                "events": {"SessionStart", "UserPromptSubmit", "Stop"},
            },
            "gemini-cli": {
                "project_layout": {
                    "skill_directory": ".gemini/skills/acceptora",
                    "instruction_file": "GEMINI.md",
                    "instruction_source": "snippets/GEMINI.md.block",
                },
                "default_directory": ".gemini",
                "settings": {"base": "client_config", "path": "settings.json"},
                "mcp": {"base": "client_config", "path": "settings.json"},
                "events": {"SessionStart", "BeforeAgent", "AfterAgent"},
            },
            "antigravity-cli": {
                "project_layout": {
                    "skill_directory": ".agents/skills/acceptora",
                    "instruction_file": "AGENTS.md",
                    "instruction_source": "snippets/AGENTS.md.block",
                },
                "default_directory": ".gemini/config",
                "settings": {"base": "client_config", "path": "hooks.json"},
                "mcp": {"base": "client_config", "path": "mcp_config.json"},
                "events": {"PreInvocation", "Stop"},
            },
        }
        for profile in profiles:
            client = profile["id"]
            with self.subTest(client=client):
                if profile["install_supported"]:
                    self.assertIsNone(profile["unsupported_reason"])
                else:
                    self.assertIsInstance(profile["unsupported_reason"], str)
                    self.assertTrue(profile["unsupported_reason"].strip())
                current = expected[client]
                self.assertEqual(current["project_layout"], profile["project_layout"])
                self.assertEqual(current["default_directory"], profile["user_config"]["default_directory"])
                self.assertEqual(current["settings"], profile["user_config"]["settings"])
                self.assertEqual(current["mcp"], profile["user_config"]["mcp"])
                lifecycle = profile["lifecycle"]
                expected_events = {*lifecycle["baseline_events"], lifecycle["completion_event"]}
                self.assertEqual(current["events"], expected_events)
                self.assertIn(lifecycle["update_check_event"], lifecycle["baseline_events"])

                hook_templates = [profile["templates"]["hooks"]["default"]]
                hook_templates.extend(profile["templates"]["hooks"]["platform_overrides"].values())
                for relative in hook_templates:
                    hook_document = json.loads((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
                    if client == "antigravity-cli":
                        self.assertEqual(["acceptora-target:{{RUNTIME_ID}}"], list(hook_document), relative)
                        hook_events = next(iter(hook_document.values()))
                    else:
                        hook_events = hook_document["hooks"]
                    self.assertEqual(current["events"], set(hook_events), relative)
                    hook_text = json.dumps(hook_document)
                    for adapter in profile["runtime_adapters"]:
                        expected_adapter_path = (
                            f"/trusted_adapters/antigravity/{Path(adapter).name}"
                            if client == "antigravity-cli"
                            else f"/{adapter}"
                        )
                        self.assertIn(expected_adapter_path, hook_text)

                referenced_files = {
                    profile["project_layout"]["instruction_source"],
                    profile["templates"]["mcp"]["path"],
                    *hook_templates,
                    *profile["runtime_adapters"],
                }
                self.assertEqual([], sorted(path for path in referenced_files if not (PACKAGE_ROOT / path).is_file()))

        specification = importlib.util.spec_from_file_location(
            "acceptora_registry_installer",
            PACKAGE_ROOT / "scripts" / "install.py",
        )
        self.assertIsNotNone(specification)
        assert specification is not None and specification.loader is not None
        installer = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(installer)
        self.assertEqual(tuple(expected), installer._client_names())
        for client, current in expected.items():
            with self.subTest(installer_client=client):
                self.assertEqual(
                    {
                        "skill": current["project_layout"]["skill_directory"],
                        "instruction": current["project_layout"]["instruction_file"],
                        "instruction_source": current["project_layout"]["instruction_source"],
                    },
                    installer._client_layout(client),
                )

    def test_gemini_profile_declares_reload_checks(self) -> None:
        registry = json.loads((PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8"))
        gemini = next(profile for profile in registry["clients"] if profile["id"] == "gemini-cli")

        self.assertEqual("0.57.0", gemini["reference_build"])
        self.assertIn("/skills reload", gemini["discovery_checks"])
        self.assertIn("/mcp reload or gemini mcp list", gemini["discovery_checks"])


if __name__ == "__main__":
    unittest.main()
