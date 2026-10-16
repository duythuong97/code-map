"""The web API reads the extract output directly (codemap-web.sqlite + graph.sqlite)."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("CODE_MAP_PROJECT_ROOT", str(PROJECT_ROOT))

from code_tree_exporter.rag_export import write_rag_database  # noqa: E402
from shared.fixtures.engine_graph import business_inputs, engine_graph  # noqa: E402

API = "/api/graph"
CANCEL = "method:a:Orders.CancelOrder"
DELETE_API = "api-operation:a:DELETE:/api/orders/{id}"
TABLE = "table:DB1:ORDER_HEADER"


class GraphApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.output = base / "output"
        self.output.mkdir()
        self.inputs = base / "input-data"
        engine_graph(self.output / "graph.sqlite")
        business_inputs(self.inputs)
        self.extract()
        import web.backend.api.app as serving

        self.serving = serving
        self.previous = (serving.GRAPH_OUTPUT, serving.KNOWLEDGE_DB)
        # The app re-reads these module settings on every request.
        serving.GRAPH_OUTPUT = self.output
        serving.KNOWLEDGE_DB = base / "knowledge.sqlite"
        self.client = serving.app.test_client()

    def tearDown(self) -> None:
        self.serving.GRAPH_OUTPUT, self.serving.KNOWLEDGE_DB = self.previous
        self.temporary.cleanup()

    def extract(self) -> None:
        write_rag_database(self.output / "graph.sqlite", self.output / "codemap-rag.sqlite", input_root=self.inputs)

    def get(self, path: str):
        response = self.client.get(API + path)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_search_and_localization_use_business_names(self) -> None:
        rows = self.get("/search?q=注文&locale=ja&exclude_node_types=COLUMN")
        self.assertEqual([row["node_id"] for row in rows], [TABLE])
        self.assertEqual(rows[0]["localized_display_name"], "注文")
        items = self.get(f"/localization?target_id={TABLE}")["items"]
        self.assertEqual(items[TABLE]["ja"]["name"], "注文")
        self.assertEqual(items[TABLE]["en"]["name"], "Order Header")

    def test_detail_reads_semantic_tree_from_graph_sqlite(self) -> None:
        with closing(sqlite3.connect(self.output / "codemap-web.sqlite")) as web:
            stored = web.execute("SELECT properties_json FROM graph_nodes WHERE node_id=?", (CANCEL,)).fetchone()[0]
        self.assertNotIn("semantic_tree", stored)
        detail = self.get(f"/detail?target_id={CANCEL}&target_type=NODE&relations=0&locale=vi")
        self.assertEqual(detail["properties"]["semantic_tree"]["label"], "Cancel order")
        self.assertEqual(detail["properties"]["route"], "x")
        semantic = detail["business_semantics"][0]
        self.assertEqual((semantic["semantic_id"], semantic["label"]), ("semantic:orders:cancel-order", "Hủy đơn hàng"))

    def test_table_detail_lists_columns_and_the_code_that_writes_it(self) -> None:
        detail = self.get(f"/detail?target_id={TABLE}&target_type=NODE")
        self.assertEqual([column["technical_name"] for column in detail["columns"]], ["STATUS"])
        self.assertEqual([(path["actor_node_id"], path["operation"]) for path in detail["impacts"]], [(DELETE_API, "W")])
        self.assertEqual(detail["impacts"][0]["terminal_edge_type"], "UPDATES")
        summary = self.get(f"/detail?target_id={TABLE}&target_type=NODE&relations=0")
        self.assertEqual(summary["counts"]["impacts"], 1)
        self.assertEqual(summary["incoming"], [])

    def test_write_flow_and_evidence(self) -> None:
        flow = self.get(f"/flow?node_id={DELETE_API}&mode=W&direction=out&semantic_level=TECHNICAL")
        self.assertEqual({node["node_id"] for node in flow["nodes"]}, {DELETE_API, CANCEL, TABLE})
        update = next(edge for edge in flow["edges"] if edge["edge_type"] == "UPDATES")
        self.assertEqual(len(flow["paths"]), 1)
        evidence = self.get(f"/evidence?target_id={update['edge_id']}")
        self.assertEqual(evidence["items"][0]["snippet"], "UPDATE ORDER_HEADER SET STATUS='C'")
        neighbours = self.get(f"/flow?node_id={CANCEL}&mode=1&direction=both")
        cancel = next(node for node in neighbours["nodes"] if node["node_id"] == CANCEL)
        self.assertNotIn("semantic_tree", cancel["properties_json"])
        system = self.get(f"/flow?node_id={DELETE_API}&mode=1&direction=out&semantic_level=SYSTEM")
        self.assertEqual([edge["graph_layer"] for edge in system["edges"]], ["DATA_FLOW"])

    def test_jobs_from_jobnet_are_in_the_graph(self) -> None:
        rows = self.get("/search?q=Allocate%20orders")
        job = next(row for row in rows if row["node_type"] == "JOB")
        flow = self.get(f"/flow?node_id={job['node_id']}&mode=1&direction=out")
        self.assertEqual(sorted(edge["edge_type"] for edge in flow["edges"]), ["CALLS", "STARTS"])
        export = next(row for row in self.get("/search?q=Partner%20export") if row["node_type"] == "JOB")
        issues = self.get(f"/detail?target_id={export['node_id']}&target_type=NODE&relations=0")["issues"]
        self.assertEqual(issues[0]["issue_type"], "JOB_EXECUTABLE_NOT_FOUND")

    def test_people_edits_survive_the_next_extract(self) -> None:
        response = self.client.put(
            f"{API}/business-semantic-mappings/{TABLE}?locale=vi",
            json={"semantic_id": "semantic:orders:cancel-order", "author_name": "an"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        token = os.environ["CODE_MAP_KNOWLEDGE_TOKEN"]
        response = self.client.post(
            f"{API}/knowledge", json={"target_id": TABLE, "body": "Đơn hủy giữ trạng thái C", "status": "approved"},
            headers={"X-Code-Map-Token": token},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.extract()  # a new extract replaces codemap-web.sqlite
        mapped = self.get(f"/business-semantic-suggestions?target_id={TABLE}&locale=vi")["selected"]
        self.assertEqual([(row["semantic_id"], row["author_name"]) for row in mapped], [("semantic:orders:cancel-order", "an")])
        notes = self.get(f"/knowledge?target_id={TABLE}")
        self.assertEqual([note["body"] for note in notes], ["Đơn hủy giữ trạng thái C"])

    def test_missing_output_is_reported(self) -> None:
        self.serving.GRAPH_OUTPUT = self.output / "missing"
        response = self.client.get(f"{API}/search?q=x")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "graph_not_ready")


if __name__ == "__main__":
    unittest.main()
