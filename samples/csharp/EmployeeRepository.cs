namespace Samples.Data;

public class EmployeeRepository
{
    private const string FindEmployeesSql =
        "SELECT e.EMPLOYEE_ID FROM HR.EMPLOYEE e " +
        "JOIN HR.DEPARTMENT d ON d.DEPARTMENT_ID = e.DEPARTMENT_ID";

    private const string InsertAuditSql = """
        INSERT INTO HR.JOB_AUDIT (JOB_NAME, STATUS)
        VALUES ('CS_SAMPLE', 'DONE')
        """;

    private const string ClearStageSql = @"DELETE FROM HR.PAYROLL_STAGING
WHERE STATUS = 'DONE'";

    public void CloseMonth(dynamic connection)
    {
        connection.Query(
            "PKG_PAYROLL.CLOSE_MONTH",
            commandType: CommandType.StoredProcedure);
    }

    public string DynamicTable(string tableName) =>
        $"SELECT * FROM HR.{tableName}";
}

public class PayrollService
{
    private const string TaxSql = "SELECT RATE FROM HR.TAX_RATE";
}
