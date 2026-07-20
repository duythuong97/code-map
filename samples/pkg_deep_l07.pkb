CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l07 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l06.run();
    INSERT INTO hr.bonus_payment(emp_id, pay_period, bonus_amount, reason)
    SELECT p.emp_id, p.pay_period, 100, 'DEEP_TEST'
    FROM hr.payroll_base p;
  END run;
END pkg_deep_l07;
/