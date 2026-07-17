from pathlib import Path
import unittest

from extractors.oracle_plsql import OraclePlSqlExtractor
from extractors.oracle_plsql_lineage import OraclePlSqlLineageExtractor
from db import schema as S
from db.entities import ExtractionContext, ExtractionResult

ROOT = Path(__file__).resolve().parents[1]
CTX = ExtractionContext(
    repository="code-map", db_name="OracleDB", extra_tags={"schema": "HR"}
)


def extract_sample(name: str) -> ExtractionResult:
    path = ROOT / "samples" / name
    text = path.read_text()
    out = ExtractionResult()
    for extractor in (OraclePlSqlExtractor(), OraclePlSqlLineageExtractor()):
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
            "PLSQLPackage:code-map:PKG_PAYROLL", qnames(result, S.LABEL_PLSQL_PACKAGE)
        )
        self.assertIn(
            "Procedure:code-map:PKG_PAYROLL.LOAD_MONTH",
            qnames(result, S.LABEL_PROCEDURE),
        )
        self.assertIn(
            "Procedure:code-map:PKG_PAYROLL.CLOSE_MONTH",
            qnames(result, S.LABEL_PROCEDURE),
        )

        edges = rels(result, S.REL_INSERTS_INTO)
        self.assertIn(
            (
                "Procedure:code-map:PKG_PAYROLL.LOAD_MONTH",
                "Table:OracleDB:HR.PAYROLL_BASE",
            ),
            edges,
        )
        self.assertIn(
            (
                "Procedure:code-map:PKG_PAYROLL.LOAD_MONTH",
                "Table:OracleDB:HR.JOB_AUDIT",
            ),
            edges,
        )
        self.assertIn(
            (
                "Procedure:code-map:PKG_PAYROLL.LOAD_MONTH",
                "Sequence:OracleDB:HR.PAYROLL_SEQ",
            ),
            rels(result, S.REL_USES_SEQUENCE),
        )
        self.assertIn(
            (
                "Procedure:code-map:PKG_PAYROLL.CLOSE_MONTH",
                "Procedure:code-map:PKG_BONUS.APPLY_BONUS",
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
        self.assertIn("PLSQLConstant:code-map:PKG_DEMO.C_RATE", constants)
        self.assertIn("PLSQLConstant:code-map:PKG_DEMO.RUN.C_STATUS", constants)


if __name__ == "__main__":
    unittest.main()
