CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l10 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l09.run();
    UPDATE hr.payroll_base
    SET net_salary = net_salary + 1
    WHERE EXISTS (SELECT 1 FROM hr.job_audit j WHERE j.job_name = 'DEEP_L09');
  END run;
END pkg_deep_l10;
/