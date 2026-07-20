import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extractors.bootstrap_v2 import bootstrap_v2, validate_staging


class BootstrapV2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.live = self.root / "live.db"
        self.live.write_bytes(b"legacy-live-db")
        metadata = self.root / "metadata"
        sources = self.root / "sources"
        metadata.mkdir()
        sources.mkdir()
        (metadata / "tables.csv").write_text(
            "table_code,table_name_en\nEMPLOYEE,Employee\n", encoding="utf-8"
        )
        (sources / "query.xml").write_text(
            "<select>SELECT * FROM HR.EMPLOYEE</select>", encoding="utf-8"
        )
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "db": "live.db",
                    "imports": {
                        "csv": [
                            {
                                "id": "metadata",
                                "path": "metadata",
                                "kind": "table_definitions",
                                "db_name": "DB",
                                "schema": "HR",
                            }
                        ]
                    },
                    "extractors": {
                        "sources": [
                            {
                                "id": "source",
                                "path": "sources",
                                "repo": "repo",
                                "db_name": "DB",
                                "schema": "HR",
                                "rules": [
                                    {
                                        "patterns": ["**/*.xml"],
                                        "extractor": "xml_sql",
                                        "owner": "file",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_success_creates_ready_without_mutating_live(self):
        before = self.live.read_bytes()
        ready, summary = bootstrap_v2(self.config)
        self.assertEqual("completed", summary["status"])
        self.assertEqual(before, self.live.read_bytes())
        self.assertTrue(ready.is_file())
        self.assertFalse(Path(f"{self.live}.v2.tmp").exists())
        self.assertGreaterEqual(validate_staging(ready)["nodes"], 1)

    def test_failure_removes_temporary_without_mutating_live(self):
        before = self.live.read_bytes()

        def fail(_config, **kwargs):
            kwargs["db_override"].write_bytes(b"partial")
            raise RuntimeError("boom")

        with patch("extractors.bootstrap_v2.run_pipeline", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                bootstrap_v2(self.config)

        self.assertEqual(before, self.live.read_bytes())
        self.assertFalse(Path(f"{self.live}.v2.tmp").exists())
        self.assertFalse(Path(f"{self.live}.v2.ready").exists())

    def test_existing_lock_or_temp_is_not_removed(self):
        lock = Path(f"{self.live}.v2.tmp.lock")
        lock.write_text("other process", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "already owns"):
            bootstrap_v2(self.config)
        self.assertEqual("other process", lock.read_text(encoding="utf-8"))
        lock.unlink()

        staging = Path(f"{self.live}.v2.tmp")
        staging.write_bytes(b"other process data")
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            bootstrap_v2(self.config)
        self.assertEqual(b"other process data", staging.read_bytes())
        self.assertFalse(Path(f"{staging}.lock").exists())


if __name__ == "__main__":
    unittest.main()
