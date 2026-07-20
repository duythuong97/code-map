CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l02 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l01.run();
    INSERT INTO hr.job_audit(job_name, batch_id, status, created_at)
    SELECT 'DEEP_L02', 2, 'OK', SYSDATE
    FROM hr.payroll_base;
  END run;
END pkg_deep_l02;
/