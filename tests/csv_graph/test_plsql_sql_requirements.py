from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from application.backend.importer.package_validator import validate_package
from contract.graph_contract import column_id, table_id
from extractors.package_support.package_writer import (
    database_link_id,
    external_db_object_id,
    load_authoritative_ids,
    procedure_id,
    synonym_id,
)
from extractors.package_support.sql_analyzer import analyze_sql

ROOT = Path(__file__).resolve().parents[2]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_catalog(input_root: Path, databases: dict[str, list[str]]) -> None:
    input_root.mkdir(parents=True, exist_ok=True)
    (input_root / "tables").mkdir(exist_ok=True)
    with (input_root / "tables.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["database", "table_code", "table_name_ja", "table_name_en"], lineterminator="\n")
        writer.writeheader()
        for database, tables in databases.items():
            for table in tables:
                writer.writerow({"database": database, "table_code": table, "table_name_ja": "", "table_name_en": table})
                with (input_root / "tables" / f"{table}.csv").open("w", encoding="utf-8", newline="") as columns:
                    column_writer = csv.DictWriter(columns, fieldnames=["column_code", "column_name_ja", "column_name_en", "ordinal_position", "data_type", "nullable", "note"], lineterminator="\n")
                    column_writer.writeheader()
                    column_writer.writerow({"column_code": "ID", "ordinal_position": 1, "data_type": "NUMBER", "nullable": "N"})


def _run_extractor(script: Path, config: dict, work: Path) -> None:
    config_path = work / f"{script.parent.name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), "--config", str(config_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"{script} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


class PlsqlSqlRequirementsTest(unittest.TestCase):
    def test_sql_file_classification_values_are_exact_and_database_context_is_per_folder(self):
        with tempfile.TemporaryDirectory(prefix=".tmp-plsql-sql-", dir=ROOT) as temp_name:
            work = Path(temp_name)
            source = work / "sql-src"
            (source / "db_a").mkdir(parents=True)
            (source / "db_b").mkdir(parents=True)
            (source / "db_a" / "dml.sql").write_text("SELECT * FROM A_TABLE;\n", encoding="utf-8")
            (source / "db_a" / "mixed.sql").write_text(
                "CREATE OR REPLACE PROCEDURE p IS BEGIN INSERT INTO A_TABLE (ID) VALUES (1); END;\n/\n",
                encoding="utf-8",
            )
            (source / "db_b" / "plsql.sql").write_text(
                "CREATE OR REPLACE PROCEDURE p IS BEGIN NULL; END;\n/\n",
                encoding="utf-8",
            )
            (source / "db_b" / "unknown.sql").write_text("PROMPT deployment marker only;\n", encoding="utf-8")
            input_root = work / "input-data"
            _write_catalog(input_root, {"DB_A": ["A_TABLE"], "DB_B": ["B_TABLE"]})
            output = work / "out"

            _run_extractor(
                ROOT / "extractors/sql-file-extractor/main.py",
                {
                    "type": "sql-files",
                    "source": "req-sql",
                    "repository": "req-repo",
                    "root": str(source),
                    "folders": [
                        {"path": "db_a", "database": "DB_A"},
                        {"path": "db_b", "database": "DB_B"},
                    ],
                    "inputData": str(input_root),
                    "output": str(output),
                },
                work,
            )

            nodes = _read_csv(output / "nodes.csv")
            by_name = {row["technical_name"]: row for row in nodes if row["node_type"] == "SQL_FILE"}
            self.assertEqual("DML_SCRIPT", json.loads(by_name["dml.sql"]["properties_json"])["classification"])
            self.assertEqual("MIXED_SCRIPT", json.loads(by_name["mixed.sql"]["properties_json"])["classification"])
            self.assertEqual("PLSQL_DEFINITION", json.loads(by_name["plsql.sql"]["properties_json"])["classification"])
            self.assertEqual("UNKNOWN_SQL", json.loads(by_name["unknown.sql"]["properties_json"])["classification"])
            self.assertEqual("DB_A", by_name["dml.sql"]["database_key"])
            self.assertEqual("DB_A", by_name["mixed.sql"]["database_key"])
            self.assertEqual("DB_B", by_name["plsql.sql"]["database_key"])
            self.assertEqual("DB_B", by_name["unknown.sql"]["database_key"])
            self.assertIn(table_id("DB_A", "A_TABLE"), {row["target_node_id"] for row in _read_csv(output / "edges.csv")})

    def test_sql_loader_control_file_emits_table_column_lineage_and_semantic_tree(self):
        with tempfile.TemporaryDirectory(prefix=".tmp-sql-loader-", dir=ROOT) as temp_name:
            work = Path(temp_name)
            source = work / "sql-src"
            source.mkdir()
            (source / "load_orders.ctl").write_text(
                """LOAD DATA
INFILE 'orders.csv'
BADFILE 'orders.bad'
DISCARDFILE 'orders.discard'
APPEND
INTO TABLE TPR001
FIELDS TERMINATED BY ','
(
  ID POSITION(1:10) INTEGER EXTERNAL,
  STATUS CHAR "TRIM(:STATUS)",
  CREATED_AT CONSTANT 'NOW',
  UNUSED FILLER CHAR
)
""",
                encoding="utf-8",
            )
            input_root = work / "input-data"
            _write_catalog(input_root, {"ORDER_DB": ["TPR001"]})
            with (input_root / "tables" / "TPR001.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["column_code", "column_name_ja", "column_name_en", "ordinal_position", "data_type", "nullable", "note"], lineterminator="\n")
                writer.writeheader()
                for index, column in enumerate(("ID", "STATUS", "CREATED_AT"), 1):
                    writer.writerow({"column_code": column, "ordinal_position": index, "data_type": "VARCHAR2", "nullable": "Y"})
            output = work / "out"

            _run_extractor(
                ROOT / "extractors/sql-file-extractor/main.py",
                {"type": "sql-files", "source": "loader", "repository": "repo", "database": "ORDER_DB", "root": str(source), "folders": ["."], "inputData": str(input_root), "output": str(output)},
                work,
            )

            package = validate_package(output, load_authoritative_ids(input_root), workspace_root=ROOT)
            node = next(row for row in package["nodes"] if row["node_type"] == "SQL_FILE")
            properties = json.loads(node["properties_json"])
            self.assertEqual("SQL_LOADER_CONTROL_FILE", properties["classification"])
            self.assertEqual("orders.csv", properties["infile"])
            self.assertEqual(2, properties["semantic_tree"]["version"])
            edges = package["edges"]
            self.assertIn(("INSERTS", "APPEND", table_id("ORDER_DB", "TPR001")), {(row["edge_type"], row["raw_operation"], row["target_node_id"]) for row in edges})
            mappings = [row for row in edges if row["edge_type"] == "WRITES_COLUMN"]
            self.assertEqual({column_id("ORDER_DB", "TPR001", name) for name in ("ID", "STATUS", "CREATED_AT")}, {row["target_node_id"] for row in mappings})
            by_target = {row["target_node_id"]: json.loads(row["properties_json"]) for row in mappings}
            self.assertEqual("1:10", by_target[column_id("ORDER_DB", "TPR001", "ID")]["position"])
            self.assertEqual("TRIM(:STATUS)", by_target[column_id("ORDER_DB", "TPR001", "STATUS")]["transform"])
            self.assertEqual("NOW", by_target[column_id("ORDER_DB", "TPR001", "CREATED_AT")]["constant"])
            mapping_evidence = [row for row in package["evidence"] if row["target_id"] in {item["edge_id"] for item in mappings}]
            self.assertEqual({"ID POSITION(1:10) INTEGER EXTERNAL,", "STATUS CHAR \"TRIM(:STATUS)\",", "CREATED_AT CONSTANT 'NOW',"}, {row["snippet"] for row in mapping_evidence})
            self.assertFalse(package["issues"])

    def test_dynamic_sql_literals_concatenations_and_assigned_literals_are_resolved_best_effort(self):
        text = """
DECLARE
  v_sql VARCHAR2(4000);
  v_table VARCHAR2(30) := 'ORDER_HEADER';
BEGIN
  EXECUTE IMMEDIATE 'UPDATE ORDER_HEADER SET STATUS = ''X'' WHERE ORDER_ID = :id';
  EXECUTE IMMEDIATE 'INSERT INTO ' || 'ORDER_LINE' || ' (ORDER_ID) VALUES (:id)';
  v_sql := 'DELETE FROM INVENTORY_RESERVATION WHERE ORDER_ID = :id';
  EXECUTE IMMEDIATE v_sql;
  EXECUTE IMMEDIATE 'SELECT * FROM ' || v_table || ' WHERE ORDER_ID = :id';
  EXECUTE IMMEDIATE p_runtime_sql;
END;
"""
        analysis = analyze_sql(text)
        resolved = {(ref.object_name.upper(), ref.operation, ref.edge_type) for ref in analysis.tables}
        self.assertTrue(
            {
                ("ORDER_HEADER", "UPDATE", "UPDATES"),
                ("ORDER_LINE", "INSERT", "INSERTS"),
                ("INVENTORY_RESERVATION", "DELETE", "DELETES"),
                ("ORDER_HEADER", "SELECT", "READS"),
            }
            <= resolved
        )
        self.assertEqual(1, len(analysis.dynamic_offsets))

    def test_oracle_system_objects_and_table_functions_are_not_authoritative_table_refs(self):
        analysis = analyze_sql(
            "SELECT * FROM DUAL;\n"
            "SELECT * FROM SYS.ALL_TABLES;\n"
            "SELECT * FROM USER_TABLES;\n"
            "SELECT * FROM TABLE(my_package.rows_for_user());\n"
            "SELECT * FROM APP_TABLE;\n"
        )
        self.assertEqual(["APP_TABLE"], [ref.object_name.upper() for ref in analysis.tables])

    def test_trigger_update_of_column_clause_does_not_emit_of_as_table_reference(self):
        analysis = analyze_sql(
            "CREATE OR REPLACE TRIGGER TRG_SHIPMENT_AUDIT\n"
            "AFTER UPDATE OF STATUS ON SHIPMENT\n"
            "FOR EACH ROW\n"
            "BEGIN\n"
            "  INSERT INTO SHIPMENT_AUDIT (AUDIT_ID) VALUES (SHIPMENT_AUDIT_SEQ.NEXTVAL);\n"
            "END;\n"
        )
        names = {ref.object_name.upper() for ref in analysis.tables}
        self.assertNotIn("OF", names)
        self.assertIn("SHIPMENT_AUDIT", names)

    def test_plsql_synonyms_resolve_to_catalog_or_external_objects_and_overloads_are_ambiguous(self):
        with tempfile.TemporaryDirectory(prefix=".tmp-plsql-req-", dir=ROOT) as temp_name:
            work = Path(temp_name)
            source = work / "plsql-src"
            source.mkdir(parents=True)
            (source / "pkg_req.sql").write_text(
                """
CREATE OR REPLACE SYNONYM ORDER_HDR_SYN FOR ORDER_HEADER;
CREATE OR REPLACE SYNONYM TAX_RATE_SYN FOR TAX_SCHEMA.TAX_RATE@TAX_DB_LINK;

CREATE OR REPLACE PACKAGE BODY PKG_REQ AS
  PROCEDURE DO_WORK(p_id IN NUMBER) IS
  BEGIN
    NULL;
  END DO_WORK;

  PROCEDURE DO_WORK(p_code IN VARCHAR2) IS
  BEGIN
    NULL;
  END DO_WORK;

  PROCEDURE RUN IS
    v_count NUMBER;
    v_rate NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_count FROM ORDER_HDR_SYN;
    SELECT RATE INTO v_rate FROM TAX_RATE_SYN;
    DO_WORK(1);
  END RUN;
END PKG_REQ;
/
""".lstrip(),
                encoding="utf-8",
            )
            input_root = work / "input-data"
            _write_catalog(input_root, {"ORDER_DB": ["ORDER_HEADER"]})
            output = work / "out"

            _run_extractor(
                ROOT / "extractors/plsql-extractor/main.py",
                {
                    "type": "oracle-plsql",
                    "source": "req-plsql",
                    "repository": "req-repo",
                    "database": "ORDER_DB",
                    "schema": "ORDER_APP",
                    "root": str(source),
                    "folders": ["."],
                    "inputData": str(input_root),
                    "output": str(output),
                },
                work,
            )

            nodes = _read_csv(output / "nodes.csv")
            edges = _read_csv(output / "edges.csv")
            issues = _read_csv(output / "issues.csv")
            node_types_by_name = {(row["node_type"], row["technical_name"]): row["node_id"] for row in nodes}
            order_synonym = synonym_id("ORDER_DB", "ORDER_HDR_SYN")
            tax_synonym = synonym_id("ORDER_DB", "TAX_RATE_SYN")
            order_table = table_id("ORDER_DB", "ORDER_HEADER")
            tax_external = external_db_object_id("ORDER_DB", "TAX_SCHEMA.TAX_RATE@TAX_DB_LINK")
            run_proc = procedure_id("ORDER_DB", "PKG_REQ", "RUN", "void")
            number_overload = procedure_id("ORDER_DB", "PKG_REQ", "DO_WORK", "NUMBER")
            varchar_overload = procedure_id("ORDER_DB", "PKG_REQ", "DO_WORK", "VARCHAR2")

            self.assertEqual(order_synonym, node_types_by_name[("SYNONYM", "ORDER_HDR_SYN")])
            self.assertEqual(tax_synonym, node_types_by_name[("SYNONYM", "TAX_RATE_SYN")])
            self.assertEqual(database_link_id("ORDER_DB", "TAX_DB_LINK"), node_types_by_name[("DATABASE_LINK", "TAX_DB_LINK")])
            self.assertEqual(tax_external, node_types_by_name[("EXTERNAL_DATABASE_OBJECT", "TAX_SCHEMA.TAX_RATE@TAX_DB_LINK")])

            edge_triples = {(row["source_node_id"], row["edge_type"], row["target_node_id"]) for row in edges}
            self.assertIn((order_synonym, "RESOLVES_TO", order_table), edge_triples)
            self.assertIn((tax_synonym, "RESOLVES_TO", tax_external), edge_triples)
            self.assertIn((run_proc, "USES", order_synonym), edge_triples)
            self.assertIn((run_proc, "USES", tax_synonym), edge_triples)
            self.assertIn((run_proc, "REMOTE_READS", tax_external), edge_triples)

            synonym_reads = [
                row
                for row in edges
                if row["source_node_id"] == run_proc and row["edge_type"] == "READS" and row["target_node_id"] == order_table
            ]
            self.assertEqual(1, len(synonym_reads))
            self.assertEqual("ORDER_HDR_SYN", json.loads(synonym_reads[0]["properties_json"])["resolvedViaSynonym"])

            ambiguous = [row for row in issues if row["issue_type"] == "AMBIGUOUS_SYMBOL" and row["source_node_id"] == run_proc]
            self.assertEqual(1, len(ambiguous))
            self.assertEqual({number_overload, varchar_overload}, set(json.loads(ambiguous[0]["properties_json"])["candidates"]))
            self.assertIn("EXTERNAL_OBJECT", {row["issue_type"] for row in issues})
            self.assertNotIn("TABLE_NOT_IMPORTED", {row["issue_type"] for row in issues})


if __name__ == "__main__":
    unittest.main()
