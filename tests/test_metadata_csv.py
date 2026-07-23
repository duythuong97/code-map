from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from application.backend.importer.import_csv import parse_metadata_facts


class MetadataCsvParserTest(unittest.TestCase):
    def write(self, directory: str, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = Path(directory, name)
        path.write_bytes(text.encode(encoding))
        return path

    def parse(self, path: Path, defaults=None, *, encoding="utf-8"):
        return parse_metadata_facts(
            path.read_bytes(), path, defaults, encoding=encoding
        )

    def test_table_file_uses_defaults_and_stable_ids(self):
        with TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "tables.csv",
                "schema_name,table_code,table_name_en\nHR,EMPLOYEE,Employee\n",
            )
            facts = self.parse(path, {"db_name": "OracleDB", "schema": "HR"})

        self.assertEqual(1, len(facts))
        self.assertEqual("table", facts[0].entity_kind)
        self.assertEqual("OracleDB:HR.EMPLOYEE", facts[0].entity_id)
        self.assertEqual("Employee", facts[0].payload["name_en"])

    def test_per_table_file_emits_folded_table_and_columns(self):
        with TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "employee.csv",
                "column_code,column_name_en,table_description\n"
                "EMP_ID,Old employee ID,Employee master\n"
                "EMP_ID,Employee ID,\n"
                "DEPT_ID,Department ID,\n",
            )
            facts = self.parse(
                path, {"db_name": "OracleDB", "schema": "HR"}
            )

        by_id = {fact.entity_id: fact for fact in facts}
        self.assertEqual(3, len(facts))
        self.assertEqual(
            "Employee master", by_id["OracleDB:HR.EMPLOYEE"].payload["description"]
        )
        employee_id = by_id["Column:OracleDB:HR.EMPLOYEE:EMP_ID"]
        self.assertEqual("Employee ID", employee_id.payload["name_en"])
        self.assertEqual(2, employee_id.row_order)

    def test_cp932_and_multiline_fields_are_preserved(self):
        with TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "employee.csv",
                'column_code,column_name_ja,column_description\nEMP_ID,従業員ID,"line 1\nline 2"\n',
                "cp932",
            )
            facts = self.parse(
                path, {"db_name": "OracleDB", "schema": "HR"}, encoding="cp932"
            )

        column = next(fact for fact in facts if fact.entity_kind == "column")
        self.assertEqual("従業員ID", column.payload["name_ja"])
        self.assertEqual("line 1\nline 2", column.payload["description"])

    def test_missing_required_identity_fails(self):
        with TemporaryDirectory() as directory:
            path = self.write(directory, "tables.csv", "table_code\nEMPLOYEE\n")
            with self.assertRaisesRegex(ValueError, "requires db_name, schema, and table"):
                self.parse(path)

    def test_extra_columns_fail_instead_of_silent_truncation(self):
        with TemporaryDirectory() as directory:
            path = self.write(directory, "tables.csv", "table_code\nEMPLOYEE,extra\n")
            with self.assertRaisesRegex(ValueError, "extra columns"):
                self.parse(path, {"db_name": "OracleDB", "schema": "HR"})


    def test_parser_uses_supplied_bytes_after_file_mutation(self):
        with TemporaryDirectory() as directory:
            path = self.write(directory, "tables.csv", "table_code\nEMPLOYEE\n")
            stable = path.read_bytes()
            path.write_text("table_code\nDEPARTMENT\n", encoding="utf-8")
            facts = parse_metadata_facts(
                stable, path, {"db_name": "OracleDB", "schema": "HR"}
            )
        self.assertEqual("OracleDB:HR.EMPLOYEE", facts[0].entity_id)

    def test_explicit_euc_jp_preserves_unicode(self):
        with TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "employee.csv",
                "column_code,column_name_ja\nEMP_ID,あ\n",
                "euc_jp",
            )
            facts = self.parse(
                path, {"db_name": "OracleDB", "schema": "HR"}, encoding="euc_jp"
            )
        column = next(fact for fact in facts if fact.entity_kind == "column")
        self.assertEqual("あ", column.payload["name_ja"])

    def test_invalid_headers_fail_but_recognized_header_only_is_empty(self):
        with TemporaryDirectory() as directory:
            defaults = {"db_name": "OracleDB", "schema": "HR"}
            for name, text, message in (
                ("empty.csv", "", "header is required"),
                ("unknown.csv", "typo\nvalue\n", "no recognized"),
                ("duplicate.csv", "table_code,Table_Code\nA,B\n", "duplicate normalized"),
            ):
                path = self.write(directory, name, text)
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                    self.parse(path, defaults)
            path = self.write(directory, "tables.csv", "table_code\n")
            self.assertEqual([], self.parse(path, defaults))

    def test_identity_matches_source_extractor_normalization(self):
        with TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "tables.csv",
                "schema,table_code,column_code\n hr , employee , emp_id \n",
            )
            facts = self.parse(path, {"db_name": " OracleDB ", "schema": " ignored "})
        by_kind = {fact.entity_kind: fact for fact in facts}
        self.assertEqual("OracleDB:HR.EMPLOYEE", by_kind["table"].entity_id)
        self.assertEqual(
            "Column:OracleDB:HR.EMPLOYEE:EMP_ID", by_kind["column"].entity_id
        )

if __name__ == "__main__":
    unittest.main()
