from pathlib import Path
import unittest

from extractors.oracle_plsql.extractor import OraclePlSqlExtractor
from extractors.oracle_plsql.antlr_calls import OraclePlSqlAntlrCallExtractor
from extractors.oracle_plsql.lineage import OraclePlSqlLineageExtractor
from contract import schema as S
from contract.entities import ExtractionContext, ExtractionResult

ROOT = Path(__file__).resolve().parents[1]
CTX = ExtractionContext(
    repository="code-map",
    db_name="OracleDB",
    schema_name="HR",
    source_id="oracle-main",
    relative_source_path="samples/plsql.pkb",
)


def extract_sample(name: str, include_antlr: bool = False) -> ExtractionResult:
    path = ROOT / "samples" / name
    text = path.read_text()
    out = ExtractionResult()
    extractors = [OraclePlSqlExtractor()]
    if include_antlr:
        extractors.append(OraclePlSqlAntlrCallExtractor())
    extractors.append(OraclePlSqlLineageExtractor())
    for extractor in extractors:
        if extractor.can_handle(str(path), text):
            out = out.merge(extractor.extract(str(path), text, CTX))
    return out


def qnames(result: ExtractionResult, label: str) -> set[str]:
    return {n.key_value for n in result.nodes if n.label == label}


def rels(result: ExtractionResult, rel_type: str) -> set[tuple[str, str]]:
    return {
        (e.from_key_value, e.to_key_value)
        for e in result.edges
        if e.rel_type == rel_type
    }


class OraclePlSqlExtractorSampleTest(unittest.TestCase):
    def test_payroll_sample_graph_dependencies(self):
        result = extract_sample("pkg_payroll.pkb")

        self.assertIn(
            "PLSQLPackage:code-map:OracleDB:HR:PKG_PAYROLL",
            qnames(result, S.LABEL_PLSQL_PACKAGE),
        )
        self.assertIn(
            "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.LOAD_MONTH",
            qnames(result, S.LABEL_PROCEDURE),
        )
        self.assertIn(
            "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.CLOSE_MONTH",
            qnames(result, S.LABEL_PROCEDURE),
        )

        edges = rels(result, S.REL_INSERTS_INTO)
        self.assertIn(
            (
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.LOAD_MONTH",
                "Table:OracleDB:HR.PAYROLL_BASE",
            ),
            edges,
        )
        self.assertIn(
            (
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.LOAD_MONTH",
                "Table:OracleDB:HR.JOB_AUDIT",
            ),
            edges,
        )
        self.assertIn(
            (
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.LOAD_MONTH",
                "Sequence:OracleDB:HR.PAYROLL_SEQ",
            ),
            rels(result, S.REL_USES_SEQUENCE),
        )
        self.assertIn(
            (
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.CLOSE_MONTH",
                "Procedure:code-map:OracleDB:HR:PKG_BONUS.APPLY_BONUS",
            ),
            rels(result, S.REL_CALLS),
        )

    def test_bonus_sample_column_lineage(self):
        result = extract_sample("pkg_bonus.pkb")

        self.assertIn(
            "Column:OracleDB:HR.BONUS_PAYMENT:BONUS_AMOUNT",
            qnames(result, S.LABEL_COLUMN),
        )
        self.assertIn(
            "Column:OracleDB:HR.PAYROLL_BASE:NET_SALARY", qnames(result, S.LABEL_COLUMN)
        )
        self.assertIn(
            (
                "Column:OracleDB:HR.PAYROLL_BASE:NET_SALARY",
                "Column:OracleDB:HR.BONUS_PAYMENT:BONUS_AMOUNT",
            ),
            rels(result, "DERIVES_FROM"),
        )

    def test_antlr_call_sample_local_call_edges(self):
        result = extract_sample("pkg_antlr_calls.pkb", include_antlr=True)
        calls = rels(result, S.REL_CALLS)

        self.assertIn(
            (
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.TAX_AMOUNT",
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.BASE_AMOUNT",
            ),
            calls,
        )
        self.assertIn(
            (
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.NET_AMOUNT",
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.BASE_AMOUNT",
            ),
            calls,
        )
        self.assertIn(
            (
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.NET_AMOUNT",
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.TAX_AMOUNT",
            ),
            calls,
        )
        self.assertIn(
            (
                "Procedure:code-map:OracleDB:HR:PKG_ANTLR_CALLS.PROCESS_EMPLOYEE",
                "SQLFunction:code-map:OracleDB:HR:PKG_ANTLR_CALLS.NET_AMOUNT",
            ),
            calls,
        )
        self.assertIn(
            (
                "Procedure:code-map:OracleDB:HR:PKG_ANTLR_CALLS.PROCESS_EMPLOYEE",
                "Procedure:code-map:OracleDB:HR:PKG_ANTLR_CALLS.WRITE_AUDIT",
            ),
            calls,
        )

    def test_constant_declaration_is_extracted(self):
        text = """
CREATE OR REPLACE PACKAGE BODY hr.pkg_demo AS
  c_rate CONSTANT NUMBER := 0.1;
  PROCEDURE run IS
    c_status CONSTANT VARCHAR2(10) := 'DONE';
  BEGIN NULL; END;
END;
/"""
        result = OraclePlSqlExtractor().extract("pkg_demo.pkb", text, CTX)

        constants = qnames(result, S.LABEL_PLSQL_CONSTANT)
        self.assertIn(
            "PLSQLConstant:code-map:OracleDB:HR:PKG_DEMO.C_RATE", constants
        )
        self.assertIn(
            "PLSQLConstant:code-map:OracleDB:HR:PKG_DEMO.RUN.C_STATUS", constants
        )


    def test_nested_routine_identity_matches_all_extractors(self):
        text = """
CREATE OR REPLACE PACKAGE BODY hr.pkg_nested AS
  PROCEDURE outer_run IS
    PROCEDURE inner_run IS
    BEGIN
      INSERT INTO hr.inner_log(id) VALUES (1);
    END;
  BEGIN
    IF 1 = 1 THEN
      INSERT INTO hr.outer_log(id) VALUES (1);
    END IF;
    inner_run();
  END;
END;
/"""
        result = ExtractionResult()
        for extractor in (
            OraclePlSqlExtractor(),
            OraclePlSqlAntlrCallExtractor(),
            OraclePlSqlLineageExtractor(),
        ):
            result = result.merge(extractor.extract("nested.pkb", text, CTX))

        outer = "Procedure:code-map:OracleDB:HR:PKG_NESTED.OUTER_RUN"
        inner = "Procedure:code-map:OracleDB:HR:PKG_NESTED.OUTER_RUN.INNER_RUN"
        self.assertIn(inner, qnames(result, S.LABEL_PROCEDURE))
        self.assertIn(
            (inner, "Table:OracleDB:HR.INNER_LOG"),
            rels(result, S.REL_INSERTS_INTO),
        )
        self.assertIn(
            (outer, "Table:OracleDB:HR.OUTER_LOG"),
            rels(result, S.REL_INSERTS_INTO),
        )
        self.assertIn((outer, inner), rels(result, S.REL_CALLS))

    def test_scanner_attaches_semantic_to_supported_edges(self):
        text = """
CREATE OR REPLACE PACKAGE BODY hr.pkg_semantic AS
  PROCEDURE run IS
  BEGIN
    INSERT INTO hr.audit_log(id, amount)
    SELECT p.id, p.net_salary FROM hr.payroll_base p;
    pkg_bonus.apply_bonus();
  EXCEPTION WHEN NO_DATA_FOUND THEN NULL;
  END;
END;
/"""
        result = extract_file(
            Path("semantic.pkb"),
            "samples/semantic.pkb",
            text,
            CTX,
            [OraclePlSqlExtractor(), OraclePlSqlLineageExtractor()],
        )

        write = next(edge for edge in result.edges if edge.rel_type == S.REL_INSERTS_INTO)
        read = next(edge for edge in result.edges if edge.rel_type == S.REL_READS_FROM)
        call = next(edge for edge in result.edges if edge.rel_type == S.REL_CALLS)
        handler = next(edge for edge in result.edges if edge.rel_type == "HANDLES_EXCEPTION")
        derive = next(edge for edge in result.edges if edge.rel_type == "DERIVES_FROM")

        self.assertEqual(write.properties["semantic"]["action"], "WRITE")
        self.assertEqual(
            {field["name"] for field in write.properties["semantic"]["fields"]},
            {"ID", "AMOUNT"},
        )
        self.assertEqual(read.properties["semantic"]["action"], "READ")
        self.assertEqual(call.properties["semantic"]["action"], "CALL")
        self.assertEqual(handler.properties["semantic"]["handler"], "NO_DATA_FOUND")
        self.assertEqual(derive.properties["semantic"]["action"], "DERIVE_FIELD")

    def test_trigger_body_owns_every_statement(self):
        text = """
CREATE OR REPLACE TRIGGER hr.trg_audit
AFTER INSERT ON hr.employee
BEGIN
  INSERT INTO hr.audit_a(id) VALUES (1);
  INSERT INTO hr.audit_b(id) VALUES (1);
END;
/"""
        result = OraclePlSqlExtractor().extract("trg_audit.sql", text, CTX)
        owner = "Trigger:code-map:OracleDB:HR:TRG_AUDIT"
        writes = rels(result, S.REL_INSERTS_INTO)
        self.assertIn((owner, "Table:OracleDB:HR.AUDIT_A"), writes)
        self.assertIn((owner, "Table:OracleDB:HR.AUDIT_B"), writes)

if __name__ == "__main__":
    unittest.main()
