from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from application.backend.importer.package_validator import PACKAGE_CSV_HEADERS
from application.backend.importer.pipeline import import_authoritative, import_roots, initialize

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = sorted((
    ROOT / "output/angular/customer-web",
    ROOT / "output/dotnet-api/order-api",
    ROOT / "output/batch/order-fulfillment",
    ROOT / "output/plsql/order-db",
    ROOT / "output/sql-files/order-ops",
))


class ImportModelRequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "graph.sqlite"
        import_roots(PACKAGES, self.db_path, ROOT / "input-data", ROOT)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_authoritative_table_column_names_and_detail_rows(self) -> None:
        table = self.conn.execute("SELECT * FROM graph_nodes WHERE node_id='table:ORDER_DB:ORDER_HEADER'").fetchone()
        column = self.conn.execute("SELECT * FROM graph_nodes WHERE node_id='column:ORDER_DB:ORDER_HEADER:STATUS'").fetchone()
        self.assertEqual("ORDER_DB.ORDER_HEADER", table["qualified_name"])
        self.assertEqual("ORDER_DB.ORDER_HEADER.STATUS", column["qualified_name"])

        table_detail = self.conn.execute("SELECT * FROM table_details WHERE node_id=?", (table["node_id"],)).fetchone()
        column_detail = self.conn.execute("SELECT * FROM column_details WHERE node_id=?", (column["node_id"],)).fetchone()
        self.assertEqual(("ORDER_DB", "ORDER_HEADER"), (table_detail["database_id"], table_detail["table_code"]))
        self.assertEqual("table:ORDER_DB:ORDER_HEADER", column_detail["table_node_id"])
        self.assertEqual(3, column_detail["ordinal_position"])
        self.assertEqual("VARCHAR2", column_detail["data_type"])
        self.assertEqual(0, column_detail["nullable"])

        related_column = self.conn.execute("SELECT properties_json FROM graph_nodes WHERE node_id='column:ORDER_DB:ORDER_LINE:ORDER_ID'").fetchone()
        self.assertEqual("ORDER_HEADER", json.loads(related_column["properties_json"])["relation_table"])

    def test_localization_rows_have_timestamps_and_compat_view(self) -> None:
        row = self.conn.execute("""
            SELECT target_type,target_id,field_name,locale,value,created_at,updated_at
            FROM localized_texts
            WHERE target_id='column:ORDER_DB:ORDER_HEADER:STATUS' AND field_name='name' AND locale='ja'
        """).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("注文状態", row["value"])
        self.assertTrue(row["created_at"])
        self.assertTrue(row["updated_at"])

    def test_package_localized_texts_are_imported(self) -> None:
        package = Path(self.tmp.name) / "localized-package"
        package.mkdir()
        node_id = "screen:test-web:/localized"
        groups = {
            "nodes": [
                {
                    "node_id": node_id,
                    "node_type": "SCREEN",
                    "technical_name": "/localized",
                    "qualified_name": "test-web:/localized",
                    "default_display_name": "Localized Orders",
                    "system_key": "order-system",
                    "database_key": "",
                    "repository_key": "test-web",
                    "graph_role": "MAIN",
                    "confidence": "1.0",
                    "properties_json": "{}",
                }
            ],
            "edges": [],
            "evidence": [],
            "issues": [],
            "localized_texts": [
                {
                    "target_type": "SCREEN",
                    "target_id": node_id,
                    "field_name": "name",
                    "locale": "ja",
                    "value": "ローカライズ注文",
                    "source_kind": "EXTRACTED",
                    "review_status": "PENDING",
                    "author_name": "test-extractor",
                    "created_at": "2026-07-20T09:00:00+07:00",
                    "updated_at": "2026-07-20T10:00:00+07:00",
                }
            ],
        }
        checksums = {}
        for name, rows in groups.items():
            path = package / f"{name}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=PACKAGE_CSV_HEADERS[name], lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            checksums[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        manifest = {
            "contractVersion": "1.0",
            "extractor": {"name": "test-extractor", "version": "1.0.0"},
            "source": {"sourceKey": "test-source", "repositoryKey": "test-web"},
            "generatedAt": "2026-07-20T09:00:00+07:00",
            "files": {name: f"{name}.csv" for name in groups},
            "statistics": {"filesScanned": 1, **{name: len(rows) for name, rows in groups.items()}},
            "checksums": checksums,
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        db_path = Path(self.tmp.name) / "localized-package.sqlite"
        import_roots([package], db_path, ROOT / "input-data", ROOT)
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                """
                SELECT value,source_kind,review_status,author_name,created_at,updated_at
                FROM localized_texts
                WHERE target_id=? AND field_name='name' AND locale='ja'
                """,
                (node_id,),
            ).fetchone()
        self.assertEqual(("ローカライズ注文", "EXTRACTED", "PENDING", "test-extractor", "2026-07-20T09:00:00+07:00", "2026-07-20T10:00:00+07:00"), tuple(row))

    def test_job_predecessors_create_depends_on_edges(self) -> None:
        expected = {
            (
                "job:batch-system:ORDER_FULFILLMENT:CREATE_SHIPMENTS",
                "job:batch-system:ORDER_FULFILLMENT:ALLOCATE_ORDERS",
            ),
            (
                "job:batch-system:ORDER_FULFILLMENT:RECONCILE_ORDERS",
                "job:batch-system:ORDER_FULFILLMENT:CREATE_SHIPMENTS",
            ),
            (
                "job:batch-system:ORDER_FULFILLMENT:UNMAPPED_EXPORT",
                "job:batch-system:ORDER_FULFILLMENT:RECONCILE_ORDERS",
            ),
        }
        actual = {tuple(row) for row in self.conn.execute("""
            SELECT source_node_id,target_node_id
            FROM graph_edges
            WHERE edge_type='DEPENDS_ON' AND graph_layer='STRUCTURAL'
        """)}
        self.assertTrue(expected <= actual)

    def test_hybrid_model_tables_views_run_state_and_indexes_exist(self) -> None:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        self.assertTrue({
            "processing_runs",
            "import_packages",
            "source_artifacts",
            "table_details",
            "column_details",
            "localized_texts",
            "knowledge_entries",
            "resolution_issues",
            "node_evidence",
            "edge_evidence",
            "edge_paths",
        } <= tables)

        run = self.conn.execute("SELECT * FROM processing_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        self.assertIn(run["run_status"], {"COMPLETED", "COMPLETED_WITH_ERRORS"})
        package_count = self.conn.execute("SELECT count(*) FROM import_packages WHERE run_id=?", (run["run_id"],)).fetchone()[0]
        self.assertEqual(len(PACKAGES), package_count)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM graph_issues").fetchone()[0],
            self.conn.execute("SELECT count(*) FROM resolution_issues").fetchone()[0],
        )
        self.assertGreater(self.conn.execute("SELECT count(*) FROM source_artifacts").fetchone()[0], 0)

        node_indexes = {row["name"] for row in self.conn.execute("PRAGMA index_list('graph_nodes')")}
        edge_indexes = {row["name"] for row in self.conn.execute("PRAGMA index_list('graph_edges')")}
        loc_indexes = {row["name"] for row in self.conn.execute("PRAGMA index_list('graph_localization')")}
        issue_indexes = {row["name"] for row in self.conn.execute("PRAGMA index_list('resolution_issues')")}
        self.assertTrue({"graph_nodes_type", "graph_nodes_database_type", "graph_nodes_technical_name"} <= node_indexes)
        self.assertTrue({"graph_edges_source_layer", "graph_edges_target_layer"} <= edge_indexes)
        self.assertIn("graph_localization_lookup", loc_indexes)
        self.assertIn("resolution_issues_status_type", issue_indexes)

    def test_business_semantic_dictionary_import_preserves_user_mapping(self) -> None:
        semantic = self.conn.execute(
            "SELECT * FROM graph_business_semantics WHERE semantic_id='semantic:orders:order-status' AND locale='vi'"
        ).fetchone()
        self.assertEqual("Trạng thái đơn hàng", semantic["label"])
        self.assertIn("STATUS", json.loads(semantic["aliases_json"]))
        target_id = "column:ORDER_DB:ORDER_HEADER:STATUS"
        self.conn.execute(
            "INSERT INTO graph_business_semantic_mappings(target_id,semantic_id,status,source_kind,author_name) VALUES(?,?,'approved','USER','tester')",
            (target_id, semantic["semantic_id"]),
        )
        self.conn.commit()
        import_authoritative(self.conn, ROOT / "input-data")
        mapping = self.conn.execute("SELECT * FROM graph_business_semantic_mappings WHERE target_id=?", (target_id,)).fetchone()
        self.assertEqual((semantic["semantic_id"], "USER", "tester"), (mapping["semantic_id"], mapping["source_kind"], mapping["author_name"]))

    def test_initialize_migrates_legacy_localization_shape(self) -> None:
        with sqlite3.connect(Path(self.tmp.name) / "legacy.sqlite") as db:
            db.execute("CREATE TABLE graph_sources(source_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,created_at TEXT NOT NULL)")
            db.execute("""
                CREATE TABLE graph_localization(
                    target_type TEXT NOT NULL,target_id TEXT NOT NULL,field_name TEXT NOT NULL,locale TEXT NOT NULL,value TEXT NOT NULL,
                    source_kind TEXT NOT NULL,review_status TEXT NOT NULL,author_name TEXT NOT NULL,PRIMARY KEY(target_id,field_name,locale)
                )
            """)
            db.execute("INSERT INTO graph_localization VALUES('TABLE','table:DB:T','name','en','Table','IMPORTED','APPROVED','demo')")
            initialize(db)
            row = db.execute("SELECT created_at,updated_at FROM localized_texts WHERE target_id='table:DB:T'").fetchone()
            self.assertTrue(row[0])
            self.assertTrue(row[1])


if __name__ == "__main__":
    unittest.main()
