"""Regressions for defects found in the extractor audit (ids that must stay stable across monthly re-extracts,
references that must keep resolving, inputs that must not abort an extract)."""
import json
import unittest

from code_tree_exporter.comments import extract_comments
from code_tree_exporter.decoding import _detect_encoding
from code_tree_exporter.linker import _api_match_key
from code_tree_exporter.serving_tables import _restore_references


class AuditRegressionTests(unittest.TestCase):
    def test_comment_markers_inside_string_literals_are_not_comments(self) -> None:
        sql = "SELECT 'https://x' , '--none--' FROM dual; -- real\nv := 'it''s -- no'; /* b */"
        self.assertEqual([c.raw_text for c in extract_comments(sql, "a.sql", "sql")], ["-- real", "/* b */"])
        ts = 'const u = "http://x"; // real\nlet t = `a//b`; /* block */'
        self.assertEqual([c.raw_text for c in extract_comments(ts, "a.ts", "angular")], ["// real", "/* block */"])

    def test_cp932_half_width_katakana_is_not_read_as_euc_jp(self) -> None:
        for text in ("-- ｺｰﾄﾞ ﾏｽ\nSELECT 1 FROM DUAL;\n", "CODE_M,ｺｰﾄﾞ\n"):
            self.assertEqual(_detect_encoding(text.encode("cp932"), "x.sql"), "cp932")
        for text in ("-- 日本語のコメントです\n", "あいうえお", "受注テーブル", "-- 注文\n"):
            self.assertEqual(_detect_encoding(text.encode("euc_jp"), "x.sql"), "euc_jp")

    def test_api_routes_match_case_insensitively(self) -> None:
        self.assertEqual(_api_match_key(("GET", "/api/Orders/{id}")), _api_match_key(("GET", "/api/orders/{id}")))

    def test_compact_ids_in_properties_are_restored_to_stable_ids(self) -> None:
        compact = 6740128207958424313
        raw = json.dumps({"semantic_tree": {"steps": [{"ref_node_id": str(compact), "line": 42}]}, "count": 3})
        restored = json.loads(_restore_references(raw, {compact: "procedure:DB1:PKG:VALIDATE"}))
        self.assertEqual(restored["semantic_tree"]["steps"][0], {"ref_node_id": "procedure:DB1:PKG:VALIDATE", "line": 42})
        self.assertEqual(restored["count"], 3)


if __name__ == "__main__":
    unittest.main()
