from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.rag_export import compact, tokens, write_rag_database  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT.parent))
from shared.fixtures.engine_graph import business_inputs, engine_graph  # noqa: E402

class TokenTests(unittest.TestCase):
    def test_shared_vectors_match_the_extension(self) -> None:
        vectors = json.loads((PROJECT_ROOT.parent / "shared" / "fixtures" / "rag-token-vectors.json").read_text(encoding="utf-8"))
        for vector in vectors:
            with self.subTest(text=vector["text"]):
                self.assertEqual(tokens(vector["text"]), vector["tokens"])
                self.assertEqual(compact(vector["text"]), vector["compact"])


class RagExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        engine_graph(base / "graph.sqlite")
        business_inputs(base / "input-data")
        self.counts = write_rag_database(base / "graph.sqlite", base / "rag.sqlite", input_root=base / "input-data", source_root=base)
        self.rag = sqlite3.connect(base / "rag.sqlite")

    def tearDown(self) -> None:
        self.rag.close()
        self.temporary.cleanup()

    def node(self, name: str) -> int:
        return self.rag.execute("SELECT id FROM nodes WHERE name=?", (name,)).fetchone()[0]

    def test_ids_are_small_and_references_survive(self) -> None:
        self.assertLess(self.rag.execute("SELECT max(id) FROM nodes").fetchone()[0], 2 ** 53)
        src, dst, operation, file, line, snippet = self.rag.execute(
            "SELECT src,dst,operation,file,line,snippet FROM edges WHERE type='WRITES_TO'"
        ).fetchone()
        self.assertEqual((src, dst, operation, file, line), (self.node("CancelOrder"), self.node("ORDER_HEADER"), "UPDATE", "a/Orders.cs", 42))
        self.assertIn("UPDATE ORDER_HEADER", snippet)
        path, operation = self.rag.execute("SELECT path, operation FROM flows").fetchone()
        self.assertEqual(json.loads(path), [self.node("DELETE /api/orders/{id}"), self.node("CancelOrder"), self.node("ORDER_HEADER")])
        self.assertEqual(operation, "UPDATE")
        self.assertEqual(self.rag.execute("SELECT node_id FROM issues WHERE type='DYNAMIC_SQL'").fetchone()[0], self.node("CancelOrder"))

    def test_node_location_doc_and_business_names(self) -> None:
        file, line, doc = self.rag.execute("SELECT file,line,doc FROM nodes WHERE name='CancelOrder'").fetchone()
        self.assertEqual((file, line), ("a/Orders.cs", 40))
        self.assertIn("Cancelled orders cannot ship", doc)
        self.assertEqual(self.rag.execute("SELECT names FROM nodes WHERE name='ORDER_HEADER'").fetchone()[0], "ja:注文; en:Order Header")
        self.assertEqual(self.rag.execute("SELECT names FROM nodes WHERE name='STATUS'").fetchone()[0], "ja:ステータス; en:Status")
        terms = {row[0] for row in self.rag.execute("SELECT term FROM terms WHERE node_id=?", (self.node("ORDER_HEADER"),))}
        self.assertTrue({"order", "header", "orderheader", "注文", "table"} <= terms, terms)

    def test_business_terms_link_to_code(self) -> None:
        linked = {
            (name, reason)
            for name, reason in self.rag.execute(
                "SELECT n.name, l.reason FROM concept_links l JOIN nodes n ON n.id=l.node_id"
            )
        }
        self.assertEqual(linked, {("CancelOrder", "alias 'CANCEL_ORDER'"), ("DELETE /api/orders/{id}", "api alias 'DELETE order'")})
        terms = {row[0] for row in self.rag.execute("SELECT term FROM concept_terms")}
        self.assertTrue({"huy", "don", "hang", "cancelorder", "deleteorder"} <= terms, terms)

    def test_jobs_link_to_the_programs_they_run(self) -> None:
        edges = {
            (source, edge_type, target)
            for source, edge_type, target in self.rag.execute(
                "SELECT s.name, e.type, t.name FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes t ON t.id=e.dst WHERE e.layer='BUSINESS'"
            )
        }
        self.assertIn(("Allocate orders", "STARTS", "order-fulfillment.exe"), edges)
        self.assertIn(("Allocate orders", "INVOKES", "allocate"), edges)
        self.assertIn(("Partner export", "DEPENDS_ON", "Allocate orders"), edges)
        message = self.rag.execute("SELECT message FROM issues WHERE type='JOB_EXECUTABLE_NOT_FOUND'").fetchone()[0]
        self.assertIn("PartnerExport.exe", message)


if __name__ == "__main__":
    unittest.main()
