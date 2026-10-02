"""The .NET SQL service must return exactly what the Python parser returns."""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.extractors.package_support import dotnet_sql  # noqa: E402
from code_tree_exporter.extractors.package_support import semantic_tree_v3  # noqa: E402
from code_tree_exporter.extractors.package_support.oracle_parser import (  # noqa: E402
    OraclePlsqlParser,
    RemoteOraclePlsqlParser,
)

SAMPLES = [
    "SELECT 1 FROM DUAL;",
    "INSERT INTO APP.ORDERS(ID) SELECT ID FROM APP.STAGING@REMOTE_DB WHERE ID = 1;",
    "MERGE INTO APP.A t USING APP.B s ON (t.ID = s.ID) WHEN MATCHED THEN UPDATE SET t.X = s.X;",
    (PROJECT_ROOT / "sample-source" / "order-db" / "order_flow.sql").read_text(encoding="utf-8"),
    (PROJECT_ROOT / "sample-source" / "order-ops" / "reconcile_orders.sql").read_text(encoding="utf-8"),
    "CREATE OR REPLACE PROCEDURE P(p_id IN NUMBER) IS\n"
    "  v NUMBER := F(p_id);\n"
    "BEGIN\n"
    "  IF v > 0 THEN UPDATE APP.T SET C = v WHERE ID = p_id; ELSE PKG.LOG('😀 none'); END IF;\n"
    "EXCEPTION WHEN OTHERS THEN RAISE;\n"
    "END P;",
]


def _python_view(parser) -> dict:
    rows = lambda items: [dataclasses.asdict(item) for item in items]  # noqa: E731
    return {
        "errors": [tuple(error) for error in parser.syntax_errors],
        "package": parser.package_name(),
        "routines": [
            {**dataclasses.asdict(item), "signature": parser.routine_signature(item)}
            for item in parser.routines()
        ],
        "triggers": rows(parser.triggers()),
        "synonyms": rows(parser.synonyms()),
        "views": rows(parser.views()),
        "tables": rows(parser.table_references()),
        "calls": rows(parser.calls()),
        "sequences": rows(parser.sequences()),
        "dynamic": parser.dynamic_sql_offsets(),
        "executable": parser.has_executable_statement(),
        "classification": parser.script_classification(),
    }


def _unresolved(value):
    if isinstance(value, list):
        return [_unresolved(item) for item in value]
    if isinstance(value, dict):
        result = {key: _unresolved(item) for key, item in value.items() if key != "_ref"}
        if "_ref" in value:
            result["resolution"] = "unresolved"
        return result
    return value


@unittest.skipUnless(
    os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet"), "dotnet not installed"
)
class DotNetSqlParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["CODE_TREE_SQL_PARSER"] = "dotnet"
        cls.service = dotnet_sql.service()

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("CODE_TREE_SQL_PARSER", None)

    def test_parse_summary_matches_python_parser(self) -> None:
        summaries = self.service.request("parse", "texts", SAMPLES)
        for text, summary in zip(SAMPLES, summaries):
            with self.subTest(text=text[:40]):
                self.assertEqual(
                    _python_view(RemoteOraclePlsqlParser(text, summary)),
                    _python_view(OraclePlsqlParser(text)),
                )

    def test_semantic_steps_match_python_projector(self) -> None:
        original = semantic_tree_v3._edge_target
        semantic_tree_v3._edge_target = lambda *args, **kwargs: None
        try:
            for text in SAMPLES:
                with self.subTest(text=text[:40]):
                    remote = self.service.request(
                        "steps", "items", [{"text": text, "source_path": "x.sql", "base_line": 3}]
                    )[0]
                    local = semantic_tree_v3._PlsqlProjector(None, "owner", text, "x.sql", 3).steps()
                    self.assertEqual(_unresolved(remote), local)
        finally:
            semantic_tree_v3._edge_target = original


if __name__ == "__main__":
    unittest.main()
