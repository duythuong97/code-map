from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("CODE_MAP_PROJECT_ROOT", str(PROJECT_ROOT))

from application.backend.importer.graph_sqlite import import_graph, inspect_graph, ui_edge_type  # noqa: E402

ENGINE_SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT);
CREATE TABLE nodes(node_id INTEGER PRIMARY KEY, stable_id TEXT, node_type TEXT, technical_name TEXT, qualified_name TEXT,
  default_display_name TEXT, system_key TEXT, database_key TEXT, repository_key TEXT, graph_role TEXT, confidence REAL,
  properties_json TEXT, package_key TEXT);
CREATE TABLE edges(edge_id INTEGER PRIMARY KEY, stable_id TEXT, source_node_id INTEGER, target_node_id INTEGER, edge_type TEXT,
  graph_layer TEXT, raw_operation TEXT, confidence REAL, properties_json TEXT, package_key TEXT);
CREATE TABLE evidence(evidence_id INTEGER PRIMARY KEY, stable_id TEXT, target_type TEXT, target_id INTEGER, source_path TEXT,
  start_line INTEGER, end_line INTEGER, start_column INTEGER, end_column INTEGER, evidence_kind TEXT, extractor_name TEXT,
  confidence REAL, snippet TEXT, properties_json TEXT, package_key TEXT);
CREATE TABLE issues(issue_id INTEGER PRIMARY KEY, stable_id TEXT, issue_type TEXT, severity TEXT, source_node_id INTEGER,
  raw_reference TEXT, database_key TEXT, source_path TEXT, start_line INTEGER, message TEXT, properties_json TEXT, package_key TEXT);
CREATE TABLE flows(flow_id INTEGER PRIMARY KEY, stable_id TEXT, flow_type TEXT, start_node_id INTEGER, target_node_id INTEGER,
  confidence REAL, complete INTEGER, properties_json TEXT);
CREATE TABLE flow_steps(flow_id INTEGER, step_index INTEGER, node_id INTEGER, incoming_edge_id INTEGER);
"""


def _engine_graph(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ENGINE_SCHEMA)
        db.executemany("INSERT INTO metadata VALUES(?,?)", [
            ("contract_version", json.dumps("3.0")), ("source_name", json.dumps("demo")),
            ("generated_at", json.dumps("2026-01-01T00:00:00Z")),
        ])
        nodes = [
            (1, "api-operation:a:GET:/orders", "API_OPERATION", "GET /orders", "{}"),
            (2, "method:a:Orders.Read", "METHOD", "Read", "{}"),
            (3, "table:DB1:ORDER_HEADER", "TABLE", "ORDER_HEADER", "{}"),
            (4, "column:DB1:ORDER_HEADER:ORDER_ID", "COLUMN", "ORDER_ID", json.dumps({"table": "ORDER_HEADER"})),
        ]
        for node_id, stable, node_type, name, properties in nodes:
            db.execute(
                "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, stable, node_type, name, name, name, "sys", "DB1", "repo", "MAIN", 1.0, properties, "sources/a"),
            )
        db.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?)", [
            (10, "e1", 1, 2, "HANDLES_API", "TECHNICAL", "", 1.0, "{}", "sources/a"),
            (11, "e2", 2, 3, "WRITES_TO", "TECHNICAL", "INSERT", 1.0, "{}", "sources/a"),
            (12, "e3", 3, 4, "CONTAINS", "STRUCTURAL", "", 1.0, "{}", "global"),
        ])
        db.execute(
            "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (20, "ev1", "EDGE", 11, "a/Orders.cs", 3, 3, 1, 10, "SQL", "dotnet", 1.0, "insert", "{}", "sources/a"),
        )
        db.execute(
            "INSERT INTO issues VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (30, "i1", "DYNAMIC_SQL", "WARNING", 2, "x", "DB1", "a/Orders.cs", 3, "dynamic", "{}", "sources/a"),
        )
        db.execute("INSERT INTO flows VALUES(40,'f1','API',1,3,1.0,1,'{}')")
        db.executemany("INSERT INTO flow_steps VALUES(?,?,?,?)", [(40, 0, 1, None), (40, 1, 2, 10), (40, 2, 3, 11)])


class GraphSqliteImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        (self.base / "out").mkdir()
        _engine_graph(self.base / "out" / "graph.sqlite")
        inputs = self.base / "input-data"
        (inputs / "tables").mkdir(parents=True)
        (inputs / "tables.csv").write_text(
            "database,table_code,table_name_ja,table_name_en\nORDER_DB,ORDER_HEADER,注文明細,Order Header\n", encoding="utf-8"
        )
        (inputs / "tables" / "ORDER_HEADER.csv").write_text(
            "column_code,column_name_ja,column_name_en,ordinal_position,data_type,nullable,note,relation_table\n"
            "ORDER_ID,注文ID,Order ID,1,NUMBER,false,,\n",
            encoding="utf-8",
        )
        self.inputs = inputs
        self.db = self.base / "serve.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inspect_reports_contract_and_counts(self) -> None:
        summary = inspect_graph(self.base / "out")
        self.assertEqual(summary["contract_version"], "3.0")
        self.assertEqual(summary["counts"], {"nodes": 4, "edges": 3, "evidence": 1, "issues": 1})

    def test_import_maps_vocabulary_flows_and_names_idempotently(self) -> None:
        first = import_graph(self.base / "out", self.db, self.inputs)
        second = import_graph(self.base / "out", self.db, self.inputs)
        self.assertEqual(first, second)
        self.assertEqual(first, {"nodes": 4, "edges": 4, "evidence": 1, "issues": 1, "paths": 1})
        with closing(sqlite3.connect(self.db)) as db:
            types = {row[0] for row in db.execute("SELECT edge_type FROM graph_edges")}
            self.assertEqual(types, {"HANDLED_BY", "INSERTS", "CONTAINS", "WRITES"})
            actor, table, operation, path = db.execute(
                "SELECT actor_node_id,table_node_id,operation,edge_path_json FROM graph_paths"
            ).fetchone()
            self.assertEqual((actor, table, operation), ("api-operation:a:GET:/orders", "table:DB1:ORDER_HEADER", "W"))
            self.assertEqual(len(json.loads(path)), 2)
            evidence_target = db.execute("SELECT target_id FROM graph_evidence").fetchone()[0]
            self.assertEqual(
                db.execute("SELECT edge_type FROM graph_edges WHERE edge_id=?", (evidence_target,)).fetchone()[0],
                "INSERTS",
            )
            names = dict(db.execute(
                "SELECT target_id||'/'||locale, value FROM graph_localization WHERE field_name='name'"
            ).fetchall())
            self.assertEqual(names["table:DB1:ORDER_HEADER/ja"], "注文明細")
            self.assertEqual(names["column:DB1:ORDER_HEADER:ORDER_ID/en"], "Order ID")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_ui_edge_type(self) -> None:
        self.assertEqual(ui_edge_type("READS_FROM", "SELECT", "TABLE"), "READS")
        self.assertEqual(ui_edge_type("READS_FROM", "SELECT", "EXTERNAL_DATABASE_OBJECT"), "REMOTE_READS")
        self.assertEqual(ui_edge_type("WRITES_TO", "update", "TABLE"), "UPDATES")
        self.assertEqual(ui_edge_type("WRITES_TO", "", "TABLE"), "WRITES")
        self.assertEqual(ui_edge_type("CALLS", "", "METHOD"), "CALLS")

    def test_rejects_unknown_contract(self) -> None:
        with closing(sqlite3.connect(self.base / "out" / "graph.sqlite")) as db, db:
            db.execute("UPDATE metadata SET value_json=? WHERE key='contract_version'", (json.dumps("9.9"),))
        with self.assertRaisesRegex(ValueError, "unsupported graph contract"):
            inspect_graph(self.base / "out")


if __name__ == "__main__":
    unittest.main()
