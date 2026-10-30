"""One flow walk: the code-flow view, business documents and MCP name the same data for an entry."""
from __future__ import annotations

import sqlite3
import unittest

from code_tree_exporter.serving_tables import SCHEMA

from codemap.business import GraphIndex
from codemap.flows import data_paths, touched_data

NODES = {
    "screen": "SCREEN", "component": "ANGULAR_COMPONENT", "api": "API_OPERATION", "method": "METHOD",
    "orders": "TABLE", "customers": "TABLE", "status": "COLUMN", "project": "ANGULAR_PROJECT",
    "job": "JOB", "exe": "EXECUTABLE", "main": "EXECUTABLE_ENTRY_POINT", "allocate": "METHOD", "lines": "TABLE",
    "repository": "METHOD", "select": "INLINE_SQL", "payments": "TABLE",
}
EDGES = [
    ("project", "CONTAINS", "screen"),  # structural: a walk from the project does not enter the screen
    ("screen", "BELONGS_TO", "component"),
    ("component", "CALLS_API", "api"),
    ("api", "HANDLED_BY", "method"),
    ("method", "UPDATES", "orders"),
    ("method", "READS", "customers"),
    ("orders", "CONTAINS", "status"),
    ("job", "STARTS", "exe"),
    ("exe", "ENTRY_IN", "main"),
    ("main", "CALLS", "allocate"),
    ("allocate", "INSERTS", "lines"),
    ("allocate", "READS", "orders"),
    ("method", "CALLS", "repository"),
    ("repository", "CONTAINS", "select"),  # a method's SQL is part of the flow
    ("select", "READS", "payments"),
]


class FlowWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        self.db.executemany(
            "INSERT INTO graph_nodes VALUES(?,?,?,?,?,NULL,NULL,'repo','MAIN',1.0,'{}','s')",
            [(node_id, node_type, node_id, node_id, node_id) for node_id, node_type in NODES.items()],
        )
        self.db.executemany(
            "INSERT INTO graph_edges VALUES(?,?,?,?,'TECHNICAL','',1.0,'{}','s')",
            [(f"{source}-{edge_type}-{target}", source, target, edge_type) for source, edge_type, target in EDGES],
        )

    def tearDown(self) -> None:
        self.db.close()

    def touched(self, entry: str) -> set[tuple[str, str]]:
        return {(target, edge_type) for target, edge_type, _ in touched_data(self.db, entry)}

    def test_screen_reaches_its_data_through_component_and_api(self) -> None:
        self.assertEqual(
            self.touched("screen"), {("orders", "UPDATES"), ("customers", "READS"), ("payments", "READS")}
        )
        [(_, _, path)] = [item for item in touched_data(self.db, "screen") if item[0] == "orders"]
        self.assertEqual(path, ["screen-BELONGS_TO-component", "component-CALLS_API-api", "api-HANDLED_BY-method",
                                "method-UPDATES-orders"])

    def test_job_reaches_the_batch_code_through_its_entry_point(self) -> None:
        self.assertEqual(self.touched("job"), {("lines", "INSERTS"), ("orders", "READS")})

    def test_structure_is_not_a_flow(self) -> None:
        self.assertEqual(self.touched("project"), set())

    def test_who_writes_a_table(self) -> None:
        walk = data_paths(self.db, "orders", "W", "in")
        starts = {walk.edges[path[0]]["source_node_id"] for path in walk.paths}
        self.assertIn("screen", starts)
        self.assertNotIn("job", starts)  # the job only reads ORDERS

    def test_business_documents_reach_the_same_data(self) -> None:
        index = GraphIndex(self.db)
        for entry in ("screen", "job", "project"):
            with self.subTest(entry=entry):
                reached = {node for node in index.reach([entry], 100) if NODES[node] == "TABLE"}
                self.assertEqual(reached, {target for target, _ in self.touched(entry)})


if __name__ == "__main__":
    unittest.main()
