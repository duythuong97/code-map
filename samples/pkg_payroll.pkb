CREATE OR REPLACE PACKAGE BODY hr.pkg_payroll AS

  PROCEDURE load_month(p_period VARCHAR2) IS
    v_batch_id NUMBER;
    v_tax NUMBER;
  BEGIN
    SELECT payroll_seq.NEXTVAL INTO v_batch_id FROM dual;

    FOR r IN (
      SELECT e.emp_id, e.dept_id, e.basic_salary, e.allowance
      FROM hr.employee e
      JOIN hr.department d ON d.dept_id = e.dept_id
      WHERE e.status = 'ACTIVE'
    ) LOOP
      v_tax := hr.pkg_tax.calc_tax(r.emp_id, r.basic_salary + r.allowance);

      INSERT INTO hr.payroll_base(
        batch_id, emp_id, dept_id, pay_period, gross_salary, tax_amount, net_salary
      ) VALUES (
        v_batch_id, r.emp_id, r.dept_id, p_period,
        r.basic_salary + r.allowance,
        v_tax,
        r.basic_salary + r.allowance - v_tax
      );
    END LOOP;

    MERGE INTO hr.payroll_summary s
    USING (
      SELECT dept_id, pay_period, COUNT(*) emp_count, SUM(net_salary) total_net
      FROM hr.payroll_base
      WHERE pay_period = p_period
      GROUP BY dept_id, pay_period
    ) x
    ON (s.dept_id = x.dept_id AND s.pay_period = x.pay_period)
    WHEN MATCHED THEN UPDATE SET
      s.emp_count = x.emp_count,
      s.total_net = x.total_net,
      s.updated_at = SYSDATE
    WHEN NOT MATCHED THEN INSERT (dept_id, pay_period, emp_count, total_net, created_at)
      VALUES (x.dept_id, x.pay_period, x.emp_count, x.total_net, SYSDATE);

    INSERT INTO hr.job_audit(job_name, batch_id, status, created_at)
    VALUES ('LOAD_PAYROLL', v_batch_id, 'DONE', SYSDATE);
  END load_month;

  PROCEDURE close_month(p_period VARCHAR2) IS
  BEGIN
    UPDATE hr.payroll_base
    SET closed_flag = 'Y', closed_at = SYSDATE
    WHERE pay_period = p_period;

    DELETE FROM hr.payroll_staging
    WHERE pay_period = p_period;

    hr.pkg_bonus.apply_bonus(p_period);
  END close_month;

END pkg_payroll;
/
