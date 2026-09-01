from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class NpmCliTest(unittest.TestCase):
    def test_node_cli_functional_suite(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to test the npm installer")
        package_root = Path(__file__).resolve().parents[1]

        process = subprocess.run(
            [str(Path(node).resolve()), "--test", "tests/npm-cli.test.mjs"],
            cwd=package_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

        self.assertEqual(0, process.returncode, process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
