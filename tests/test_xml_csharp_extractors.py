from __future__ import annotations

import unittest

from db import schema as S
from db.entities import ExtractionContext, ExtractionResult
from extractors.csharp_sql import CSharpSqlExtractor
from extractors.sql_references import extract_sql_calls, extract_sql_references, looks_like_sql
from extractors.xml_sql import XmlSqlExtractor


XML_CONTEXT = ExtractionContext(
    repository="code-map",
    db_name="OracleDB",
    schema_name="HR",
    source_id="xml-main",
    relative_source_path="Queries/Employee.xml",
    project_name="Payroll",
)
CSHARP_CONTEXT = ExtractionContext(
    repository="code-map",
    db_name="OracleDB",
    schema_name="HR",
    source_id="csharp-main",
    relative_source_path="Data/EmployeeRepository.cs",
    project_name="Payroll",
)


def targets(result: ExtractionResult, relation: str) -> set[str]:
    return {edge.to_key_value for edge in result.edges if edge.rel_type == relation}


def sources(result: ExtractionResult, relation: str) -> set[str]:
    return {edge.from_key_value for edge in result.edges if edge.rel_type == relation}


class SharedSqlReferenceTest(unittest.TestCase):
    def test_comments_and_quoted_values_do_not_emit_lineage(self):
        sql = """
SELECT note FROM HR.REAL_TABLE
-- JOIN HR.LINE_GHOST ON 1 = 1
/* DELETE FROM HR.BLOCK_GHOST */
WHERE note = 'CALL HR.STRING_GHOST() FROM HR.VALUE_GHOST'
"""
        references = extract_sql_references(sql)

        self.assertEqual(
            [(item.object_name, item.relation) for item in references],
            [("HR.REAL_TABLE", S.REL_READS_FROM)],
        )
        self.assertEqual(extract_sql_calls(sql), [])
        self.assertFalse(looks_like_sql("-- SELECT * FROM HR.GHOST"))

    def test_delete_target_is_not_also_read(self):
        references = extract_sql_references(
            "DELETE FROM HR.EMPLOYEE WHERE ID IN (SELECT ID FROM HR.PAYROLL_BASE)"
        )
        self.assertEqual(
            {(item.object_name, item.relation) for item in references},
            {
                ("HR.EMPLOYEE", S.REL_DELETES_FROM),
                ("HR.PAYROLL_BASE", S.REL_READS_FROM),
            },
        )

    def test_multiple_sources_targets_and_dynamic_execute(self):
        references = extract_sql_references(
            "SELECT * FROM HR.EMPLOYEE e, HR.DEPARTMENT d; "
            "INSERT ALL INTO HR.AUDIT_A VALUES (1) "
            "INTO HR.AUDIT_B VALUES (1) SELECT * FROM DUAL"
        )
        self.assertEqual(
            {(item.object_name, item.relation) for item in references},
            {
                ("HR.EMPLOYEE", S.REL_READS_FROM),
                ("HR.DEPARTMENT", S.REL_READS_FROM),
                ("HR.AUDIT_A", S.REL_INSERTS_INTO),
                ("HR.AUDIT_B", S.REL_INSERTS_INTO),
            },
        )
        self.assertEqual(extract_sql_calls("EXECUTE IMMEDIATE v_sql"), [])
        self.assertEqual(
            extract_sql_references("SELECT q'[FROM HR.GHOST]' FROM HR.EMPLOYEE")[0].object_name,
            "HR.EMPLOYEE",
        )

    def test_table_functions_and_nested_from_clauses(self):
        self.assertEqual(
            extract_sql_references("SELECT * FROM TABLE(get_rows())"), []
        )
        self.assertEqual(
            extract_sql_references("SELECT * FROM OPENQUERY(remote, 'query')"), []
        )
        self.assertEqual(
            extract_sql_references("SELECT * FROM HR.GET_ROWS()"), []
        )
        references = extract_sql_references(
            "SELECT * FROM (SELECT * FROM HR.EMPLOYEE WHERE ID > 0) e, HR.DEPARTMENT d"
        )
        self.assertEqual(
            {item.object_name for item in references},
            {"HR.EMPLOYEE", "HR.DEPARTMENT"},
        )
        collision = extract_sql_references(
            "WITH EMPLOYEE AS (SELECT * FROM HR.PAYROLL_BASE) "
            "SELECT * FROM HR.EMPLOYEE"
        )
        self.assertEqual(
            {item.object_name for item in collision},
            {"HR.PAYROLL_BASE", "HR.EMPLOYEE"},
        )


class XmlSqlExtractorTest(unittest.TestCase):
    def test_cdata_namespace_id_source_owner_and_line(self):
        text = """<mapper namespace="Payroll.Employee">
  <select id="findAll"><![CDATA[
    SELECT * FROM HR.EMPLOYEE
  ]]></select>
</mapper>"""
        result = XmlSqlExtractor().extract("Employee.xml", text, XML_CONTEXT)
        edge = next(edge for edge in result.edges if edge.rel_type == S.REL_READS_FROM)

        self.assertEqual(edge.from_key_value, "SourceFile:xml-main:Queries/Employee.xml")
        self.assertEqual(edge.to_key_value, "Table:OracleDB:HR.EMPLOYEE")
        self.assertEqual(edge.properties["query_id"], "Payroll.Employee.findAll")
        self.assertEqual(edge.properties["mapper_tag"], "select")
        self.assertEqual(edge.properties["line"], 2)

    def test_malformed_cdata_uses_controlled_fallback(self):
        text = "<mapper><select><![CDATA[SELECT * FROM HR.EMPLOYEE]]></mapper>"
        result = XmlSqlExtractor().extract("Employee.xml", text, XML_CONTEXT)

        self.assertEqual(
            targets(result, S.REL_READS_FROM), {"Table:OracleDB:HR.EMPLOYEE"}
        )

    def test_malformed_non_sql_xml_fails(self):
        with self.assertRaisesRegex(ValueError, "Invalid XML"):
            XmlSqlExtractor().extract(
                "Employee.xml", "<mapper><select>not sql</mapper>", XML_CONTEXT
            )

    def test_mybatis_include_fragment_is_expanded(self):
        text = """<mapper namespace="Employee">
  <sql id="fromEmployee">FROM HR.EMPLOYEE</sql>
  <select id="find">SELECT * <include refid="fromEmployee" /></select>
</mapper>"""
        result = XmlSqlExtractor().extract("Employee.xml", text, XML_CONTEXT)
        self.assertEqual(
            targets(result, S.REL_READS_FROM), {"Table:OracleDB:HR.EMPLOYEE"}
        )

    def test_duplicate_basenames_keep_distinct_source_owners(self):
        text = "<select>SELECT * FROM HR.EMPLOYEE</select>"
        first = XmlSqlExtractor().extract("Employee.xml", text, XML_CONTEXT)
        second_context = ExtractionContext(
            **{
                **XML_CONTEXT.__dict__,
                "relative_source_path": "Archive/Employee.xml",
            }
        )
        second = XmlSqlExtractor().extract("Employee.xml", text, second_context)

        self.assertNotEqual(
            sources(first, S.REL_READS_FROM), sources(second, S.REL_READS_FROM)
        )


class CSharpSqlExtractorTest(unittest.TestCase):
    def extract(self, text: str) -> ExtractionResult:
        return CSharpSqlExtractor().extract(
            "EmployeeRepository.cs", text, CSHARP_CONTEXT
        )

    def test_literal_forms_and_comment_masking(self):
        text = r'''
namespace Payroll.Data;
public class EmployeeRepository {
  // var ghost = "SELECT * FROM HR.COMMENT_GHOST";
  const string normal = "SELECT * FROM HR.EMPLOYEE";
  const string verbatim = @"SELECT *
FROM HR.PAYROLL_BASE";
  const string interpolated = $"SELECT * FROM HR.DEPARTMENT WHERE ID = {id}";
  const string raw = """
SELECT * FROM HR.JOB_AUDIT
""";
}
'''
        result = self.extract(text)

        self.assertEqual(
            targets(result, S.REL_READS_FROM),
            {
                "Table:OracleDB:HR.EMPLOYEE",
                "Table:OracleDB:HR.PAYROLL_BASE",
                "Table:OracleDB:HR.JOB_AUDIT",
            },
        )
        self.assertEqual(
            sources(result, S.REL_READS_FROM),
            {"Repository:code-map:Payroll:Payroll.Data.EmployeeRepository"},
        )

    def test_non_repository_uses_project_application(self):
        text = '''
// namespace Wrong.Namespace;
namespace Payroll.Services;
public class EmployeeService {
  const string Sql = "SELECT * FROM HR.EMPLOYEE";
}
'''
        result = self.extract(text)

        self.assertEqual(
            sources(result, S.REL_READS_FROM),
            {"Application:code-map:Payroll"},
        )

    def test_namespace_spans_and_static_concatenation(self):
        text = '''
namespace First.Data {
  class EmployeeRepository {
    const string Note = "https://host/*not-comment*/";
    const string Sql = "SELECT * " + "FROM HR.EMPLOYEE";
  }
}
namespace Second.Data {
  class EmployeeRepository {
    const string Sql = "SELECT * FROM HR.DEPARTMENT";
  }
}
'''
        result = self.extract(text)
        self.assertEqual(
            sources(result, S.REL_READS_FROM),
            {
                "Repository:code-map:Payroll:First.Data.EmployeeRepository",
                "Repository:code-map:Payroll:Second.Data.EmployeeRepository",
            },
        )

    def test_nested_namespaces_compose_owner_identity(self):
        text = '''
namespace A { namespace Data {
  class EmployeeRepository { const string Sql = "SELECT * FROM HR.EMPLOYEE"; }
}}
namespace B { namespace Data {
  class EmployeeRepository { const string Sql = "SELECT * FROM HR.DEPARTMENT"; }
}}
'''
        result = self.extract(text)
        self.assertEqual(
            sources(result, S.REL_READS_FROM),
            {
                "Repository:code-map:Payroll:A.Data.EmployeeRepository",
                "Repository:code-map:Payroll:B.Data.EmployeeRepository",
            },
        )

    def test_static_ef_and_dapper_sql(self):
        text = '''
class EmployeeDao {
  void Run() {
    db.Users.FromSqlRaw("SELECT * FROM HR.EMPLOYEE");
    connection.Query("SELECT * FROM HR.DEPARTMENT");
    connection.Execute("INSERT FIRST INTO HR.AUDIT_A VALUES (1) " +
        "INTO HR.AUDIT_B VALUES (1) SELECT * FROM DUAL");
  }
}
'''
        result = self.extract(text)

        self.assertEqual(
            targets(result, S.REL_READS_FROM),
            {"Table:OracleDB:HR.EMPLOYEE", "Table:OracleDB:HR.DEPARTMENT"},
        )
        self.assertEqual(
            sources(result, S.REL_READS_FROM),
            {"Repository:code-map:Payroll:EmployeeDao"},
        )

    def test_dynamic_sql_emits_owner_evidence_without_fake_target(self):
        text = '''
class EmployeeRepository {
  void Run(string tableName) {
    var sql = $"SELECT * FROM HR.{tableName}";
    var other = "SELECT * FROM "
        + tableName;
    var formatted = string.Format("SELECT * FROM HR.{0}", tableName);
  }
}
'''
        result = self.extract(text)
        owner = next(node for node in result.nodes if node.label == S.LABEL_REPOSITORY)
        self.assertTrue(owner.properties["unresolved"])
        self.assertTrue(owner.properties["unresolved_sql"])
        self.assertFalse(any(node.label == S.LABEL_TABLE for node in result.nodes))
        self.assertFalse(result.edges)

    def test_dapper_stored_procedure_variants(self):
        text = '''
class EmployeeRepository {
  void Run() {
    connection.Query(sql: "PKG_PAYROLL.LOAD_MONTH",
        commandType: CommandType.StoredProcedure);
    connection.Query(new CommandDefinition(
        "PKG_PAYROLL.CLOSE_MONTH",
        commandType: CommandType.StoredProcedure));
    connection.Query("PKG_PAYROLL.DAPPER_RUN", "PKG_PAYROLL.DECOY",
        commandType: CommandType.StoredProcedure);
  }
}
'''
        result = self.extract(text)

        self.assertEqual(
            targets(result, S.REL_CALLS),
            {
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.LOAD_MONTH",
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.CLOSE_MONTH",
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.DAPPER_RUN",
            },
        )

    def test_ado_property_order_and_initializer_stored_procedures(self):
        text = '''
class EmployeeRepository {
  void First() {
    cmd.CommandType = CommandType.StoredProcedure;
    cmd.CommandText = "PKG_PAYROLL.LOAD_MONTH";
    cmd.CommandText = "PKG_PAYROLL.DECOY";
    cmd.CommandType = CommandType.StoredProcedure;
    cmd.CommandType = CommandType.Text;
    cmd.CommandText = "PKG_PAYROLL.NOT_A_CALL";
  }
  void Second() {
    using var cmd2 = new SqlCommand {
      CommandText = "PKG_PAYROLL.CLOSE_MONTH",
      CommandType = CommandType.StoredProcedure
    };
    using var cmd3 = new SqlCommand("PKG_PAYROLL.REBUILD_MONTH", connection) {
      CommandType = CommandType.StoredProcedure
    };
  }
}
'''
        result = self.extract(text)

        self.assertEqual(
            targets(result, S.REL_CALLS),
            {
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.LOAD_MONTH",
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.CLOSE_MONTH",
                "Procedure:code-map:OracleDB:HR:PKG_PAYROLL.REBUILD_MONTH",
            },
        )


if __name__ == "__main__":
    unittest.main()
