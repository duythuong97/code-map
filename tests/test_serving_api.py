import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import api.app as serving
from db.writer import ensure_db_schema
from extractors.state import ensure_state_schema


SOURCE = "SourceFile:source-a:src/pkg.pkb"
SOURCE_DUP = "SourceFile:source-b:other/pkg.pkb"
REPOSITORY = "Repository:repo-a:App.Data:EmployeeRepository"
APPLICATION = "Application:repo-a:Sample.Data"
TABLE = "Table:DB:HR.EMPLOYEE"
READ = "READS_FROM"


class ServingApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root_a = self.base / "root-a"
        self.root_b = self.base / "root-b"
        self.root_a.mkdir()
        (self.root_a / "src").mkdir()
        (self.root_a / "other").mkdir()
        (self.root_a / "src" / "pkg.pkb").write_text(
            "line one\nselect employee_id\nline three\n", encoding="utf-8"
        )
        (self.root_a / "other" / "pkg.pkb").write_text("duplicate\n", encoding="utf-8")
        (self.root_a / "src" / "Repository.cs").write_text("CallStoredProcedure();\n", encoding="utf-8")
        (self.root_a / "src" / "Other.cs").write_text("Other();\n", encoding="utf-8")
        self.db_path = self.base / "serving.db"
        db = sqlite3.connect(self.db_path)
        ensure_db_schema(db)
        ensure_state_schema(db)
        self._seed(db)
        db.close()
        self.old_db_path = serving.DB_PATH
        serving.DB_PATH = self.db_path
        serving.app.config.update(TESTING=True)
        self.client = serving.app.test_client()

    def tearDown(self):
        serving.DB_PATH = self.old_db_path
        self.temporary.cleanup()

    def _seed(self, db):
        for source_id, root in (("source-a", self.root_a), ("source-b", self.root_a)):
            db.execute(
                "INSERT INTO extraction_sources(source_id,kind,source_root,source_rule_hash) VALUES(?,?,?,?)",
                (source_id, "filesystem", str(root), "rules"),
            )
        files = [
            ("source-a", "src/pkg.pkb", "oracle", "Payroll"),
            ("source-b", "other/pkg.pkb", "oracle", "Billing"),
            ("source-a", "src/Repository.cs", "csharp", "Sample.Data"),
            ("source-a", "src/Other.cs", "csharp", "Sample.Data"),
        ]
        for source_id, path, handler, project in files:
            db.execute(
                """INSERT INTO extraction_files(
                source_id,source_root,relative_path,handler_name,project_name,status,updated_at
                ) VALUES(?,?,?,?,?,'success','now')""",
                (source_id, str(self.root_a), path, handler, project),
            )
        self.file_ids = {
            row[1]: row[0]
            for row in db.execute("SELECT id,relative_path FROM extraction_files")
        }
        nodes = [
            ("SourceFile", SOURCE, "pkg.pkb", {"source_id": "source-a", "source_path": "src/pkg.pkb", "repository": "repo-a"}),
            ("SourceFile", SOURCE_DUP, "pkg.pkb", {"source_id": "source-b", "source_path": "other/pkg.pkb", "repository": "repo-b"}),
            ("Repository", REPOSITORY, "EmployeeRepository", {"namespace": "App.Data", "repository": "repo-a", "project": "Sample.Data", "unresolved_sql": True}),
            ("Application", APPLICATION, "Sample.Data", {"repository": "repo-a", "project": "Sample.Data"}),
            ("Table", TABLE, "EMPLOYEE", {"schema": "HR", "db_name": "DB"}),
        ]
        for label, qname, name, properties in nodes:
            db.execute(
                "INSERT INTO nodes(label,qualified_name,name,properties_json) VALUES(?,?,?,?)",
                (label, qname, name, json.dumps(properties)),
            )
        facts = [
            (self.file_ids["src/pkg.pkb"], SOURCE, "SourceFile", "pkg.pkb", nodes[0][3]),
            (self.file_ids["other/pkg.pkb"], SOURCE_DUP, "SourceFile", "pkg.pkb", nodes[1][3]),
            (self.file_ids["src/Repository.cs"], REPOSITORY, "Repository", "EmployeeRepository", nodes[2][3]),
            (self.file_ids["src/Repository.cs"], APPLICATION, "Application", "Sample.Data", nodes[3][3]),
            (self.file_ids["src/Other.cs"], APPLICATION, "Application", "Sample.Data", nodes[3][3]),
        ]
        for file_id, qname, label, name, properties in facts:
            db.execute(
                "INSERT INTO node_facts(file_id,qname,label,name,properties_json,source,priority) VALUES(?,?,?,?,?,'test',1)",
                (file_id, qname, label, name, json.dumps(properties)),
            )
        canonical = {
            "operation": "SELECT",
            "columns": ["EMPLOYEE_ID"],
            "evidence_count": 101,
            "evidence_truncated": True,
        }
        db.execute(
            "INSERT INTO edges(from_qname,to_qname,rel_type,properties_json,source_file,line) VALUES(?,?,?,?,?,?)",
            (SOURCE, TABLE, READ, json.dumps(canonical), "src/pkg.pkb", 2),
        )
        for index in range(101):
            properties = {
                "operation": "SELECT",
                "columns": [f"COL_{index:03d}"],
                "query_id": f"query-{index:03d}",
                "mapper_tag": "select",
                "confidence": "high",
            }
            db.execute(
                """INSERT INTO edge_facts(
                file_id,fact_key,from_qname,to_qname,rel_type,properties_json,source_path,line,priority
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    self.file_ids["src/pkg.pkb"], f"fact-{index:03d}", SOURCE, TABLE,
                    READ, json.dumps(properties), "src/pkg.pkb", index + 1, 1,
                ),
            )
        call_properties = {"operation": "CALL", "call_type": "stored_procedure", "expression": "PKG_EMP.LOAD"}
        db.execute(
            "INSERT INTO edges(from_qname,to_qname,rel_type,properties_json,source_file,line) VALUES(?,?,?,?,?,?)",
            (REPOSITORY, TABLE, "CALLS", json.dumps(call_properties), "src/Repository.cs", 1),
        )
        db.execute(
            """INSERT INTO edge_facts(
            file_id,fact_key,from_qname,to_qname,rel_type,properties_json,source_path,line,priority
            ) VALUES(?,?,?,?,?,?,?,?,1)""",
            (self.file_ids["src/Repository.cs"], "call-fact", REPOSITORY, TABLE, "CALLS", json.dumps(call_properties), "src/Repository.cs", 1),
        )
        db.commit()

    def get_json(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(status, response.status_code, response.get_data(as_text=True))
        return response.get_json()

    def test_owner_contract_roots_search_and_one_hop_display(self):
        contract = self.get_json("/api/graph-contract")
        for label in ("SourceFile", "Repository", "Application"):
            self.assertTrue(contract["nodes"][label]["visible"])
        roots = self.get_json("/api/roots")
        owners = {item["qualified_name"]: item for item in roots}
        self.assertEqual("pkg.pkb", owners[SOURCE]["display"]["title"])
        self.assertNotEqual(owners[SOURCE]["display"]["scope"], owners[SOURCE_DUP]["display"]["scope"])
        self.assertEqual({SOURCE, SOURCE_DUP}, {item["qualified_name"] for item in self.get_json("/api/search?q=pkg.pkb")})
        flow = self.get_json(f"/api/flow?qname={SOURCE}")
        self.assertEqual(SOURCE, flow["node"]["qualified_name"])
        self.assertEqual(TABLE, flow["flows"][0]["to_qname"])
        self.assertEqual("pkg.pkb", flow["flows"][0]["from_display"]["title"])

    def test_typed_owner_details_are_aggregated_without_fake_schema(self):
        source = self.get_json(f"/api/node-detail?qname={SOURCE}")
        self.assertEqual("src/pkg.pkb", source["properties"]["source_path"])
        self.assertEqual("source-a", source["properties"]["source_id"])
        self.assertEqual("plsql", source["properties"]["language"])
        self.assertEqual("oracle", source["properties"]["extractor"])
        self.assertNotIn("schema", source["properties"])
        repository = self.get_json(f"/api/node-detail?qname={REPOSITORY}")
        self.assertEqual("EmployeeRepository", repository["properties"]["class_name"])
        self.assertEqual("App.Data", repository["properties"]["namespace"])
        self.assertEqual("dynamic_sql_unresolved", repository["warnings"][0]["code"])
        self.assertNotIn("method", repository["properties"])
        application = self.get_json(f"/api/node-detail?qname={APPLICATION}")
        self.assertEqual(2, application["counts"]["sources"])
        self.assertNotIn("namespace", application["properties"])
        self.assertNotIn("class_name", application["properties"])

    def test_edge_metadata_paginates_all_occurrences_stably(self):
        flow = self.get_json(f"/api/flow?qname={SOURCE}")["flows"][0]
        self.assertEqual(READ, flow["rel_type"])
        self.assertEqual("READS", flow["flow_type"])
        self.assertEqual(101, flow["evidence_count"])
        self.assertEqual(100, len(flow["evidence"]))
        self.assertTrue(flow["evidence_truncated"])
        first_page = self.get_json(
            f"/api/edge-evidence?from_qname={SOURCE}&to_qname={TABLE}&rel_type={READ}&limit=40"
        )
        second_page = self.get_json(
            f"/api/edge-evidence?from_qname={SOURCE}&to_qname={TABLE}&rel_type={READ}&limit=40&cursor={first_page['next_cursor']}"
        )
        third_page = self.get_json(
            f"/api/edge-evidence?from_qname={SOURCE}&to_qname={TABLE}&rel_type={READ}&limit=40&cursor={second_page['next_cursor']}"
        )
        evidence = first_page["evidence"] + second_page["evidence"] + third_page["evidence"]
        self.assertEqual(101, len(evidence))
        self.assertEqual(101, len({item["occurrence_id"] for item in evidence}))
        self.assertEqual("query-000", evidence[0]["query_id"])
        call = self.get_json(f"/api/flow?qname={REPOSITORY}")["flows"][0]
        self.assertEqual("stored_procedure", call["evidence"][0]["call_type"])
        lineage = self.get_json(f"/api/lineage?qname={TABLE}")
        self.assertTrue(lineage["upstream"][0]["evidence"][0]["occurrence_id"].startswith("occ_"))

    def test_snippet_uses_current_root_and_never_returns_absolute_path(self):
        evidence = self.get_json(
            f"/api/edge-evidence?from_qname={SOURCE}&to_qname={TABLE}&rel_type={READ}&limit=1"
        )["evidence"][0]
        occurrence_id = evidence["occurrence_id"]
        self.root_b.mkdir()
        shutil.move(str(self.root_a / "src"), str(self.root_b / "src"))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE extraction_sources SET source_root=? WHERE source_id='source-a'", (str(self.root_b),))
            db.commit()
        snippet = self.get_json(
            f"/api/snippet?from_qname={SOURCE}&to_qname={TABLE}&rel_type={READ}&occurrence_id={occurrence_id}"
        )
        self.assertEqual("available", snippet["status"])
        self.assertEqual("src/pkg.pkb", snippet["source_path"])
        self.assertEqual(1, snippet["focus_line"])
        self.assertNotIn(str(self.base), json.dumps(snippet))

    def test_snippet_rejects_escape_and_reports_missing_or_unreadable(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.sql").write_text("secret", encoding="utf-8")
        (self.root_a / "escape").symlink_to(outside, target_is_directory=True)
        cases = (("/private/secret.sql", "invalid_source"), ("../secret.sql", "invalid_source"), ("escape/secret.sql", "invalid_source"), ("missing.sql", "not_found"))
        for index, (source_path, expected) in enumerate(cases):
            relation = f"SECURITY_{index}"
            occurrence_id = self._insert_occurrence(relation, source_path)
            payload = self.get_json(
                f"/api/snippet?from_qname={SOURCE}&to_qname={TABLE}&rel_type={relation}&occurrence_id={occurrence_id}"
            )
            self.assertEqual(expected, payload["status"])
            self.assertNotIn(str(self.base), json.dumps(payload))
            if source_path.startswith(("/", "..")):
                self.assertIsNone(payload["source_path"])
        unreadable_path = self.root_a / "unreadable.sql"
        unreadable_path.write_text("private", encoding="utf-8")
        occurrence_id = self._insert_occurrence("UNREADABLE", "unreadable.sql")
        with patch("api.app.os.access", return_value=False):
            payload = self.get_json(
                f"/api/snippet?from_qname={SOURCE}&to_qname={TABLE}&rel_type=UNREADABLE&occurrence_id={occurrence_id}"
            )
        self.assertEqual("unreadable", payload["status"])

    def _insert_occurrence(self, relation, source_path):
        fact_key = f"security-{relation}"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO edges(from_qname,to_qname,rel_type,properties_json) VALUES(?,?,?,?)",
                (SOURCE, TABLE, relation, "{}"),
            )
            db.execute(
                """INSERT INTO edge_facts(
                file_id,fact_key,from_qname,to_qname,rel_type,properties_json,source_path,line,priority
                ) VALUES(?,?,?,?,?,?,?,1,1)""",
                (self.file_ids["src/pkg.pkb"], fact_key, SOURCE, TABLE, relation, "{}", source_path),
            )
            db.commit()
        return serving.stable_occurrence_id("source-a", source_path, fact_key)

    def test_recursive_truncation_and_structured_errors(self):
        truncated = self.get_json(f"/api/flow-all?qname={SOURCE}&max_nodes=1")
        self.assertTrue(truncated["truncated"])
        error = self.get_json(f"/api/flow-all?qname={SOURCE}&max_nodes=nope", status=400)
        self.assertEqual("invalid_request", error["error"]["code"])
        missing = self.get_json("/api/node-detail?qname=missing", status=404)
        self.assertEqual("node_not_found", missing["error"]["code"])


if __name__ == "__main__":
    unittest.main()
