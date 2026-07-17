CREATE OR REPLACE PACKAGE BODY hr.pkg_bonus AS

  PROCEDURE apply_bonus(p_period VARCHAR2) IS
  BEGIN
    INSERT INTO hr.bonus_payment(emp_id, pay_period, bonus_amount, reason)
    SELECT p.emp_id, p.pay_period,
           CASE WHEN p.net_salary > 5000 THEN 300 ELSE 100 END,
           'MONTH_CLOSE'
    FROM hr.payroll_base p
    JOIN hr.employee e ON e.emp_id = p.emp_id
    WHERE p.pay_period = p_period
      AND e.status = 'ACTIVE';

    UPDATE hr.payroll_summary s
    SET bonus_total = (
      SELECT SUM(b.bonus_amount)
      FROM hr.bonus_payment b
      JOIN hr.payroll_base p ON p.emp_id = b.emp_id AND p.pay_period = b.pay_period
      WHERE p.dept_id = s.dept_id
        AND b.pay_period = p_period
    )
    WHERE s.pay_period = p_period;
  END apply_bonus;

END pkg_bonus;
/
