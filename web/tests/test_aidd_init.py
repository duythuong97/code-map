"""aidd/init.py installs the AIDD kit into a source repository without clobbering what is there."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run(*args: str) -> str:
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "aidd" / "init.py"), *args],
                          capture_output=True, text=True, check=True, cwd=PROJECT_ROOT).stdout


class AiddInitTests(unittest.TestCase):
    def test_installs_kit_and_keeps_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "order-api")
            target.mkdir()
            (target / "AGENTS.md").write_text("team rules\n", encoding="utf-8")
            output = run(str(target), "--server-url", "http://codemap:8000/code-map/mcp", "--codemap-repository", "acme/code-map")
            self.assertIn("kept         AGENTS.md", output)
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "team rules\n")
            mcp = json.loads((target / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(mcp["servers"]["codemap"]["url"], "http://codemap:8000/code-map/mcp")
            claude = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(claude["mcpServers"]["codemap"]["type"], "http")
            prompt = (target / ".github" / "prompts" / "codemap-impact.prompt.md").read_text(encoding="utf-8")
            self.assertTrue(prompt.startswith("---\ndescription:"))
            self.assertIn("${input:change:", prompt)
            self.assertIn("{kind: table, name: ORDER_HEADER, action: write}", prompt)
            workflow = (target / ".github" / "workflows" / "codemap-pr.yml").read_text(encoding="utf-8")
            self.assertIn("CODEMAP_TOOL_REPOSITORY: acme/code-map", workflow)
            config = json.loads((target / ".codemap" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["sources"][0]["name"], "order-api")
            for relative in ("docs/changes/_template/impact.yaml", ".github/agents/challenger.agent.md", "CLAUDE.md"):
                self.assertTrue((target / relative).is_file(), relative)
            self.assertIn("wrote        AGENTS.md", run(str(target), "--force"))


if __name__ == "__main__":
    unittest.main()
