from __future__ import annotations

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
        self.assertEqual("verify-generated-work", metadata["name"])
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
            "SUPPORT.md",
            "agents/openai.yaml",
            "adapters/codex/hooks.json.example",
            "adapters/claude/settings.json.example",
            "adapters/claude/settings.windows.json.example",
            "adapters/gemini/hooks.json.example",
            "config/codex-mcp.example.toml",
            "config/claude-mcp.example.json",
            "config/gemini-mcp.example.json",
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

    def test_public_documentation_contains_only_released_customer_guidance(self) -> None:
        for document in PACKAGE_ROOT.rglob("*.md"):
            self.assertNotIn("[Unreleased]", document.read_text(encoding="utf-8"), document)

    def test_openai_interface_metadata_matches_the_skill_identity(self) -> None:
        metadata = (PACKAGE_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn('display_name: "Verify Generated Work"', metadata)
        self.assertIn('short_description: "Create durable human acceptance checklists"', metadata)
        self.assertIn("Use $verify-generated-work", metadata)

    def test_python_cache_files_are_ignored(self) -> None:
        ignored = {
            line.strip()
            for line in (PACKAGE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue({"__pycache__/", "*.py[cod]", ".pytest_cache/"}.issubset(ignored))

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
                self.assertIn('"$SAFE_PYTHON" -B -I scripts/build_release.py', workflow)
                self.assertNotIn("--allow-dirty", workflow)
                self.assertNotRegex(workflow, r"chmod[^\n]*(?:hostedtoolcache|source_python)")

        self.assertIn('"$SAFE_PYTHON" -m compileall', workflows["tests.yml"])
        self.assertIn('version="$("$SAFE_PYTHON" -I -c', workflows["release.yml"])

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

    def test_setup_names_release_routes_and_unavailable_behavior(self) -> None:
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")

        self.assertIn("<canonical-origin>/agent-skill/release-manifest.json", setup)
        self.assertIn("<canonical-origin>/agent-skill/verify-generated-work.zip", setup)
        self.assertIn("A `404` or `503`", setup)
        self.assertIn("https://www.acceptora.com/contact", setup)

    def test_codex_minimum_matches_the_generated_approval_policy(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "config" / "package-manifest.json").read_text(encoding="utf-8"))
        template = (PACKAGE_ROOT / "config" / "codex-mcp.example.toml").read_text(encoding="utf-8")
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        setup = (PACKAGE_ROOT / "SETUP.md").read_text(encoding="utf-8")

        self.assertEqual({"codex": "codex-cli 0.144.0"}, manifest["minimum_client_builds"])
        self.assertEqual("codex-cli 0.147.0", manifest["reference_client_builds"]["codex"])
        self.assertIn('default_tools_approval_mode = "writes"', template)
        self.assertIn("Codex CLI 0.144.0 or newer", readme)
        self.assertIn("Codex CLI 0.144.0 or newer", setup)

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
