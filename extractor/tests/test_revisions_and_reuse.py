"""Graph versioning: recorded commits, stale checks, incremental reuse and ``extract --at``."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from code_tree_exporter.checkout import CheckoutError, checkout_at, parse_at
from code_tree_exporter.pipeline import _typescript_path, configured_sources, run_pipeline
from code_tree_exporter.stale import stale_report, summary_line

SQL_V1 = "UPDATE APP.ORDERS SET STATUS = 'X' WHERE ID = 1;\n"
SQL_V2 = "UPDATE APP.ORDERS SET STATUS = 'X' WHERE ID = 1;\nDELETE FROM APP.ORDER_LINE WHERE ID = 1;\n"


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.email=t@example.com", "-c", "user.name=t", *arguments],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _metadata(output: Path) -> dict:
    with closing(sqlite3.connect(output / "graph.sqlite")) as graph:
        return {key: json.loads(value) for key, value in graph.execute("SELECT key, value_json FROM metadata")}


@unittest.skipUnless(shutil.which("git"), "git is required")
class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "src"
        (self.root / "ops").mkdir(parents=True)
        (self.root / "ops" / "job.sql").write_text(SQL_V1, encoding="utf-8")
        _git(self.root, "init", "-q")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "v1")
        self.first = _git(self.root, "rev-parse", "HEAD")
        (self.root / "ops" / "job.sql").write_text(SQL_V2, encoding="utf-8")
        _git(self.root, "commit", "-qam", "v2")
        self.second = _git(self.root, "rev-parse", "HEAD")
        self.output = base / "out"
        self.config = base / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "name": "t",
                    "root": str(self.root),
                    "output": str(self.output),
                    "lowPriority": False,
                    "servingExport": False,
                    "sources": [
                        {"name": "ops", "type": "sql-files", "folders": ["ops"], "database": "DB1", "schema": "APP"}
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_commit_and_reuses_unchanged_sources(self) -> None:
        run_pipeline(self.config)
        metadata = _metadata(self.output)
        self.assertEqual(metadata["source_revisions"]["ops"]["commit"], self.second)
        self.assertFalse(metadata["source_revisions"]["ops"]["dirty"])
        self.assertEqual(metadata["incremental"]["extracted"], ["ops"])
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["revisions"]["ops"]["commit"], self.second)

        with closing(sqlite3.connect(self.output / "graph.sqlite")) as graph:
            first_edges = sorted(graph.execute("SELECT stable_id FROM edges"))
        run_pipeline(self.config)
        self.assertEqual(_metadata(self.output)["incremental"]["reused"], ["ops"])
        with closing(sqlite3.connect(self.output / "graph.sqlite")) as graph:
            self.assertEqual(sorted(graph.execute("SELECT stable_id FROM edges")), first_edges)

    def test_stale_report_lists_changed_and_new_files_and_commits(self) -> None:
        run_pipeline(self.config)
        report = stale_report(self.output / "graph.sqlite")
        self.assertTrue(report["fresh"], report)

        (self.root / "ops" / "job.sql").write_text(SQL_V1, encoding="utf-8")
        (self.root / "ops" / "new.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "v3")
        report = stale_report(self.output / "graph.sqlite")
        self.assertFalse(report["fresh"])
        self.assertEqual(report["changed"], ["ops/job.sql"])
        self.assertEqual(report["added"], ["ops/new.sql"])
        self.assertEqual(report["revisions"][0]["commits_since"], 1)
        self.assertTrue(any(node["file"] == "ops/job.sql" for node in report["affected_nodes"]))
        self.assertIn("STALE", summary_line(report))

    def test_extract_at_an_older_commit(self) -> None:
        root, sources = configured_sources(self.config)
        default, per_source = parse_at([self.first])
        with checkout_at(root, sources, default=default, per_source=per_source) as checked_out:
            run_pipeline(self.config, root_override=checked_out, output_override=self.output, display_root=root)
        metadata = _metadata(self.output)
        self.assertEqual(metadata["source_revisions"]["ops"]["commit"], self.first)
        self.assertEqual(Path(metadata["source_root"]), self.root.resolve())
        with closing(sqlite3.connect(self.output / "graph.sqlite")) as graph:
            operations = {row[0] for row in graph.execute("SELECT raw_operation FROM edges WHERE edge_type='WRITES_TO'")}
        self.assertNotIn("DELETE", operations)
        # The worktree is gone and the working tree is untouched.
        self.assertEqual(len(_git(self.root, "worktree", "list").splitlines()), 1)
        self.assertEqual((self.root / "ops" / "job.sql").read_text(encoding="utf-8"), SQL_V2)
        # The older graph is stale against today's working tree.
        self.assertEqual(stale_report(self.output / "graph.sqlite")["changed"], ["ops/job.sql"])

    def test_at_validation(self) -> None:
        self.assertEqual(parse_at(["main", "web=v2"]), ("main", {"web": "v2"}))
        with self.assertRaises(CheckoutError):
            parse_at(["a", "b"])
        root, sources = configured_sources(self.config)
        with self.assertRaises(CheckoutError):
            with checkout_at(root, sources, per_source={"nope": "HEAD"}):
                pass
        with self.assertRaises(CheckoutError):
            with checkout_at(root, sources, default="no-such-revision"):
                pass


class TypeScriptVersionTests(unittest.TestCase):
    def test_typescript_7_is_skipped_for_the_extractor_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            native = root / "web" / "node_modules" / "typescript"
            native.mkdir(parents=True)
            (native / "package.json").write_text('{"version": "7.0.2"}')
            (root / "web" / "src").mkdir(parents=True)
            self.assertNotEqual(_typescript_path(root, ["web/src"]), str(native))


if __name__ == "__main__":
    unittest.main()
