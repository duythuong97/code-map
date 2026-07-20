import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extractors.run_all import run_pipeline


class UnifiedCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.metadata = self.root / "metadata"
        self.sources = self.root / "sources"
        self.metadata.mkdir()
        self.sources.mkdir()
        self.csv = self.metadata / "tables.csv"
        self.xml = self.sources / "query.xml"
        self.write_csv("Employee")
        self.xml.write_text(
            "<select>SELECT * FROM HR.EMPLOYEE</select>", encoding="utf-8"
        )
        self.config_path = self.root / "config.json"
        self.write_config()

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self, source_path: Path | None = None):
        config = {
            "db": "code.db",
            "imports": {
                "csv": [
                    {
                        "id": "metadata",
                        "path": str(self.metadata),
                        "kind": "table_definitions",
                        "db_name": "DB",
                        "schema": "HR",
                        "priority": 0,
                    }
                ]
            },
            "extractors": {
                "state": {
                    "lease_stale_seconds": 120,
                    "heartbeat_seconds": 15,
                    "log_max_bytes": 1024,
                    "log_backups": 1,
                },
                "sources": [
                    {
                        "id": "source",
                        "path": str(source_path or self.sources),
                        "repo": "repo",
                        "db_name": "DB",
                        "schema": "HR",
                        "priority": 0,
                        "exclude": [],
                        "rules": [
                            {
                                "patterns": ["**/*.xml"],
                                "extractor": "xml_sql",
                                "owner": "file",
                            }
                        ],
                    }
                ],
            },
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def write_csv(self, name: str):
        self.csv.write_text(
            "table_code,table_name_en\nEMPLOYEE," + name + "\n",
            encoding="utf-8",
        )

    def connect(self):
        return sqlite3.connect(self.root / "code.db")

    def test_one_lifecycle_imports_first_and_stat_skip(self):
        first = run_pipeline(self.config_path)
        self.assertEqual("completed", first["status"])
        self.assertEqual(2, first["discovered_count"])
        self.assertEqual(2, first["extracted_count"])

        with self.connect() as db:
            self.assertEqual((1,), db.execute("SELECT COUNT(*) FROM extraction_runs").fetchone())
            attempts = db.execute(
                """
                SELECT f.source_id FROM extraction_run_files a
                JOIN extraction_files f ON f.id=a.file_id
                WHERE a.run_id=? ORDER BY a.id
                """,
                (first["id"],),
            ).fetchall()
            lease = db.execute(
                "SELECT owner_run_id FROM extraction_lease WHERE singleton=1"
            ).fetchone()
        self.assertEqual([("metadata",), ("source",)], attempts)
        self.assertEqual((None,), lease)

        with patch(
            "extractors.run_all.parse_metadata_facts",
            side_effect=AssertionError("stat skip must not parse"),
        ):
            second = run_pipeline(self.config_path)
        self.assertEqual("completed", second["status"])
        self.assertEqual(2, second["skipped_count"])
        with self.connect() as db:
            attempts = db.execute(
                "SELECT COUNT(*) FROM extraction_run_files WHERE run_id=?",
                (second["id"],),
            ).fetchone()
        self.assertEqual((0,), attempts)

    def test_atomic_replace_same_size_and_mtime_is_not_stat_skipped(self):
        run_pipeline(self.config_path)
        original = self.xml.stat()
        replacement = self.sources / "replacement.xml"
        replacement.write_text(
            "<select>SELECT * FROM HR.TAX_RATE</select>", encoding="utf-8"
        )
        self.assertEqual(original.st_size, replacement.stat().st_size)
        os.utime(
            replacement,
            ns=(replacement.stat().st_atime_ns, original.st_mtime_ns),
        )
        os.replace(replacement, self.xml)

        changed = run_pipeline(self.config_path)
        self.assertEqual(1, changed["extracted_count"])
        self.assertEqual(1, changed["skipped_count"])
        with self.connect() as db:
            targets = db.execute(
                "SELECT to_qname FROM edges WHERE from_qname LIKE 'SourceFile:%'"
            ).fetchall()
        self.assertEqual([("Table:DB:HR.TAX_RATE",)], targets)

    def test_failure_preserves_facts_then_retries_and_delete_cleans(self):
        run_pipeline(self.config_path)
        with self.connect() as db:
            before = db.execute(
                "SELECT successful_sha256 FROM extraction_files "
                "WHERE source_id='metadata'"
            ).fetchone()

        self.csv.write_text("unknown\nvalue\n", encoding="utf-8")
        os.utime(self.csv, ns=(self.csv.stat().st_atime_ns, self.csv.stat().st_mtime_ns + 1_000_000))
        failed = run_pipeline(self.config_path)
        self.assertEqual("completed_with_errors", failed["status"])
        with self.connect() as db:
            preserved = db.execute(
                "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
            ).fetchone()
            fingerprint = db.execute(
                "SELECT successful_sha256 FROM extraction_files "
                "WHERE source_id='metadata'"
            ).fetchone()
        self.assertEqual(("Employee",), preserved)
        self.assertEqual(before, fingerprint)

        self.write_csv("Changed")
        retried = run_pipeline(self.config_path)
        self.assertEqual("completed", retried["status"])
        with self.connect() as db:
            changed = db.execute(
                "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
            ).fetchone()
        self.assertEqual(("Changed",), changed)

        self.csv.unlink()
        deleted = run_pipeline(self.config_path)
        self.assertEqual("completed", deleted["status"])
        self.assertEqual(1, deleted["deleted_count"])
        with self.connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT id FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
                ).fetchone()
            )

    def test_fatal_source_preflight_keeps_committed_import_in_same_failed_run(self):
        missing = self.root / "missing-source"
        self.write_config(missing)
        with self.assertRaisesRegex(ValueError, "Source path is not a directory"):
            run_pipeline(self.config_path)

        with self.connect() as db:
            runs = db.execute("SELECT status FROM extraction_runs").fetchall()
            table = db.execute(
                "SELECT name_en FROM table_definitions WHERE id='DB:HR.EMPLOYEE'"
            ).fetchone()
            lease = db.execute(
                "SELECT owner_run_id FROM extraction_lease WHERE singleton=1"
            ).fetchone()
        self.assertEqual([("failed",)], runs)
        self.assertEqual(("Employee",), table)
        self.assertEqual((None,), lease)


if __name__ == "__main__":
    unittest.main()
