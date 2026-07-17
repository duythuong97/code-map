CREATE OR REPLACE TRIGGER hr.trg_payroll_audit
AFTER INSERT OR UPDATE OR DELETE ON hr.payroll_base
FOR EACH ROW
BEGIN
  INSERT INTO hr.payroll_audit(
    emp_id, pay_period, old_net_salary, new_net_salary, action_name, created_at
  ) VALUES (
    COALESCE(:NEW.emp_id, :OLD.emp_id),
    COALESCE(:NEW.pay_period, :OLD.pay_period),
    :OLD.net_salary,
    :NEW.net_salary,
    CASE
      WHEN INSERTING THEN 'INSERT'
      WHEN UPDATING THEN 'UPDATE'
      ELSE 'DELETE'
    END,
    SYSDATE
  );
END;
/
