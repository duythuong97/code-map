from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.pipeline import (  # noqa: E402
    _resolved_limits,
    _selected_files,
    _typescript_path,
)


class SelectedFilesTests(unittest.TestCase):
    def test_root_below_blocked_directory_name_keeps_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "build", "sources").resolve()
            (root / "app").mkdir(parents=True)
            (root / "app" / "a.sql").write_text("SELECT 1 FROM DUAL;\n")
            (root / "app" / "bin").mkdir()
            (root / "app" / "bin" / "b.sql").write_text("SELECT 1 FROM DUAL;\n")
            (root / "node_modules" / "x").mkdir(parents=True)
            (root / "node_modules" / "x" / "c.sql").write_text("SELECT 1;\n")

            selected = _selected_files(root, ["."], (".sql",))

            self.assertEqual(
                [path.relative_to(root).as_posix() for path in selected],
                ["app/a.sql"],
            )


class LimitsTests(unittest.TestCase):
    def test_extractor_timeout_must_cover_project_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "extractorTimeoutSeconds"):
            _resolved_limits(
                {"limits": {"extractorTimeoutSeconds": 300, "projectTimeoutSeconds": 900}}
            )

    def test_default_extractor_timeout_covers_project_timeout(self) -> None:
        limits = _resolved_limits({})
        self.assertGreaterEqual(
            limits["extractorTimeoutSeconds"], limits["projectTimeoutSeconds"]
        )


class TypeScriptPathTests(unittest.TestCase):
    def test_nearest_node_modules_above_source_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            typescript = root / "web" / "node_modules" / "typescript"
            typescript.mkdir(parents=True)
            (typescript / "package.json").write_text("{}")
            (root / "web" / "src" / "app").mkdir(parents=True)

            self.assertEqual(
                _typescript_path(root, ["web/src/app"]), str(typescript)
            )


class AntlrWarmCacheTests(unittest.TestCase):
    _SCRIPT = textwrap.dedent(
        """
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql
        result = analyze_sql("UPDATE APP.ORDERS SET STATUS = 'X' WHERE ID = 1;")
        print(json.dumps([[t.object_name, t.edge_type] for t in result.tables]))
        """
    )

    def test_restored_dfa_gives_identical_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            environment = os.environ | {
                "CODE_TREE_CACHE_DIR": cache,
                "CODE_TREE_ANTLR_CACHE_PROFILE": "test",
                "CODE_TREE_ANTLR_CACHE": "1",
                "CODE_TREE_SQL_PARSER": "python",
            }

            def run() -> str:
                return subprocess.run(
                    [sys.executable, "-c", self._SCRIPT, str(PROJECT_ROOT)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout

            cold = run()
            self.assertTrue(list(Path(cache).glob("plsql-antlr-dfa-test-*.pickle")))
            warm = run()

            self.assertEqual(cold, warm)
            self.assertIn("WRITES_TO", warm)


if __name__ == "__main__":
    unittest.main()


class SlimGraphIndexTests(unittest.TestCase):
    def test_slim_index_keeps_lookup_keys_and_is_readable(self) -> None:
        from code_tree_exporter.graph_package import GraphPackage, _write_graph_index
        from code_tree_exporter.query import KnowledgeStore

        graph = GraphPackage()
        graph.nodes["1"] = {"node_id": "1", "node_type": "API_OPERATION", "properties_json": "{}"}
        graph.edges["2"] = {"edge_id": "2", "edge_type": "CALLS", "source_node_id": "1", "target_node_id": "1"}
        memory = {"1": {"file": "codebase-memory/entities/api.jsonl", "line": 1}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            _write_graph_index(
                graph, output, generated_at="2026-01-01T00:00:00Z", packages={},
                node_packages={}, edge_packages={}, evidence_packages={}, issue_packages={},
                knowledge_refs={}, memory_refs=memory,
            )
            index = json.loads((output / "graph-index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["indexVersion"], "3.0")
            self.assertEqual(index["nodeTypeCounts"], {"API_OPERATION": 1})
            self.assertEqual(index["edgeTypeCounts"], {"CALLS": 1})
            self.assertEqual(index["memoryByNodeId"], memory)
            self.assertNotIn("edgesBySource", index)
            self.assertNotIn("evidenceById", index)

            (output / "manifest.json").write_text("{}", encoding="utf-8")
            (output / "graph.sqlite").write_bytes(b"")
            self.assertEqual(KnowledgeStore(output).index["memoryByNodeId"], memory)
