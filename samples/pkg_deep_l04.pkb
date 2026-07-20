CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l04 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l03.run();
    INSERT INTO hr.job_audit(job_name, batch_id, status, created_at)
    SELECT 'DEEP_L04', 4, 'OK', SYSDATE
    FROM hr.payroll_summary;
  END run;
END pkg_deep_l04;
/