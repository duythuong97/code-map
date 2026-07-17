import unittest

from extractors.oracle_plsql import OraclePlSqlExtractor
from extractors.oracle_plsql_lineage import OraclePlSqlLineageExtractor
from db import schema as S
from db.entities import ExtractionContext, ExtractionResult

CTX = ExtractionContext(repository="repo", db_name="DB", extra_tags={"schema": "HR"})


def extract(text: str, file_name: str = "case.pkb") -> ExtractionResult:
    out = ExtractionResult()
    for extractor in (OraclePlSqlExtractor(), OraclePlSqlLineageExtractor()):
        if extractor.can_handle(file_name, text):
            out = out.merge(extractor.extract(file_name, text, CTX))
    return out


def has_node(result: ExtractionResult, label: str, suffix: str | None = None) -> bool:
    return any(n.label == label and (suffix is None or n.key_value.endswith(suffix)) for n in result.nodes)


def has_edge(result: ExtractionResult, rel: str, to_suffix: str | None = None, from_suffix: str | None = None) -> bool:
    return any(
        e.rel_type == rel
        and (to_suffix is None or e.to_key_value.endswith(to_suffix))
        and (from_suffix is None or e.from_key_value.endswith(from_suffix))
        for e in result.edges
    )


def has_property(result: ExtractionResult, label: str, prop: str) -> bool:
    return any(n.label == label and n.properties.get(prop) for n in result.nodes)


CASES = [
    (
        "package body",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN NULL; END; END; /",
        lambda r: has_node(r, S.LABEL_PLSQL_PACKAGE, ":PKG"),
        True,
    ),
    (
        "package spec procedure",
        "CREATE OR REPLACE PACKAGE hr.pkg AS PROCEDURE p(x NUMBER); END; /",
        lambda r: has_node(r, S.LABEL_PROCEDURE, ":PKG.P"),
        True,
    ),
    (
        "function return",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; END; /",
        lambda r: has_node(r, S.LABEL_SQL_FUNCTION, ":PKG.F"),
        True,
    ),
    (
        "trigger fired table",
        "CREATE OR REPLACE TRIGGER trg BEFORE INSERT ON hr.emp BEGIN INSERT INTO hr.audit(id) VALUES(1); END; /",
        lambda r: has_node(r, S.LABEL_TRIGGER, ":TRG") and has_edge(r, S.REL_TRIGGERS, ":HR.EMP"),
        True,
    ),
    (
        "procedure params modeled",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p(x NUMBER) IS BEGIN NULL; END; END; /",
        lambda r: has_property(r, S.LABEL_PROCEDURE, "parameters"),
        False,
    ),
    (
        "package constant",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS\n c_rate CONSTANT NUMBER := 0.1;\n PROCEDURE p IS BEGIN NULL; END; END; /",
        lambda r: has_node(r, S.LABEL_PLSQL_CONSTANT, ":PKG.C_RATE"),
        True,
    ),
    (
        "local constant",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS\n c_status CONSTANT VARCHAR2(1) := 'Y';\n BEGIN NULL; END; END; /",
        lambda r: has_node(r, S.LABEL_PLSQL_CONSTANT, ":PKG.P.C_STATUS"),
        True,
    ),
    (
        "variable declaration modeled",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS v_id NUMBER; BEGIN NULL; END; END; /",
        lambda r: has_node(r, "PLSQLVariable"),
        True,
    ),
    (
        "record type modeled",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS\n TYPE t_rec IS RECORD(id NUMBER);\n PROCEDURE p IS BEGIN NULL; END; END; /",
        lambda r: has_node(r, "PLSQLType"),
        True,
    ),
    (
        "insert values table",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN INSERT INTO hr.t(id, name) VALUES(1, 'A'); END; END; /",
        lambda r: has_edge(r, S.REL_INSERTS_INTO, ":HR.T"),
        True,
    ),
    (
        "insert select table read write",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN INSERT INTO hr.t(id) SELECT s.id FROM hr.src s; END; END; /",
        lambda r: has_edge(r, S.REL_INSERTS_INTO, ":HR.T") and has_edge(r, S.REL_READS_FROM, ":HR.SRC"),
        True,
    ),
    (
        "update table",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN UPDATE hr.t SET name = 'A' WHERE id = 1; END; END; /",
        lambda r: has_edge(r, S.REL_UPDATES, ":HR.T"),
        True,
    ),
    (
        "delete table",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN DELETE FROM hr.t WHERE id = 1; END; END; /",
        lambda r: has_edge(r, S.REL_DELETES_FROM, ":HR.T"),
        True,
    ),
    (
        "merge table",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN MERGE INTO hr.t x USING hr.src s ON (x.id=s.id) WHEN MATCHED THEN UPDATE SET x.name=s.name; END; END; /",
        lambda r: has_edge(r, S.REL_MERGES_INTO, ":HR.T"),
        True,
    ),
    (
        "join read table",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN INSERT INTO hr.t(id) SELECT e.id FROM hr.emp e JOIN hr.dept d ON d.id=e.dept_id; END; END; /",
        lambda r: has_edge(r, S.REL_READS_FROM, ":HR.EMP") and has_edge(r, S.REL_READS_FROM, ":HR.DEPT"),
        True,
    ),
    (
        "cte read table",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN INSERT INTO hr.t(id) WITH x AS (SELECT id FROM hr.src) SELECT id FROM x; END; END; /",
        lambda r: has_edge(r, S.REL_READS_FROM, ":HR.SRC"),
        True,
    ),
    (
        "sequence usage",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN SELECT hr.seq.NEXTVAL INTO v FROM dual; END; END; /",
        lambda r: has_edge(r, S.REL_USES_SEQUENCE, ":HR.SEQ"),
        True,
    ),
    (
        "literal dynamic sql",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN EXECUTE IMMEDIATE 'delete from hr.tab1 where id=1'; END; END; /",
        lambda r: has_edge(r, S.REL_DELETES_FROM, ":HR.TAB1"),
        True,
    ),
    (
        "variable dynamic sql",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS v_sql VARCHAR2(1000); BEGIN v_sql := 'delete from hr.tab1'; EXECUTE IMMEDIATE v_sql; END; END; /",
        lambda r: has_edge(r, S.REL_DELETES_FROM, ":HR.TAB1"),
        True,
    ),
    (
        "package call",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN pkg2.run(1); END; END; /",
        lambda r: has_edge(r, S.REL_CALLS, ":PKG2.RUN"),
        True,
    ),
    (
        "schema package call",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN app.pkg2.run(1); END; END; /",
        lambda r: has_edge(r, S.REL_CALLS, ":PKG2.RUN"),
        True,
    ),
    (
        "builtin package ignored",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN dbms_output.put_line('x'); END; END; /",
        lambda r: not has_edge(r, S.REL_CALLS, ":DBMS_OUTPUT.PUT_LINE"),
        True,
    ),
    (
        "cursor declaration",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS CURSOR c IS SELECT e.id FROM hr.emp e; BEGIN NULL; END; END; /",
        lambda r: has_node(r, "Cursor") and has_edge(r, "READS_COLUMN", ":HR.EMP:ID"),
        True,
    ),
    (
        "open for cursor",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS rc SYS_REFCURSOR; BEGIN OPEN rc FOR SELECT e.id FROM hr.emp e; END; END; /",
        lambda r: has_node(r, "Cursor") and has_edge(r, "READS_COLUMN", ":HR.EMP:ID"),
        True,
    ),
    (
        "for select loop variable lineage",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN FOR r IN (SELECT e.id FROM hr.emp e) LOOP INSERT INTO hr.t(id) VALUES(r.id); END LOOP; END; END; /",
        lambda r: has_edge(r, "DERIVES_FROM", ":HR.T:ID", ":HR.EMP:ID"),
        True,
    ),
    (
        "select into variable lineage",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS v_name VARCHAR2(10); BEGIN SELECT e.name INTO v_name FROM hr.emp e; INSERT INTO hr.t(name) VALUES(v_name); END; END; /",
        lambda r: has_edge(r, "DERIVES_FROM", ":HR.T:NAME", ":HR.EMP:NAME"),
        True,
    ),
    (
        "quoted identifiers",
        'CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN INSERT INTO "Emp Log"("Id") VALUES(1); END; END; /',
        lambda r: has_edge(r, S.REL_INSERTS_INTO, ":HR.Emp Log"),
        False,
    ),
    (
        "local nested procedure ownership",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE outer IS PROCEDURE inner IS BEGIN INSERT INTO hr.inner_t(id) VALUES(1); END; BEGIN INSERT INTO hr.outer_t(id) VALUES(1); inner; END; END; /",
        lambda r: has_edge(r, S.REL_INSERTS_INTO, ":HR.OUTER_T", ":PKG.OUTER") and has_edge(r, S.REL_INSERTS_INTO, ":HR.INNER_T", ":PKG.OUTER.INNER"),
        False,
    ),
    (
        "exception flow modeled",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN INSERT INTO hr.t(id) VALUES(1); EXCEPTION WHEN OTHERS THEN INSERT INTO hr.err_log(msg) VALUES(SQLERRM); END; END; /",
        lambda r: has_edge(r, "HANDLES_EXCEPTION"),
        False,
    ),
    (
        "synonym resolved",
        "CREATE OR REPLACE PACKAGE BODY hr.pkg AS PROCEDURE p IS BEGIN SELECT id INTO v FROM emp_syn; END; END; /",
        lambda r: has_edge(r, S.REL_READS_FROM, ":HR.EMP"),
        False,
    ),
]


class OraclePlSqlCoverageMatrixTest(unittest.TestCase):
    def test_current_plsql_feature_coverage_matrix(self):
        covered = 0
        unexpected = []
        for name, text, check, expected in CASES:
            with self.subTest(name=name):
                actual = bool(check(extract(text)))
                covered += int(actual)
                if actual != expected:
                    unexpected.append((name, expected, actual))
        total = len(CASES)
        pct = round(covered * 100 / total, 1)
        print(f"PLSQL feature coverage matrix: {covered}/{total} = {pct}%")
        self.assertEqual([], unexpected)


if __name__ == "__main__":
    unittest.main()
