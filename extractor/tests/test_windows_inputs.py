"""Inputs as they come on Japanese Windows: backslash paths, CP932 CSV, locked folders."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT.parent))

from code_tree_exporter import graph_package  # noqa: E402
from code_tree_exporter.decoding import read_text_auto  # noqa: E402
from code_tree_exporter.env_loader import expand_env_json  # noqa: E402
from code_tree_exporter.rag_export import write_rag_database  # noqa: E402
from shared.fixtures.engine_graph import business_inputs, engine_graph  # noqa: E402


class WindowsInputTests(unittest.TestCase):
    def test_backslash_paths_survive_env_expansion(self) -> None:
        path = r"C:\src\new\テスト\table"
        with mock.patch.dict(os.environ, {"CODE_MAP_SOURCE_ROOT": path}):
            config = json.loads(expand_env_json('{"root": "${CODE_MAP_SOURCE_ROOT}", "x": "$HOME"}'))
        self.assertEqual(config["root"], path)
        self.assertEqual(config["x"], "$HOME")
        with self.assertRaisesRegex(ValueError, "MISSING_VAR is not set"):
            expand_env_json('{"root": "${MISSING_VAR}"}')

    def test_cp932_csv_from_excel_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            engine_graph(base / "graph.sqlite")
            business_inputs(base / "input")
            # Excel on Japanese Windows: CP932, with NEC extensions (①, ㈱) that strict Shift_JIS rejects.
            (base / "input" / "tables.csv").write_bytes(
                "database,table_code,table_name_ja,table_name_en\r\nORDER_DB,ORDER_HEADER,注文①㈱,Order Header\r\n".encode("cp932")
            )
            self.assertIn("注文①㈱", read_text_auto(base / "input" / "tables.csv"))
            write_rag_database(base / "graph.sqlite", base / "rag.sqlite", input_root=base / "input")
            with sqlite3.connect(base / "rag.sqlite") as rag:
                names = rag.execute("SELECT names FROM nodes WHERE name='ORDER_HEADER'").fetchone()[0]
            self.assertEqual(names, "ja:注文①㈱; en:Order Header")

    def test_output_replacement_retries_while_a_file_is_locked(self) -> None:
        calls = []

        def locked_twice() -> None:
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")

        with mock.patch("time.sleep"):
            graph_package._retry_locked(locked_twice)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
