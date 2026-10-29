"""Graph diff of two extracts: dependencies, behaviour, schema, plan comparison and affected knowledge."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.cli import main  # noqa: E402
from code_tree_exporter.diff import affected_knowledge, compare_with_plan, graph_diff, load_plan, to_markdown  # noqa: E402
from code_tree_exporter.pipeline import run_pipeline  # noqa: E402

BASE_SQL = """CREATE OR REPLACE PROCEDURE CANCEL_ORDER(p_id IN NUMBER) IS
BEGIN
  UPDATE ORDERS SET STATUS = 'C' WHERE ID = p_id;
END;
/
"""
HEAD_SQL = """CREATE OR REPLACE PROCEDURE CANCEL_ORDER(p_id IN NUMBER) IS
BEGIN
  -- comment and blank lines above the old statements must not count as a change

  UPDATE ORDERS SET STATUS = 'C' WHERE ID = p_id;
  DELETE FROM ORDER_LINE WHERE ORDER_ID = p_id;
END;
/
CREATE OR REPLACE PROCEDURE ARCHIVE_ORDER(p_id IN NUMBER) IS
BEGIN
  INSERT INTO ORDER_ARCHIVE SELECT * FROM ORDERS WHERE ID = p_id;
END;
/
"""


class DiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def extract(self, name: str, sql: str) -> Path:
        root = self.base_dir / f"src-{name}"
        (root / "db").mkdir(parents=True)
        (root / "db" / "orders.sql").write_text(sql, encoding="utf-8")
        config = self.base_dir / f"{name}.json"
        config.write_text(json.dumps({
            "name": "t", "root": str(root), "output": str(self.base_dir / f"out-{name}"), "lowPriority": False,
            "incremental": False,
            "sources": [{"name": "db", "type": "oracle-plsql", "folders": ["db"], "database": "DB1", "schema": "APP"}],
        }), encoding="utf-8")
        return run_pipeline(config)

    def test_diff_plan_and_knowledge(self) -> None:
        base, head = self.extract("base", BASE_SQL), self.extract("head", HEAD_SQL)
        diff = graph_diff(base / "graph.sqlite", head / "graph.sqlite")
        added = {(item["source"]["name"], item["type"], item["operation"], item["target"]["name"]) for item in diff["added_edges"]}
        self.assertIn(("CANCEL_ORDER", "WRITES_TO", "DELETE", "ORDER_LINE"), added)
        self.assertIn(("ARCHIVE_ORDER", "WRITES_TO", "INSERT", "ORDER_ARCHIVE"), added)
        self.assertEqual([node["name"] for node in diff["modified_nodes"] if node["type"] == "PROCEDURE"], ["CANCEL_ORDER"])
        self.assertIn("ARCHIVE_ORDER", [node["name"] for node in diff["added_nodes"]])
        self.assertFalse(diff["removed_edges"])

        # Identical extracts: nothing changed.
        same = graph_diff(head / "graph.sqlite", head / "graph.sqlite")
        self.assertFalse(any(same["counts"].values()))

        plan_path = self.base_dir / "impact.yaml"
        plan_path.write_text(
            "change: ORD-7\nexpected:\n"
            "  - {kind: node, name: CANCEL_ORDER, action: modify}\n"
            "  - {kind: table, name: ORDER_LINE, action: write}\n"
            "  - {kind: table, name: PAYMENT, action: write}\n",
            encoding="utf-8",
        )
        plan = compare_with_plan(diff, load_plan(plan_path))
        unplanned = " ".join(item["text"] for item in plan["unplanned"])
        self.assertIn("ARCHIVE_ORDER", unplanned)
        self.assertNotIn("ORDER_LINE", unplanned)  # the planned delete is not listed
        self.assertEqual([item["name"] for item in plan["untouched"]], ["PAYMENT"])

        knowledge_db = self.base_dir / "knowledge.sqlite"
        cancel_id = next(node["id"] for node in diff["modified_nodes"] if node["name"] == "CANCEL_ORDER")
        with closing(sqlite3.connect(knowledge_db)) as db, db:
            db.execute("CREATE TABLE biz_rules(rule_id TEXT, title TEXT, status TEXT, node_ids TEXT)")
            db.execute("INSERT INTO biz_rules VALUES('RULE-ORD-001','Hủy đơn giữ dòng đơn','approved',?)", (json.dumps([cancel_id]),))
            db.execute("INSERT INTO biz_rules VALUES('RULE-ORD-002','Khác','approved','[\"x\"]')")
        knowledge = affected_knowledge(diff, knowledge_db, head / "codemap-web.sqlite")
        self.assertEqual([rule["rule_id"] for rule in knowledge["rules"]], ["RULE-ORD-001"])

        markdown = to_markdown(diff, plan, knowledge)
        self.assertTrue(markdown.startswith("<!-- codemap-diff -->"))
        self.assertIn("outside the plan", markdown)
        self.assertIn("**RULE-ORD-001**", markdown)
        self.assertIn("table `PAYMENT` (write)", markdown)

        report = self.base_dir / "report.md"
        code = main(["diff", "--base", str(base), "--head", str(head), "--plan", str(plan_path), "--out", str(report),
                     "--fail-on-unplanned"])
        self.assertEqual(code, 1)
        self.assertIn("ARCHIVE_ORDER", report.read_text(encoding="utf-8"))

    def test_moved_code_is_not_a_behaviour_change(self) -> None:
        base = self.extract("base", BASE_SQL)
        moved = self.extract("moved", "-- header comment\n\n\n" + BASE_SQL.replace("BEGIN\n", "BEGIN\n  -- note\n\n"))
        diff = graph_diff(base / "graph.sqlite", moved / "graph.sqlite")
        self.assertEqual(diff["modified_nodes"], [])
        self.assertEqual(diff["added_edges"], [])
        self.assertEqual(diff["new_issues"], [])


if __name__ == "__main__":
    unittest.main()
