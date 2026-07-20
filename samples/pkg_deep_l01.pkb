CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l01 AS
  PROCEDURE run IS
  BEGIN
    INSERT INTO hr.payroll_base(emp_id, dept_id, pay_period, gross_salary, net_salary)
    SELECT e.emp_id, e.dept_id, '2026-07', e.basic_salary + e.allowance, e.basic_salary
    FROM hr.employee e;
  END run;
END pkg_deep_l01;
/