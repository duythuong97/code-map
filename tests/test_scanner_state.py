import copy
import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from common.config import load_pipeline_config, source_rule_hash
from db.entities import ExtractionResult, GraphEdge, GraphNode
from db.writer import ensure_db_schema, replace_file_result
from extractors.scanner import (
    cleanup_missing_files,
    extractor_registry,
    manifest_candidates,
    manifest_relative_path,
    normalize_manifest,
    prepare_sources,
    process_file,
    process_sources,
    read_manifest,
    run_incremental,
)
from extractors.state import (
    ExtractionLeaseLost,
    LeaseHeartbeat,
    acquire_lease,
    begin_run,
    finish_run,
    get_or_create_file,
    increment_run,
    mark_file_failed,
    mark_source_scanned,
    record_file_attempt,
    register_source,
    update_run_current,
    utc_now,
)


def source_config(source_id: str, path: Path, extractor: str = "xml_sql") -> dict:
    owners = {
        "xml_sql": "file",
        "csharp_sql": "repository_or_project",
        "oracle_plsql": "callable_or_file",
    }
    patterns = {
        "xml_sql": ["**/*.xml"],
        "csharp_sql": ["**/*.cs"],
        "oracle_plsql": ["**/*.sql"],
    }
    return {
        "id": source_id,
        "path": str(path),
        "repo": "repo",
        "db_name": "DB",
        "schema": "HR",
        "priority": 0,
        "exclude": [],
        "rules": [
            {
                "patterns": patterns[extractor],
                "extractor": extractor,
                "owner": owners[extractor],
            }
        ],
    }


def pipeline_config(root: Path) -> dict:
    return {
        "db": "artifacts/code.db",
        "extractors": {
            "state": {
                "log_path": "logs/extraction.log",
                "lease_stale_seconds": 120,
                "heartbeat_seconds": 15,
                "log_max_bytes": 1024,
                "log_backups": 1,
            },
            "features": {"antlr_plsql_calls": False},
            "sources": [source_config("source", root)],
        },
    }


class ConfigValidationTest(unittest.TestCase):
    def test_invalid_runtime_config_fails_before_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            config = pipeline_config(source)
            config["extractors"]["state"]["heartbeat_seconds"] = 120
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be less"):
                run_incremental(config_path)

            self.assertFalse((root / "artifacts").exists())
            self.assertFalse((root / "logs").exists())

    def test_runtime_config_types_and_ranges_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            base = pipeline_config(source)
            cases = [
                (("features",), [], "features must be an object"),
                (("features", "antlr_plsql_calls"), 1, "must be a boolean"),
                (("state",), [], "state must be an object"),
                (("state", "lease_stale_seconds"), True, "positive number"),
                (("state", "lease_stale_seconds"), float("nan"), "finite positive number"),
                (("state", "heartbeat_seconds"), float("inf"), "finite positive number"),
                (("state", "heartbeat_seconds"), 0, "positive number"),
                (("state", "log_max_bytes"), 0, "positive integer"),
                (("state", "log_backups"), -1, "non-negative integer"),
                (("sources",), {}, "sources must be a list"),
            ]
            for keys, value, message in cases:
                with self.subTest(keys=keys):
                    config = copy.deepcopy(base)
                    target = config["extractors"]
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    path = root / "config.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_pipeline_config(path)


    def test_non_string_scalar_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            cases = [
                ("db", "db"),
                ("repo", "repo"),
                ("db_name", "db_name"),
                ("schema", "schema"),
            ]
            for target, message in cases:
                with self.subTest(target=target):
                    config = pipeline_config(source)
                    if target == "db":
                        config["db"] = 1
                    else:
                        config["extractors"]["sources"][0][target] = 1
                    path = root / "config.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_pipeline_config(path)

    def test_falsy_malformed_nested_containers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for target, message in [
                ("exclude", r"extractors.sources\[0\].exclude"),
                ("imports", "imports.csv must be a list"),
            ]:
                with self.subTest(target=target):
                    config = pipeline_config(source)
                    if target == "exclude":
                        config["extractors"]["sources"][0]["exclude"] = {}
                    else:
                        config["imports"] = {"csv": {}}
                    path = root / "config.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_pipeline_config(path)

    def test_import_encoding_and_roots_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one-alias").symlink_to(root / "one", target_is_directory=True)
            base = pipeline_config(source)

            unsupported = copy.deepcopy(base)
            unsupported["imports"] = {"csv": [self.import_entry(root / "one", "utf-16")]}
            self.write_config(root, unsupported)
            with self.assertRaisesRegex(ValueError, "encoding is unsupported"):
                load_pipeline_config(root / "config.json")

            for second in (
                root / "one" / ".",
                root / "one" / "nested",
                root / "one" / ".." / "one",
                root / "one-alias",
            ):
                with self.subTest(second=second):
                    overlapping = copy.deepcopy(base)
                    overlapping["imports"] = {
                        "csv": [
                            self.import_entry(root / "one", "utf-8", "first"),
                            self.import_entry(second, "cp932", "second"),
                        ]
                    }
                    self.write_config(root, overlapping)
                    with self.assertRaisesRegex(ValueError, "Overlapping import path"):
                        load_pipeline_config(root / "config.json")

            independent = copy.deepcopy(base)
            independent["imports"] = {
                "csv": [
                    self.import_entry(root / "one", "shift_jis", "first"),
                    self.import_entry(root / "two", "euc_jp", "second"),
                ]
            }
            self.write_config(root, independent)
            self.assertEqual(independent, load_pipeline_config(root / "config.json"))

    @staticmethod
    def import_entry(path: Path, encoding: str, import_id: str = "metadata") -> dict:
        return {
            "id": import_id,
            "path": str(path),
            "kind": "table_definitions",
            "db_name": "DB",
            "schema": "HR",
            "encoding": encoding,
        }

    @staticmethod
    def write_config(root: Path, config: dict) -> None:
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")

class ManifestSafetyTest(unittest.TestCase):
    def test_hidden_path_is_preserved_and_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = source_config("source", root)
            self.assertEqual(
                ".hidden/query.xml",
                manifest_relative_path(".hidden/query.xml", root, source),
            )
            with self.assertRaisesRegex(ValueError, "traversal"):
                manifest_relative_path("../outside/query.xml", root, source)
            with self.assertRaisesRegex(ValueError, "traversal"):
                manifest_relative_path(
                    str(root / "nested" / ".." / "query.xml"), root, source
                )

    def test_canonical_manifest_aliases_dedupe_or_reject_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = source_config("source", root)
            absolute = str(root / "query.xml")
            self.assertEqual(
                {"source": {"query.xml": "M"}},
                normalize_manifest(
                    [(source, root)], {"query.xml": "M", absolute: "M"}
                ),
            )
            with self.assertRaisesRegex(ValueError, "Contradictory"):
                normalize_manifest(
                    [(source, root)], {"query.xml": "M", absolute: "D"}
                )

    def test_relative_source_root_manifest_round_trip_keeps_all_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "samples"
            root.mkdir()
            (root / "added.xml").write_text("<select/>", encoding="utf-8")
            (root / "modified.xml").write_text("<select/>", encoding="utf-8")
            source = source_config("source", root)
            source["path"] = "nested/../samples"
            normalized = normalize_manifest(
                [(source, root)],
                {
                    "samples/added.xml": "A",
                    "samples/modified.xml": "M",
                    "samples/deleted.xml": "D",
                },
            )
            db = sqlite3.connect(":memory:")
            ensure_db_schema(db)
            candidates = list(
                manifest_candidates(db, root, source, normalized["source"])
            )
            self.assertEqual(
                [
                    ("A", "added.xml"),
                    ("D", "deleted.xml"),
                    ("M", "modified.xml"),
                ],
                [(action, relative) for action, _, relative, _ in candidates],
            )
            db.close()

    def test_unmatched_and_multiple_source_manifest_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first = source_config("first", first_root)
            second = source_config("second", second_root)
            sources = [(first, first_root), (second, second_root)]
            with self.assertRaisesRegex(ValueError, "no configured source"):
                normalize_manifest(sources, {str(base / "outside.xml"): "M"})
            with self.assertRaisesRegex(ValueError, "multiple sources"):
                normalize_manifest(sources, {"query.xml": "M"})

    def test_contradictory_duplicate_manifest_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.txt"
            manifest.write_text("A ./query.xml\nD query.xml\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Contradictory"):
                read_manifest(manifest)

    def test_empty_manifest_skips_project_walk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = source_config("source", root, "csharp_sql")
            db = sqlite3.connect(":memory:")
            ensure_db_schema(db)
            run_id = begin_run(db, "config", "full")
            lease = acquire_lease(db, run_id)
            register_source(
                db,
                lease=lease,
                source_id=source["id"],
                kind="source",
                source_root=str(root),
                rule_hash=source_rule_hash(source),
                priority=0,
            )
            with patch("extractors.scanner.build_project_index") as build_index:
                prepared = prepare_sources(
                    db, [source], root, {}, extractor_registry({})
                )
            self.assertEqual([], prepared[0][2])
            self.assertEqual([], prepared[0][3])
            build_index.assert_not_called()
            db.close()

    def test_full_csharp_scan_maps_nearest_project_in_one_walk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            nested = root / "nested"
            nested.mkdir()
            (root / "Parent.csproj").write_text(
                "<Project><PropertyGroup><AssemblyName>Parent.App</AssemblyName>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            (nested / "Child.csproj").write_text(
                "<Project><PropertyGroup><AssemblyName>Child.App</AssemblyName>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            (root / "Parent.cs").write_text("class Parent {}", encoding="utf-8")
            (nested / "Child.cs").write_text("class Child {}", encoding="utf-8")
            source = source_config("source", root, "csharp_sql")
            config = {"extractors": {"sources": [source]}}
            db = sqlite3.connect(":memory:")
            ensure_db_schema(db)
            run_id = begin_run(db, "config", "full")
            lease = acquire_lease(db, run_id)

            self.assertEqual(
                0,
                process_sources(
                    db,
                    config,
                    root,
                    run_id,
                    lease,
                    None,
                    False,
                    Mock(),
                    logging.getLogger("single-project-walk-test"),
                ),
            )
            rows = dict(
                db.execute(
                    "SELECT relative_path,project_name FROM extraction_files ORDER BY relative_path"
                ).fetchall()
            )
            self.assertEqual("Parent.App", rows["Parent.cs"])
            self.assertEqual("Child.App", rows["nested/Child.cs"])
            db.close()

    def test_empty_full_scan_deletes_last_file_without_open_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            file_path = root / "query.xml"
            file_path.write_text(
                "<select>SELECT * FROM HR.EMPLOYEE</select>", encoding="utf-8"
            )
            source = source_config("source", root)
            config = {"extractors": {"sources": [source]}}
            db = sqlite3.connect(":memory:")
            ensure_db_schema(db)
            run_id = begin_run(db, "config", "full")
            lease = acquire_lease(db, run_id)
            heartbeat = Mock()

            self.assertEqual(
                0,
                process_sources(
                    db,
                    config,
                    root,
                    run_id,
                    lease,
                    None,
                    False,
                    heartbeat,
                    logging.getLogger("empty-full-scan-test"),
                ),
            )
            file_path.unlink()
            self.assertEqual(
                0,
                process_sources(
                    db,
                    config,
                    root,
                    run_id,
                    lease,
                    None,
                    False,
                    heartbeat,
                    logging.getLogger("empty-full-scan-test"),
                ),
            )
            status = db.execute(
                "SELECT status FROM extraction_files WHERE relative_path='query.xml'"
            ).fetchone()[0]
            facts = db.execute("SELECT COUNT(*) FROM edge_facts").fetchone()[0]
            self.assertEqual("deleted", status)
            self.assertEqual(0, facts)
            db.close()

    def test_all_sources_are_preflighted_before_file_state_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = root / "first"
            first.mkdir()
            (first / "query.xml").write_text("<select/>", encoding="utf-8")
            missing = root / "missing"
            config = {
                "extractors": {
                    "sources": [
                        source_config("first", first),
                        source_config("missing", missing),
                    ]
                }
            }
            db = sqlite3.connect(":memory:")
            ensure_db_schema(db)
            run_id = begin_run(db, "config", "full")
            lease = acquire_lease(db, run_id)

            with self.assertRaisesRegex(ValueError, "not a directory"):
                process_sources(
                    db,
                    config,
                    root,
                    run_id,
                    lease,
                    None,
                    False,
                    object(),
                    logging.getLogger("scanner-preflight-test"),
                )

            count = db.execute("SELECT COUNT(*) FROM extraction_files").fetchone()[0]
            self.assertEqual(0, count)
            db.close()


class ExtractionTruthTest(unittest.TestCase):
    def test_failed_handler_transition_preserves_old_file_until_replacement_succeeds(self):
        db = sqlite3.connect(":memory:")
        ensure_db_schema(db)
        run_id = begin_run(db, "config", "full")
        lease = acquire_lease(db, run_id)
        old = get_or_create_file(
            db,
            lease=lease,
            source_id="source",
            source_root="/source",
            relative_path="query.xml",
            handler_name="xml_sql",
        )
        replacement = get_or_create_file(
            db,
            lease=lease,
            source_id="source",
            source_root="/source",
            relative_path="query.xml",
            handler_name="csharp_sql",
        )
        replace_file_result(
            db,
            lease=lease,
            file_id=old.id,
            run_id=run_id,
            result=ExtractionResult(
                nodes=[
                    GraphNode("SourceFile", "qualified_name", "source:query.xml"),
                    GraphNode("Table", "qualified_name", "Table:DB:HR.EMPLOYEE"),
                ],
                edges=[
                    GraphEdge(
                        "SourceFile",
                        "qualified_name",
                        "source:query.xml",
                        "Table",
                        "qualified_name",
                        "Table:DB:HR.EMPLOYEE",
                        "READS_FROM",
                    )
                ],
            ),
            size=1,
            mtime_ns=1,
            sha256="old",
            rule_context_hash="old",
            handler_version="old",
        )
        db.execute("UPDATE extraction_files SET status='failed' WHERE id=?", (replacement.id,))
        db.commit()
        db.execute(
            "CREATE TEMP TABLE seen_files("
            "source_id TEXT,relative_path TEXT,handler_name TEXT,"
            "PRIMARY KEY(source_id,relative_path,handler_name)) WITHOUT ROWID"
        )
        db.execute("INSERT INTO seen_files VALUES('source','query.xml','csharp_sql')")
        db.commit()

        cleanup_missing_files(
            db, "source", run_id, lease, logging.getLogger("handler-transition-test")
        )
        self.assertEqual(
            "success",
            db.execute(
                "SELECT status FROM extraction_files WHERE id=?", (old.id,)
            ).fetchone()[0],
        )
        self.assertEqual(
            (2, 1, 1),
            (
                db.execute(
                    "SELECT COUNT(*) FROM node_facts WHERE file_id=?", (old.id,)
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM edge_facts WHERE file_id=?", (old.id,)
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM edges WHERE from_qname='source:query.xml'"
                ).fetchone()[0],
            ),
        )

        db.execute(
            "UPDATE extraction_files SET status='success' WHERE id=?", (replacement.id,)
        )
        db.commit()
        cleanup_missing_files(
            db, "source", run_id, lease, logging.getLogger("handler-transition-test")
        )
        self.assertEqual(
            "deleted",
            db.execute(
                "SELECT status FROM extraction_files WHERE id=?", (old.id,)
            ).fetchone()[0],
        )
        self.assertEqual(
            (0, 0, 0),
            (
                db.execute(
                    "SELECT COUNT(*) FROM node_facts WHERE file_id=?", (old.id,)
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM edge_facts WHERE file_id=?", (old.id,)
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM edges WHERE from_qname='source:query.xml'"
                ).fetchone()[0],
            ),
        )
        db.close()

    def test_post_commit_telemetry_failure_does_not_mark_file_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            file_path = root / "query.xml"
            file_path.write_text(
                "<select>SELECT * FROM HR.EMPLOYEE</select>", encoding="utf-8"
            )
            source = source_config("source", root)
            db = sqlite3.connect(":memory:")
            ensure_db_schema(db)
            run_id = begin_run(db, "config", "full")
            lease = acquire_lease(db, run_id)
            file_state = get_or_create_file(
                db,
                lease=lease,
                source_id="source",
                source_root=str(root),
                relative_path="query.xml",
                handler_name="xml_sql",
            )

            with patch(
                "extractors.scanner.record_file_attempt",
                side_effect=RuntimeError("audit unavailable"),
            ), self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                process_file(
                    db,
                    source,
                    root,
                    source["rules"][0],
                    file_path,
                    "query.xml",
                    {"name": "repo", "relative_root": ".", "project_file": ""},
                    file_state,
                    extractor_registry({})["xml_sql"],
                    run_id,
                    lease,
                    False,
                    logging.getLogger("extraction-truth-test"),
                )

            status, last_error = db.execute(
                "SELECT status,last_error FROM extraction_files WHERE id=?",
                (file_state.id,),
            ).fetchone()
            edge_count = db.execute(
                "SELECT COUNT(*) FROM edge_facts WHERE file_id=?", (file_state.id,)
            ).fetchone()[0]
            failed_count = db.execute(
                "SELECT failed_count FROM extraction_runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            self.assertEqual(("success", None), (status, last_error))
            self.assertEqual(1, edge_count)
            self.assertEqual(0, failed_count)
            db.close()

class LeaseFencingTest(unittest.TestCase):
    def test_takeover_fences_old_heartbeat_and_state_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "state.db"
            db = sqlite3.connect(db_path)
            ensure_db_schema(db)
            old_run = begin_run(db, "old", "full")
            old_lease = acquire_lease(db, old_run)
            register_source(
                db,
                lease=old_lease,
                source_id="source",
                kind="source",
                source_root="/source",
                rule_hash="rules",
                priority=0,
            )
            file_state = get_or_create_file(
                db,
                lease=old_lease,
                source_id="source",
                source_root="/source",
                relative_path="query.xml",
                handler_name="xml_sql",
            )
            db.execute(
                "UPDATE extraction_lease SET heartbeat_at='2000-01-01T00:00:00+00:00'"
            )
            db.commit()
            new_run = begin_run(db, "new", "full")
            new_lease = acquire_lease(db, new_run, stale_after_seconds=1)

            with self.assertRaises(ExtractionLeaseLost):
                LeaseHeartbeat(db_path, old_lease).check()

            stale_writes = [
                lambda: register_source(
                    db,
                    lease=old_lease,
                    source_id="other",
                    kind="source",
                    source_root="/other",
                    rule_hash="other",
                    priority=0,
                ),
                lambda: mark_source_scanned(db, old_lease, "source", old_run),
                lambda: get_or_create_file(
                    db,
                    lease=old_lease,
                    source_id="source",
                    source_root="/source",
                    relative_path="other.xml",
                    handler_name="xml_sql",
                ),
                lambda: update_run_current(
                    db, old_lease, old_run, "source", "project", "query.xml"
                ),
                lambda: increment_run(db, old_lease, old_run, discovered_count=1),
                lambda: record_file_attempt(
                    db,
                    lease=old_lease,
                    run_id=old_run,
                    file_id=file_state.id,
                    action="failed",
                    status="failed",
                    started_at=utc_now(),
                    elapsed_ms=1,
                ),
                lambda: mark_file_failed(db, old_lease, file_state.id, "stale"),
                lambda: finish_run(db, old_lease, old_run, "failed", "stale"),
            ]
            for write in stale_writes:
                with self.assertRaises(ExtractionLeaseLost):
                    write()

            increment_run(db, new_lease, new_run, discovered_count=1)
            old = db.execute(
                "SELECT status, discovered_count, current_relative_path FROM extraction_runs WHERE id=?",
                (old_run,),
            ).fetchone()
            new = db.execute(
                "SELECT status, discovered_count FROM extraction_runs WHERE id=?",
                (new_run,),
            ).fetchone()
            file_row = db.execute(
                "SELECT status, last_error FROM extraction_files WHERE id=?",
                (file_state.id,),
            ).fetchone()
            attempts = db.execute(
                "SELECT COUNT(*) FROM extraction_run_files WHERE run_id=?", (old_run,)
            ).fetchone()[0]
            other_sources = db.execute(
                "SELECT COUNT(*) FROM extraction_sources WHERE source_id='other'"
            ).fetchone()[0]

            self.assertEqual(("interrupted", 0, None), old)
            self.assertEqual(("running", 1), new)
            self.assertEqual(("new", None), file_row)
            self.assertEqual(0, attempts)
            self.assertEqual(0, other_sources)
            db.close()


if __name__ == "__main__":
    unittest.main()
