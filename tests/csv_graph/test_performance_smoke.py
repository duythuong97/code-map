from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from flask import Flask

from application.backend.importer.pipeline import import_roots

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = sorted((
    ROOT / "output/angular/customer-web",
    ROOT / "output/dotnet-api/order-api",
    ROOT / "output/batch/order-fulfillment",
    ROOT / "output/plsql/order-db",
    ROOT / "output/sql-files/order-ops",
))


class CsvGraphPerformanceSmokeTest(unittest.TestCase):
    """Lightweight local guardrails for section-34 graph API latency.

    These thresholds are intentionally generous so the test catches obvious
    regressions without acting as a production benchmark.
    """

    SEARCH_MAX_SECONDS = 2.0
    ONE_HOP_MAX_SECONDS = 3.0
    FLOW_ALL_MAX_SECONDS = 5.0

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "performance-smoke.sqlite"
        import_roots(PACKAGES, cls.db, ROOT / "input-data", ROOT)

        from application.backend.api import graph_routes

        cls._old_db_path = graph_routes.DB_PATH
        graph_routes.DB_PATH = cls.db
        app = Flask(__name__)
        app.config.update(TESTING=True, CODE_MAP_GRAPH_DB_PATH=cls.db)
        app.register_blueprint(graph_routes.bp)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        from application.backend.api import graph_routes

        graph_routes.DB_PATH = cls._old_db_path
        cls.tmp.cleanup()

    def _assert_timed_get(self, label: str, path: str, query: dict[str, str], max_seconds: float) -> dict:
        warmup = self.client.get(path, query_string=query)
        self.assertEqual(200, warmup.status_code, warmup.get_data(as_text=True))

        started = time.perf_counter()
        response = self.client.get(path, query_string=query)
        elapsed = time.perf_counter() - started

        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assertLess(elapsed, max_seconds, f"{label} took {elapsed:.3f}s; max {max_seconds:.3f}s")
        payload = response.get_json()
        self.assertIsNotNone(payload)
        return payload

    def test_search_one_hop_and_flow_all_are_within_local_smoke_thresholds(self) -> None:
        search = self._assert_timed_get(
            "search",
            "/api/graph/search",
            {"q": "ORDER", "database": "ORDER_DB", "node_types": "TABLE", "limit": "20"},
            self.SEARCH_MAX_SECONDS,
        )
        self.assertTrue(search)
        self.assertTrue(any(row["node_id"] == "table:ORDER_DB:ORDER_HEADER" for row in search))

        one_hop = self._assert_timed_get(
            "one-hop flow",
            "/api/graph/flow",
            {
                "node_id": "screen:customer-web:/orders/new",
                "mode": "1",
                "direction": "both",
                "max_nodes": "100",
                "semantic_level": "TECHNICAL",
            },
            self.ONE_HOP_MAX_SECONDS,
        )
        self.assertGreater(len(one_hop["nodes"]), 0)
        self.assertGreater(len(one_hop["edges"]), 0)
        self.assertLessEqual(len(one_hop["nodes"]), one_hop["max_nodes"])

        flow_all = self._assert_timed_get(
            "recursive flow-all",
            "/api/graph/flow-all",
            {
                "node_id": "screen:customer-web:/orders/new",
                "mode": "E",
                "direction": "both",
                "max_nodes": "500",
                "semantic_level": "TECHNICAL",
            },
            self.FLOW_ALL_MAX_SECONDS,
        )
        self.assertGreaterEqual(len(flow_all["nodes"]), len(one_hop["nodes"]))
        self.assertGreaterEqual(len(flow_all["edges"]), len(one_hop["edges"]))
        self.assertLessEqual(len(flow_all["nodes"]), flow_all["max_nodes"])


if __name__ == "__main__":
    unittest.main()