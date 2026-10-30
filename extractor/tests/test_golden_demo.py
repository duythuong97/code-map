"""Golden snapshots: extracting examples/sample-source must give the committed graph.

Any change to a parser, linker or the demo source that changes nodes, edges or
issues shows up here as a readable diff. After an intended change, refresh the
snapshot with ``CODE_MAP_UPDATE_GOLDEN=1`` and review the diff in the commit.

* ``demo-portable.json``: Angular, PL/SQL and SQL files (Python + Node.js).
* ``demo-full.json``: every demo source, including .NET (needs the .NET SDK).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT_ROOT.parent

from code_tree_exporter.pipeline import _typescript_path, run_pipeline
from code_tree_exporter.snapshot import graph_snapshot, snapshot_difference, write_snapshot

GOLDEN = REPOSITORY / "testdata" / "golden"
EXAMPLES = REPOSITORY / "examples"
UPDATE = os.environ.get("CODE_MAP_UPDATE_GOLDEN") == "1"


def _typescript_available() -> bool:
    if not shutil.which(os.environ.get("CODE_TREE_NODE") or "node"):
        return False
    return Path(_typescript_path(EXAMPLES / "sample-source", ["customer-web/src/app"]), "package.json").is_file()


class GoldenDemoTests(unittest.TestCase):
    def extract(self, keep) -> dict[str, list[str]]:
        config = json.loads((EXAMPLES / "demo-config.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            config.update(
                {
                    "root": str(EXAMPLES / "sample-source"),
                    "output": str(Path(temporary, "out")),
                    "inputData": str(EXAMPLES / "input-data"),
                    "catalog": {"folder": str(EXAMPLES / "catalog"), "strict": True},
                    "sources": [source for source in config["sources"] if keep(source)],
                    "incremental": False,
                    "servingExport": False,
                    "lowPriority": False,
                }
            )
            config_path = Path(temporary, "config.json")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = run_pipeline(config_path)
            return graph_snapshot(output / "graph.sqlite")

    def check(self, name: str, snapshot: dict[str, list[str]]) -> None:
        path = GOLDEN / name
        if UPDATE:
            write_snapshot(snapshot, path)
            return
        if not path.is_file():
            self.skipTest(f"{path.name} not recorded yet; run with CODE_MAP_UPDATE_GOLDEN=1")
        expected = json.loads(path.read_text(encoding="utf-8"))
        difference = snapshot_difference(expected, snapshot)
        self.assertFalse(
            difference,
            f"graph differs from {path.relative_to(REPOSITORY)} "
            f"(refresh with CODE_MAP_UPDATE_GOLDEN=1 if intended):\n{difference}",
        )

    @unittest.skipUnless(_typescript_available(), "Node.js and the angular-extractor TypeScript are required")
    def test_portable_sources(self) -> None:
        snapshot = self.extract(lambda source: not source["type"].startswith("dotnet"))
        self.assertTrue(snapshot["edges"])
        self.check("demo-portable.json", snapshot)

    @unittest.skipUnless(
        shutil.which(os.environ.get("CODE_TREE_DOTNET") or "dotnet") and _typescript_available(),
        "the .NET SDK is required",
    )
    def test_all_sources(self) -> None:
        self.check("demo-full.json", self.extract(lambda source: True))


if __name__ == "__main__":
    unittest.main()
