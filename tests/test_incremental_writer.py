import json
import sqlite3
import unittest

from application.backend.database.entities import ExtractionResult, GraphEdge
from application.backend.database.writer import delete_file_result, ensure_db_schema, replace_file_result
from application.backend.database.state import acquire_lease, begin_run, get_or_create_file


FROM = "Procedure:repo:DB:HR:PKG.RUN"
TO = "Table:DB:HR.EMPLOYEE"
REL = "READS_FROM"


class IncrementalWriterTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        ensure_db_schema(self.db)
        self.run_id = begin_run(self.db, "config", "full")
        self.lease = acquire_lease(self.db, self.run_id)

    def tearDown(self):
        self.db.close()

    def file(self, source_id: str, path: str):
        return get_or_create_file(
            self.db,
            lease=self.lease,
            source_id=source_id,
            source_root="/source",
            relative_path=path,
            handler_name="test",
        )

    def replace(self, file_id: int, edges: list[GraphEdge]):
        replace_file_result(
            self.db,
            lease=self.lease,
            file_id=file_id,
            run_id=self.run_id,
            result=ExtractionResult(edges=edges),
            size=1,
            mtime_ns=1,
            sha256="sha",
            rule_context_hash="rules",
            handler_version="handler",
        )

    @staticmethod
    def edge(line: int, **properties):
        return GraphEdge(
            "Procedure",
            "qualified_name",
            FROM,
            "Table",
            "qualified_name",
            TO,
            REL,
            {"line": line, "source_path": "pkg.pkb", **properties},
        )

    def canonical(self):
        rows = self.db.execute(
            "SELECT properties_json FROM edges WHERE from_qname=? AND to_qname=? AND rel_type=?",
            (FROM, TO, REL),
        ).fetchall()
        return rows, json.loads(rows[0][0]) if rows else None

    def test_relation_is_unique_and_evidence_is_aggregated(self):
        first = self.file("source-a", "a.pkb")
        second = self.file("source-b", "b.pkb")
        self.replace(first.id, [self.edge(2, operation="SELECT", columns=["B"])])
        self.replace(second.id, [self.edge(1, columns=["A", "B"])])

        rows, properties = self.canonical()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, properties["evidence_count"])
        self.assertFalse(properties["evidence_truncated"])
        self.assertEqual(["A", "B"], properties["columns"])
        self.assertEqual({"source-a", "source-b"}, {item["source_id"] for item in properties["evidence"]})

    def test_evidence_is_capped_without_losing_count(self):
        file_state = self.file("source", "many.pkb")
        self.replace(file_state.id, [self.edge(line) for line in range(1, 102)])

        rows, properties = self.canonical()
        self.assertEqual(1, len(rows))
        self.assertEqual(101, properties["evidence_count"])
        self.assertEqual(100, len(properties["evidence"]))
        self.assertTrue(properties["evidence_truncated"])

    def test_deletion_keeps_shared_relation_then_removes_last(self):
        first = self.file("source-a", "a.pkb")
        second = self.file("source-b", "b.pkb")
        self.replace(first.id, [self.edge(1)])
        self.replace(second.id, [self.edge(2)])

        delete_file_result(self.db, lease=self.lease, file_id=first.id, run_id=self.run_id)
        rows, properties = self.canonical()
        self.assertEqual(1, len(rows))
        self.assertEqual(1, properties["evidence_count"])
        self.assertEqual("source-b", properties["evidence"][0]["source_id"])

        delete_file_result(self.db, lease=self.lease, file_id=second.id, run_id=self.run_id)
        self.assertEqual(([], None), self.canonical())

    def test_failed_replacement_rolls_back_old_facts(self):
        file_state = self.file("source", "rollback.pkb")
        self.replace(file_state.id, [self.edge(1, operation="SELECT")])
        self.db.execute(
            "CREATE TRIGGER fail_edge_fact BEFORE INSERT ON edge_facts BEGIN SELECT RAISE(ABORT, 'forced'); END"
        )
        self.db.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.replace(file_state.id, [self.edge(2, operation="UPDATE")])

        rows, properties = self.canonical()
        self.assertEqual(1, len(rows))
        self.assertEqual("SELECT", properties["operation"])
        self.assertEqual(1, properties["line"])


if __name__ == "__main__":
    unittest.main()
