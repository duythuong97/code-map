import sqlite3
import unittest

from application.backend.database.writer import (
    MetadataFact,
    delete_file_result,
    ensure_db_schema,
    replace_metadata_file,
)
from application.backend.database.state import acquire_lease, begin_run, get_or_create_file


class MetadataWriterTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        ensure_db_schema(self.db)
        self.run_id = begin_run(self.db, "config", "full")
        self.lease = acquire_lease(self.db, self.run_id)

    def tearDown(self):
        self.db.close()

    def file(self, source_id, path):
        return get_or_create_file(
            self.db,
            lease=self.lease,
            source_id=source_id,
            source_root="/metadata",
            relative_path=path,
            handler_name="table_definitions",
        )

    def replace(self, file_id, facts, priority=0):
        replace_metadata_file(
            self.db,
            lease=self.lease,
            file_id=file_id,
            run_id=self.run_id,
            facts=facts,
            size=1,
            mtime_ns=1,
            sha256="sha",
            rule_context_hash="rules",
            handler_version="metadata-v1",
            priority=priority,
        )

    @staticmethod
    def table_fact(name_en="", description="", row_order=1):
        return MetadataFact(
            "table",
            "DB:HR.EMPLOYEE",
            {
                "db_name": "DB",
                "schema_name": "HR",
                "table_name": "EMPLOYEE",
                "code": "EMPLOYEE",
                "name_en": name_en,
                "description": description,
            },
            row_order,
        )

    def test_precedence_is_priority_source_path_and_row_order(self):
        path_winner = self.file("z-source", "z.csv")
        path_loser = self.file("z-source", "a.csv")
        priority_loser = self.file("zz-source", "low.csv")
        self.replace(path_winner.id, [self.table_fact(name_en="path winner")], 2)
        self.replace(path_loser.id, [self.table_fact(name_en="path loser")], 2)
        self.replace(priority_loser.id, [self.table_fact(name_en="priority loser")], 1)

        row = self.db.execute(
            "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        self.assertEqual(("path winner",), row)

    def test_replace_clears_stale_fields_and_delete_rematerializes(self):
        first = self.file("source-a", "first.csv")
        second = self.file("source-b", "second.csv")
        self.replace(
            first.id, [self.table_fact(name_en="Employee", description="stale")]
        )
        self.replace(second.id, [self.table_fact(name_en="Preferred")])
        self.replace(first.id, [self.table_fact(name_en="Employee")])

        row = self.db.execute(
            "SELECT name_en,description FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        self.assertEqual(("Preferred", ""), row)
        delete_file_result(
            self.db, lease=self.lease, file_id=second.id, run_id=self.run_id
        )
        row = self.db.execute(
            "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        self.assertEqual(("Employee",), row)

    def test_invalid_kind_does_not_replace_old_facts(self):
        file_state = self.file("source", "metadata.csv")
        self.replace(file_state.id, [self.table_fact(name_en="Employee")])

        with self.assertRaisesRegex(ValueError, "Unsupported metadata entity kind"):
            self.replace(
                file_state.id, [MetadataFact("unsupported", "bad", {}, 1)]
            )

        row = self.db.execute(
            "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        self.assertEqual(("Employee",), row)

    def test_failed_replacement_rolls_back_old_facts(self):
        file_state = self.file("source", "metadata.csv")
        self.replace(file_state.id, [self.table_fact(name_en="Employee")])
        self.db.execute(
            "CREATE TRIGGER fail_metadata BEFORE INSERT ON metadata_file_facts "
            "BEGIN SELECT RAISE(ABORT, 'forced'); END"
        )
        self.db.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.replace(file_state.id, [self.table_fact(name_en="Changed")])

        row = self.db.execute(
            "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        self.assertEqual(("Employee",), row)


    def test_writer_rejects_wrong_run_unknown_file_and_wrong_handler(self):
        metadata = self.file("source", "metadata.csv")
        self.replace(metadata.id, [self.table_fact(name_en="Employee")])
        graph = get_or_create_file(
            self.db,
            lease=self.lease,
            source_id="source",
            source_root="/source",
            relative_path="source.sql",
            handler_name="oracle_plsql",
        )
        before = self.db.execute(
            "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        for file_id, run_id, message in (
            (metadata.id, self.run_id + 1, "run_id"),
            (999999, self.run_id, "Unknown extraction file_id"),
            (graph.id, self.run_id, "handler must be table_definitions"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                replace_metadata_file(
                    self.db,
                    lease=self.lease,
                    file_id=file_id,
                    run_id=run_id,
                    facts=[self.table_fact(name_en="Changed")],
                    size=2,
                    mtime_ns=2,
                    sha256="changed",
                    rule_context_hash="changed",
                    handler_version="metadata-writer",
                )
        after = self.db.execute(
            "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
        ).fetchone()
        fingerprint = self.db.execute(
            "SELECT successful_sha256 FROM extraction_files WHERE id=?", (metadata.id,)
        ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(("sha",), fingerprint)


if __name__ == "__main__":
    unittest.main()
