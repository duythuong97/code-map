from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from code_tree_exporter.search_index import compact, tokens
from code_tree_exporter.serving import write_serving_database

from testdata.engine_graph import business_inputs, engine_graph

class TokenTests(unittest.TestCase):
    def test_shared_vectors_match_the_extension(self) -> None:
        vectors = json.loads((PROJECT_ROOT.parent / "testdata" / "search-token-vectors.json").read_text(encoding="utf-8"))
        for vector in vectors:
            with self.subTest(text=vector["text"]):
                self.assertEqual(tokens(vector["text"]), vector["tokens"])
                self.assertEqual(compact(vector["text"]), vector["compact"])


class ServingExportTests(unittest.TestCase):
    """codemap.sqlite: the serving tables and the search index over them, in one file."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        engine_graph(base / "graph.sqlite")
        business_inputs(base / "input-data")
        self.counts = write_serving_database(
            base / "graph.sqlite", base / "codemap.sqlite", input_root=base / "input-data", source_root=base
        )
        self.db = sqlite3.connect(base / "codemap.sqlite")

    def tearDown(self) -> None:
        self.db.close()
        self.temporary.cleanup()

    def node(self, name: str) -> int:
        return self.db.execute("SELECT id FROM search_nodes WHERE name=?", (name,)).fetchone()[0]

    def test_ids_are_small_and_references_survive(self) -> None:
        self.assertLess(self.db.execute("SELECT max(id) FROM search_nodes").fetchone()[0], 2 ** 53)
        src, dst, operation, file, line, snippet = self.db.execute(
            "SELECT src,dst,operation,file,line,snippet FROM search_edges WHERE type='UPDATES'"
        ).fetchone()
        self.assertEqual((src, dst, operation, file, line), (self.node("CancelOrder"), self.node("ORDER_HEADER"), "UPDATE", "a/Orders.cs", 42))
        self.assertIn("UPDATE ORDER_HEADER", snippet)
        path, operation = self.db.execute("SELECT path, operation FROM search_flows").fetchone()
        self.assertEqual(json.loads(path), [self.node("DELETE /api/orders/{id}"), self.node("CancelOrder"), self.node("ORDER_HEADER")])
        self.assertEqual(operation, "UPDATE")
        self.assertEqual(self.db.execute("SELECT node_id FROM search_issues WHERE type='DYNAMIC_SQL'").fetchone()[0], self.node("CancelOrder"))

    def test_the_index_is_the_serving_graph(self) -> None:
        """Same nodes, same edge names: the web app and the MCP server cannot disagree."""
        self.assertEqual(
            {row[0] for row in self.db.execute("SELECT stable_id FROM search_nodes")},
            {row[0] for row in self.db.execute("SELECT node_id FROM graph_nodes")},
        )
        self.assertEqual(
            {row[0] for row in self.db.execute("SELECT edge_id FROM search_edges")},
            {row[0] for row in self.db.execute("SELECT edge_id FROM graph_edges WHERE graph_layer <> 'DATA_FLOW'")},
        )
        engine_names = {"READS_FROM", "WRITES_TO", "HANDLES_API", "INVOKES"}
        self.assertFalse(engine_names & {row[0] for row in self.db.execute("SELECT DISTINCT type FROM search_edges")})

    def test_node_location_doc_and_business_names(self) -> None:
        file, line, doc = self.db.execute("SELECT file,line,doc FROM search_nodes WHERE name='CancelOrder'").fetchone()
        self.assertEqual((file, line), ("a/Orders.cs", 40))
        self.assertIn("Cancelled orders cannot ship", doc)
        self.assertEqual(self.db.execute("SELECT names FROM search_nodes WHERE name='ORDER_HEADER'").fetchone()[0], "ja:注文; en:Order Header")
        self.assertEqual(self.db.execute("SELECT names FROM search_nodes WHERE name='STATUS'").fetchone()[0], "ja:ステータス; en:Status")
        terms = {row[0] for row in self.db.execute("SELECT term FROM search_terms WHERE node_id=?", (self.node("ORDER_HEADER"),))}
        self.assertTrue({"order", "header", "orderheader", "注文", "table"} <= terms, terms)

    def test_business_terms_link_to_code(self) -> None:
        linked = {
            (name, reason)
            for name, reason in self.db.execute(
                "SELECT n.name, l.reason FROM search_concept_links l JOIN search_nodes n ON n.id=l.node_id"
            )
        }
        self.assertEqual(linked, {("CancelOrder", "alias 'CANCEL_ORDER'"), ("DELETE /api/orders/{id}", "api alias 'DELETE order'")})
        self.assertEqual(
            len(linked), self.db.execute("SELECT count(*) FROM graph_business_semantic_links").fetchone()[0]
        )
        terms = {row[0] for row in self.db.execute("SELECT term FROM search_concept_terms")}
        self.assertTrue({"huy", "don", "hang", "cancelorder", "deleteorder"} <= terms, terms)

    def test_jobs_link_to_the_programs_they_run(self) -> None:
        edges = {
            (source, edge_type, target)
            for source, edge_type, target in self.db.execute(
                "SELECT s.name, e.type, t.name FROM search_edges e JOIN search_nodes s ON s.id=e.src "
                "JOIN search_nodes t ON t.id=e.dst WHERE e.layer='BUSINESS'"
            )
        }
        self.assertIn(("Allocate orders", "STARTS", "order-fulfillment.exe"), edges)
        self.assertIn(("Allocate orders", "CALLS", "allocate"), edges)
        self.assertIn(("Partner export", "DEPENDS_ON", "Allocate orders"), edges)
        message = self.db.execute("SELECT message FROM search_issues WHERE type='JOB_EXECUTABLE_NOT_FOUND'").fetchone()[0]
        self.assertIn("PartnerExport.exe", message)


if __name__ == "__main__":
    unittest.main()
