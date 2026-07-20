CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l09 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l08.run();
    INSERT INTO hr.job_audit(job_name, batch_id, status, created_at)
    SELECT 'DEEP_L09', 9, 'OK', SYSDATE
    FROM hr.bonus_payment;
  END run;
END pkg_deep_l09;
/