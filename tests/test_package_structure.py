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
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/actions/prepare-python/action.yml",
            ".github/workflows/release.yml",
            ".github/workflows/tests.yml",
            ".gitattributes",
            ".gitignore",
            "CHANGELOG.md",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "SETUP.md",
            "SETUP-CODEX.md",
            "SETUP-CLAUDE-CODE.md",
            "SETUP-GEMINI-CLI.md",
            "GETTING-STARTED.md",
            "SUPPORT.md",
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

    def test_readme_identifies_the_standalone_skill_without_claiming_plugin_packaging(self) -> None:
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("standalone agent skill", readme)
        self.assertIn("It is not a Codex plugin.", readme)

    def test_only_changelog_contains_one_top_unreleased_section(self) -> None:
        for document in PACKAGE_ROOT.rglob("*.md"):
            body = document.read_text(encoding="utf-8")

            if document == PACKAGE_ROOT / "CHANGELOG.md":
                headings = re.findall(r"^## .+$", body, re.MULTILINE)
                self.assertEqual("## [Unreleased]", headings[0], document)
                self.assertEqual(1, headings.count("## [Unreleased]"), document)
                continue

            self.assertNotIn("[Unreleased]", body, document)

    def test_openai_interface_metadata_matches_the_skill_identity(self) -> None:
        metadata = (PACKAGE_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn('display_name: "Acceptora"', metadata)
        self.assertIn('short_description: "Verify changes in any software stack"', metadata)
        self.assertIn("Use $acceptora", metadata)

    def test_target_project_defaults_do_not_assume_a_language_or_framework(self) -> None:
        project = json.loads((PACKAGE_ROOT / "config" / "project.example.json").read_text(encoding="utf-8"))
        skill = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")

        self.assertEqual([], project["ignored_paths"])
        self.assertIn("regardless of programming language, framework, build system", skill)
        self.assertIn("Do not assume Laravel, PHP, Composer", skill)
        self.assertIn("not target-application dependencies", setup)
        self.assertLessEqual(len(skill.splitlines()), 500)

    def test_getting_started_teaches_the_ten_minute_path_without_replacing_setup(self) -> None:
        getting_started = (PACKAGE_ROOT / "GETTING-STARTED.md").read_text(encoding="utf-8")
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        contributing = (PACKAGE_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

        self.assertIn("What you'll have", getting_started)
        self.assertIn("ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>", getting_started)
        self.assertIn("different projects use different names", getting_started)
        self.assertIn("rolled back independently", setup)
        self.assertIn("onboarding", getting_started.casefold())
        self.assertIn("plan SHA-256", getting_started)
        self.assertIn("$acceptora", getting_started)
        self.assertIn("$acceptora init", getting_started)
        self.assertIn("$acceptora doctor", getting_started)
        self.assertIn("Fresh-clone `https://github.com/Elvesora/acceptora-agent-skill`", getting_started)
        self.assertNotIn("ZIP from that onboarding page", getting_started)
        self.assertNotIn("$verify-generated-work", getting_started)
        self.assertNotIn("extracted `verify-generated-work`", getting_started)
        self.assertIn("outside the target worktree", getting_started)
        self.assertIn("[SETUP.md](SETUP.md)", getting_started)
        self.assertIn("--format text", getting_started)
        self.assertIn("three supported `--client` values explicitly", getting_started)
        self.assertIn("do not create or apply an `antigravity-cli` plan", getting_started)
        self.assertNotIn("npx ", getting_started)
        self.assertNotIn("npm i", getting_started)
        self.assertNotRegex(getting_started, r"extract(?:ed)? to (?:your )?project root")

        self.assertIn("## Coding-agent install or update", setup)
        self.assertIn("reread the selected client setup file and `SETUP.md`", setup)
        self.assertIn("Treat the Git worktree where the prompt was submitted as the target", setup)
        self.assertIn("exact `plan_sha256`", setup)
        self.assertIn("`--format text`", setup)
        self.assertIn("Pass `--client` explicitly in every reviewed installation", setup)
        self.assertIn(
            "not supported for new installation, reconnection, upgrade, or project work",
            setup,
        )
        self.assertIn("[GETTING-STARTED.md](GETTING-STARTED.md)", readme)
        self.assertIn("references/init.md", contributing)
        self.assertIn("references/doctor.md", contributing)
        self.assertIn("completion workflow", contributing)

    def test_client_setup_entrypoints_fix_the_client_and_delegate_the_shared_lifecycle(self) -> None:
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")
        entrypoints = {
            "SETUP-CODEX.md": "codex",
            "SETUP-CLAUDE-CODE.md": "claude-code",
            "SETUP-GEMINI-CLI.md": "gemini-cli",
        }

        for filename, client in entrypoints.items():
            with self.subTest(filename=filename):
                body = (PACKAGE_ROOT / filename).read_text(encoding="utf-8")
                self.assertIn("current Git worktree", body)
                self.assertIn(f"`{client}` as the client", body)
                self.assertIn("Fresh-clone only the `main` branch", body)
                self.assertIn("reread this file and `SETUP.md` completely", body)
                self.assertIn("Coding-agent install or update", body)
                self.assertIn(filename, setup)

        self.assertIn("https://www.acceptora.com", setup)
        self.assertIn("ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>", setup)
        self.assertIn("derive `proj_<ULID>` without reading or printing the credential value", setup)
        self.assertIn("If more than one exists", setup)
        self.assertIn("Do not guess", setup)
        self.assertNotIn("`client`, `project_id`, and `acceptora_origin`", setup)

    def test_skill_routes_init_doctor_and_default_completion(self) -> None:
        skill = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        init = (PACKAGE_ROOT / "references" / "init.md").read_text(encoding="utf-8")
        doctor = (PACKAGE_ROOT / "references" / "doctor.md").read_text(encoding="utf-8")

        self.assertIn("[init](references/init.md)", skill)
        self.assertIn("[doctor](references/doctor.md)", skill)
        self.assertIn("completion hook", skill.casefold())
        self.assertIn("reconcile_checklist", skill)
        self.assertIn("same validated request object", skill)
        self.assertIn("Do not reconstruct it", skill)
        self.assertIn("Never show a command menu instead of reconciling", skill)
        self.assertNotIn("npx ", skill)

        mcp_contract = (PACKAGE_ROOT / "references" / "mcp-tool-contract.md").read_text(encoding="utf-8")
        self.assertIn("all 18 required root fields", mcp_contract)
        self.assertIn("Preserve required arrays even when they are empty", mcp_contract)

        self.assertIn("[SETUP.md](../SETUP.md)", init)
        self.assertIn("Coding-agent install or update", init)
        self.assertIn("exact `plan_sha256`", init)
        self.assertIn("--confirm-connection", init)
        self.assertIn("Never approve a plan digest", init)
        self.assertIn("--format text --output", init)
        self.assertIn("outside the target", init)
        self.assertNotIn("npx ", init)

        self.assertIn("health_check.py", doctor)
        self.assertIn("omit `--confirm-connection`", doctor)
        self.assertIn("status --client", doctor)
        self.assertIn("Never print", doctor)
        self.assertIn("ACCEPTORA_AGENT_TOKEN_PROJ_<ULID>", doctor)
        self.assertIn("$acceptora init", doctor)
        self.assertNotIn("$verify-generated-work", doctor)
        self.assertNotIn("npx ", doctor)
        self.assertNotIn("--accept-plan-sha256", doctor)

    def test_pattern_anchor_guidance_uses_only_contract_prefixes(self) -> None:
        common_schema = json.loads(
            (PACKAGE_ROOT / "tests" / "fixtures" / "contracts" / "v1" / "common.schema.json").read_text(
                encoding="utf-8"
            )
        )
        anchor_pattern = common_schema["$defs"]["coverageAnchor"]["pattern"]
        prefix_group = re.match(r"^\^\((?P<prefixes>[^)]+)\):", anchor_pattern)
        self.assertIsNotNone(prefix_group)
        assert prefix_group is not None
        allowed = set(prefix_group.group("prefixes").split("|"))

        documented: set[str] = set()
        for document in sorted((PACKAGE_ROOT / "references" / "patterns").glob("*.md")):
            body = document.read_text(encoding="utf-8")
            documented.update(re.findall(r"`([a-z]+):(?:[^`]*)`", body))

        self.assertTrue(documented)
        self.assertEqual(set(), documented - allowed)
        self.assertEqual(allowed, documented)

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

    def test_active_bug_report_placeholder_matches_the_package_skill_version(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8"))
        bug_report = (PACKAGE_ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(
            encoding="utf-8"
        )
        package_version_block = re.search(
            r"(?ms)^\s+- type: input\s*\n\s+id: package_version\s*\n(?P<body>.*?)(?=^\s+- type:|\Z)",
            bug_report,
        )

        self.assertIsNotNone(package_version_block)
        assert package_version_block is not None
        placeholder = re.search(
            r"(?m)^\s+placeholder:\s*(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\s*$",
            package_version_block.group("body"),
        )

        self.assertIsNotNone(placeholder)
        assert placeholder is not None
        self.assertEqual(manifest["skill"]["version"], placeholder.group("version"))

    def test_documented_isolated_python_commands_also_disable_bytecode(self) -> None:
        documents = [
            *PACKAGE_ROOT.glob("*.md"),
            *(PACKAGE_ROOT / "references").rglob("*.md"),
            *(PACKAGE_ROOT / "snippets").glob("*.block"),
            PACKAGE_ROOT / ".github" / "actions" / "prepare-python" / "action.yml",
        ]

        for path in sorted(documents):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if " -I " not in line:
                    continue
                with self.subTest(path=path.relative_to(PACKAGE_ROOT), line=line_number):
                    self.assertIn(" -B -I ", line)

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

        for relative in ("SKILL.md", "references/mcp-tool-contract.md", "references/rest-api-contract.md"):
            document = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(document=relative):
                for projection in expected_include:
                    self.assertIn(projection, document)

        for relative in (
            "SKILL.md",
            "references/checklist-writing-rules.md",
            "references/identity-and-reconciliation.md",
            "references/mcp-tool-contract.md",
            "references/rest-api-contract.md",
        ):
            document = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(definition_document=relative):
                self.assertIn("checklist_sections", document)
                self.assertIn("target", document)
                self.assertIn("test_data", document)

    def test_manifest_repository_locator_is_reused_for_every_source_bound_operation(self) -> None:
        skill = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        contract = (PACKAGE_ROOT / "references" / "mcp-tool-contract.md").read_text(encoding="utf-8")
        identity = (PACKAGE_ROOT / "references" / "identity-and-reconciliation.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("exact emitted `manifest.repository`", skill)
        self.assertIn("Never reconstruct it from the current working directory", skill)
        for operation in (
            "resolve_feature",
            "reconcile_checklist",
            "address_feedback",
            "record_verification_exception",
            "check_completion_gate",
        ):
            with self.subTest(operation=operation):
                self.assertIn(operation, skill)
                self.assertIn(operation, contract)
        self.assertIn("exact `repository` value", identity)
        self.assertIn("Windows drive-absolute and UNC remotes", identity)

    def test_distribution_metadata_and_setup_keep_installation_bound_to_production_main(self) -> None:
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        security = (PACKAGE_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        skill = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        getting_started = (PACKAGE_ROOT / "GETTING-STARTED.md").read_text(encoding="utf-8")
        init_reference = (PACKAGE_ROOT / "references" / "init.md").read_text(encoding="utf-8")
        client_setups = [
            (PACKAGE_ROOT / filename).read_text(encoding="utf-8")
            for filename in ("SETUP-CODEX.md", "SETUP-CLAUDE-CODE.md", "SETUP-GEMINI-CLI.md")
        ]
        contributing = (PACKAGE_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        changelog = (PACKAGE_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        pull_request_template = (PACKAGE_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )
        bug_report_template = (PACKAGE_ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(
            encoding="utf-8"
        )
        bundle_builder = (PACKAGE_ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
        agent_procedure = setup.split("## Coding-agent install or update", 1)[1].split("## Boundary", 1)[0]
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
        self.assertIn("## [1.2.3] - 2026-08-29", changelog)
        self.assertIn("git clone --depth 1 --branch main --single-branch", setup)
        self.assertIn("https://github.com/Elvesora/acceptora-agent-skill", setup)
        self.assertIn("## Coding-agent install or update", setup)
        self.assertIn("selected setup filename fixes the explicit installer client", setup)
        self.assertIn("fixed package configuration, not a prompt input", setup)
        self.assertIn("For a new installation", setup)
        self.assertIn("For an update", setup)
        self.assertIn("exact `plan_sha256`", setup)
        self.assertIn("exact `rollback_plan_sha256`", setup)
        self.assertIn("full source commit", setup)
        self.assertIn("`SessionStart` update notice", agent_procedure)
        self.assertIn("<runtime-root>/state/skill-update.json", agent_procedure)
        self.assertIn("<runtime-root>/package/scripts/install.py", agent_procedure)
        self.assertIn("<runtime-root>/install-receipt.json", agent_procedure)
        self.assertNotIn("Post-install and upgrades", agent_procedure)
        self.assertIn("Do not make unrelated project changes", setup)
        for installation_document in (setup, security, skill, getting_started, init_reference, *client_setups):
            with self.subTest(document=installation_document[:40]):
                self.assertNotIn("https://www.acceptora.com/agent-skill/", installation_document)
                self.assertNotIn("application-hosted ZIP", installation_document)
                self.assertNotIn("intact ZIP", installation_document)
                self.assertNotIn("verified-ZIP installation", installation_document)

        self.assertIn("only supported acquisition path", setup)
        self.assertIn("package-owned ZIP builder", readme)
        self.assertIn("GETTING-STARTED.md", readme)
        self.assertNotIn("Acceptora-hosted ZIP", security)
        self.assertIn("Application-hosted mirrors", security)
        self.assertIn("deterministic ZIP bundle and embedded provenance", contributing)
        self.assertIn("extracted ZIP passes installer plan/apply tests", pull_request_template)
        self.assertIn("Release bundle builder or generated artifact", bug_report_template)
        self.assertIn("Build deterministic public bundles", bundle_builder)
        self.assertIn("acceptora-agent-skill-provenance.json", bundle_builder)

    def test_codex_minimum_matches_the_generated_approval_policy(self) -> None:
        registry = json.loads((PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8"))
        clients = {profile["id"]: profile for profile in registry["clients"]}
        template = (PACKAGE_ROOT / "config" / "codex-mcp.example.toml").read_text(encoding="utf-8")
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")

        self.assertEqual("codex-cli 0.144.0", clients["codex"]["minimum_build"])
        self.assertEqual("codex-cli 0.150.1", clients["codex"]["reference_build"])
        self.assertIn('default_tools_approval_mode = "writes"', template)
        self.assertIn("Codex CLI 0.144.0 or newer", readme)
        self.assertIn("Codex CLI 0.144.0 or newer", setup)

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

    def test_dated_client_capability_matrix_matches_the_registry(self) -> None:
        registry = json.loads((PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8"))
        matrix = (PACKAGE_ROOT / "references" / "client-capabilities.md").read_text(encoding="utf-8")
        normalized_matrix = matrix.replace("`", "")

        self.assertIn(f"Capabilities reviewed: **{registry['capabilities_reviewed_on']}**", matrix)
        self.assertIn("configuration-review baselines, not claims about each provider's latest release", matrix)
        self.assertIn("`config/client-profiles.json`", matrix)
        for profile in registry["clients"]:
            with self.subTest(client=profile["id"]):
                self.assertIn(profile["display_name"], matrix)
                self.assertIn(profile["reference_build"], matrix)
                for check in profile["discovery_checks"]:
                    self.assertIn(check, normalized_matrix)
                for url in profile["official_docs"].values():
                    self.assertIn(url, matrix)
                if not profile["install_supported"]:
                    self.assertIn(profile["unsupported_reason"], matrix)

    def test_gemini_reference_build_supports_the_documented_reload_checks(self) -> None:
        registry = json.loads((PACKAGE_ROOT / "config" / "client-profiles.json").read_text(encoding="utf-8"))
        gemini = next(profile for profile in registry["clients"] if profile["id"] == "gemini-cli")

        self.assertEqual("0.57.0", gemini["reference_build"])
        self.assertIn("/skills reload", gemini["discovery_checks"])
        self.assertIn("/mcp reload or gemini mcp list", gemini["discovery_checks"])

    def test_local_markdown_links_stay_inside_the_release_package(self) -> None:
        for document in PACKAGE_ROOT.rglob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                if target.startswith(("https://", "http://", "#", "mailto:")):
                    continue

                relative = target.split("#", 1)[0]
                resolved = (document.parent / relative).resolve(strict=False)
                resolved.relative_to(PACKAGE_ROOT)
                self.assertTrue(resolved.exists(), f"Broken package link in {document}: {target}")


if __name__ == "__main__":
    unittest.main()
