CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l06 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l05.run();
    INSERT INTO hr.tax_calc_log(emp_id, income_amount, tax_rate, created_at)
    SELECT p.emp_id, p.net_salary, 0.10, SYSDATE
    FROM hr.payroll_base p;
  END run;
END pkg_deep_l06;
/