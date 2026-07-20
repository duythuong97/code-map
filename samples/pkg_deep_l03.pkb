CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l03 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l02.run();
    UPDATE hr.payroll_summary s
    SET updated_at = SYSDATE
    WHERE EXISTS (SELECT 1 FROM hr.payroll_base p WHERE p.dept_id = s.dept_id);
  END run;
END pkg_deep_l03;
/