CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l05 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l04.run();
    UPDATE hr.payroll_base
    SET net_salary = net_salary
    WHERE pay_period = '2026-07';
  END run;
END pkg_deep_l05;
/