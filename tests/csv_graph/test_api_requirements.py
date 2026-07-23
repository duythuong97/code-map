from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import application.backend.api.app as serving
from application.backend.importer.pipeline import import_roots

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = sorted((
    ROOT / "output/angular/customer-web",
    ROOT / "output/dotnet-api/order-api",
    ROOT / "output/batch/order-fulfillment",
    ROOT / "output/plsql/order-db",
    ROOT / "output/sql-files/order-ops",
))


class ApiRequirementsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "api-requirements.sqlite"
        import_roots(PACKAGES, self.db, ROOT / "input-data", ROOT)
        self.old_db_path = serving.DB_PATH
        serving.DB_PATH = self.db
        serving.app.config.update(TESTING=True, CODE_MAP_GRAPH_DB_PATH=self.db)
        self.client = serving.app.test_client()

    def tearDown(self):
        serving.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_canonical_search_filters_database_node_types_locale_and_limit(self):
        response = self.client.get(
            "/api/search",
            query_string={"q": "ORDER", "database": "ORDER_DB", "node_types": "TABLE", "locale": "ja", "limit": "3"},
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        rows = response.get_json()
        self.assertLessEqual(len(rows), 3)
        self.assertTrue(rows)
        self.assertTrue(all(row["database_key"] == "ORDER_DB" for row in rows))
        self.assertTrue(all(row["node_type"] == "TABLE" for row in rows))
        self.assertNotIn("schema", json.dumps(rows).lower())

        localized = self.client.get("/api/search", query_string={"q": "注文", "locale": "ja", "limit": "5"})
        self.assertEqual(200, localized.status_code, localized.get_data(as_text=True))
        self.assertIn("table:ORDER_DB:ORDER_HEADER", {row["node_id"] for row in localized.get_json()})

        without_columns = self.client.get(
            "/api/search", query_string={"exclude_node_types": "COLUMN", "limit": "500"}
        )
        self.assertEqual(200, without_columns.status_code, without_columns.get_data(as_text=True))
        self.assertNotIn("COLUMN", {row["node_type"] for row in without_columns.get_json()})

    def test_evidence_defaults_to_50_and_exposes_total_fields(self):
        with closing(sqlite3.connect(self.db)) as db:
            target_id, total = db.execute(
                """
                SELECT target_id, count(*) AS count
                FROM graph_evidence
                GROUP BY target_id HAVING count(*) > 0
                ORDER BY count(*) DESC, target_id LIMIT 1
                """
            ).fetchone()
        response = self.client.get("/api/evidence", query_string={"target_id": target_id})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertLessEqual(payload["count"], 50)
        self.assertEqual(total, payload["evidence_count"])
        self.assertEqual(total > 50, payload["evidence_truncated"])
        self.assertEqual(payload["truncated"], payload["evidence_truncated"])

    def test_snippet_statuses_focus_lines_and_no_absolute_paths(self):
        path = "demo-sources/order-api/OrderFlow.cs"
        available = self.client.get("/api/snippet", query_string={"path": path})
        self.assertEqual(200, available.status_code, available.get_data(as_text=True))
        payload = available.get_json()
        self.assertEqual("available", payload["status"])
        self.assertEqual(path, payload["source_path"])
        self.assertGreaterEqual(payload["focus_line"], 1)
        self.assertNotIn(str(ROOT), json.dumps(payload))

        with patch("application.backend.api.graph_routes.os.access", return_value=False):
            unreadable = self.client.get("/api/snippet", query_string={"path": path})
        self.assertEqual("unreadable", unreadable.get_json()["status"])

        invalid = self.client.get("/api/snippet", query_string={"path": "/etc/passwd"})
        self.assertEqual(403, invalid.status_code)
        self.assertEqual("invalid_source", invalid.get_json()["status"])
        self.assertIsNone(invalid.get_json()["source_path"])

        missing_path = "demo-sources/order-api/missing.cs"
        with closing(sqlite3.connect(self.db)) as db:
            source_id = db.execute("SELECT source_id FROM graph_sources ORDER BY source_id LIMIT 1").fetchone()[0]
            target_id = db.execute("SELECT node_id FROM graph_nodes ORDER BY node_id LIMIT 1").fetchone()[0]
            db.execute(
                """
                INSERT INTO graph_evidence(
                    evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,
                    evidence_kind,extractor_name,confidence,snippet,properties_json,source_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("ev:missing-api-requirements", "NODE", target_id, missing_path, 7, 9, None, None, "SOURCE", "test", 1.0, "", "{}", source_id),
            )
            db.commit()
        missing = self.client.get("/api/snippet", query_string={"path": missing_path, "target_id": target_id})
        self.assertEqual(200, missing.status_code, missing.get_data(as_text=True))
        self.assertEqual("not_found", missing.get_json()["status"])
        self.assertEqual(7, missing.get_json()["focus_line"])
        self.assertNotIn(str(ROOT), json.dumps(missing.get_json()))

    def test_import_validate_import_and_unsupported_upload_are_structured(self):
        paths = [str(path.relative_to(ROOT)) for path in PACKAGES]
        validate = self.client.post("/api/imports/validate", json={"roots": paths})
        self.assertEqual(200, validate.status_code, validate.get_data(as_text=True))
        validation_payload = validate.get_json()
        self.assertEqual("valid", validation_payload["status"])
        self.assertEqual(len(paths), validation_payload["package_count"])
        self.assertNotIn(str(ROOT), json.dumps(validation_payload))

        imported = self.client.post("/api/imports", json={"roots": paths})
        self.assertEqual(201, imported.status_code, imported.get_data(as_text=True))
        self.assertEqual("imported", imported.get_json()["status"])
        self.assertGreater(imported.get_json()["counts"]["nodes"], 0)

        upload = self.client.post(
            "/api/imports/validate",
            data={"file": (io.BytesIO(b"zip"), "package.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(400, upload.status_code, upload.get_data(as_text=True))
        self.assertEqual("unsupported_upload", upload.get_json()["error"]["code"])
        self.assertIn("details", upload.get_json()["error"])

        outside = self.client.post("/api/imports/validate", json={"path": str(Path(self.tmp.name) / "outside")})
        self.assertEqual(400, outside.status_code, outside.get_data(as_text=True))
        self.assertNotIn(str(ROOT), outside.get_data(as_text=True))
        self.assertNotIn(str(self.tmp.name), outside.get_data(as_text=True))


    def test_business_semantic_suggestion_selection_and_detail(self):
        target_id = "procedure:ORDER_DB:PKG_ORDER:CREATE_ORDER:NUMBER_VARCHAR2"
        suggested = self.client.get(
            "/api/graph/business-semantic-suggestions",
            query_string={"target_id": target_id, "locale": "vi", "limit": "3"},
        )
        self.assertEqual(200, suggested.status_code, suggested.get_data(as_text=True))
        payload = suggested.get_json()
        self.assertEqual("semantic:orders:create-order", payload["items"][0]["semantic_id"])
        self.assertGreaterEqual(payload["items"][0]["confidence"], 0.8)
        self.assertTrue(payload["items"][0]["reasons"])

        invalid = self.client.put(
            f"/api/graph/business-semantic-mappings/{target_id}",
            json={"semantic_id": "semantic:missing", "author_name": "tester"},
        )
        selected = self.client.put(
            f"/api/graph/business-semantic-mappings/{target_id}?locale=vi",
            json={"semantic_id": "semantic:orders:create-order", "author_name": "tester"},
        )
        repeated = self.client.put(
            f"/api/graph/business-semantic-mappings/{target_id}?locale=vi",
            json={"semantic_id": "semantic:orders:create-order", "author_name": "tester"},
        )
        self.assertEqual(400, invalid.status_code)
        self.assertEqual(200, selected.status_code, selected.get_data(as_text=True))
        self.assertEqual(selected.get_json(), repeated.get_json())
        self.assertEqual("Tạo đơn hàng", selected.get_json()["items"][0]["label"])

        detail = self.client.get("/api/graph/detail", query_string={"target_id": target_id, "locale": "vi"})
        self.assertEqual(200, detail.status_code, detail.get_data(as_text=True))
        self.assertEqual("semantic:orders:create-order", detail.get_json()["business_semantics"][0]["semantic_id"])
        self.assertEqual("Tạo đơn hàng", detail.get_json()["semantic_tree_business_semantics"][target_id]["label"])
        self.assertEqual("CREATE_ORDER", detail.get_json()["semantic_tree_nodes"][target_id]["technical_name"])
        with closing(sqlite3.connect(self.db)) as db:
            persisted = db.execute(
                "SELECT semantic_id, author_name FROM graph_business_semantic_mappings WHERE target_id=?",
                (target_id,),
            ).fetchone()
        self.assertEqual(("semantic:orders:create-order", "tester"), persisted)
        after_selection = self.client.get("/api/graph/business-semantic-suggestions", query_string={"target_id": target_id, "locale": "vi"})
        self.assertEqual("semantic:orders:create-order", after_selection.get_json()["items"][0]["semantic_id"])
        self.assertEqual("semantic:orders:create-order", after_selection.get_json()["selected"][0]["semantic_id"])

if __name__ == "__main__":
    unittest.main()
