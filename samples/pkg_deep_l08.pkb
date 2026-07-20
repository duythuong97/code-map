CREATE OR REPLACE PACKAGE BODY hr.pkg_deep_l08 AS
  PROCEDURE run IS
  BEGIN
    pkg_deep_l07.run();
    UPDATE hr.payroll_summary s
    SET total_net = total_net + 1
    WHERE EXISTS (SELECT 1 FROM hr.bonus_payment b WHERE b.pay_period = s.pay_period);
  END run;
END pkg_deep_l08;
/