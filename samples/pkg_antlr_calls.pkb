CREATE OR REPLACE PACKAGE BODY hr.pkg_antlr_calls AS

  FUNCTION base_amount(p_emp_id NUMBER) RETURN NUMBER IS
    v_amount NUMBER;
  BEGIN
    SELECT NVL(e.basic_salary, 0) + NVL(e.allowance, 0)
    INTO v_amount
    FROM hr.employee e
    WHERE e.emp_id = p_emp_id;

    RETURN v_amount;
  END base_amount;

  FUNCTION tax_amount(p_emp_id NUMBER) RETURN NUMBER IS
    nRet NUMBER;
  BEGIN
    nRet := base_amount(p_emp_id) * 0.1;
    RETURN nRet;
  END tax_amount;

  FUNCTION net_amount(p_emp_id NUMBER) RETURN NUMBER IS
  BEGIN
    RETURN base_amount(p_emp_id) - tax_amount(p_emp_id);
  END net_amount;

  PROCEDURE write_audit(p_emp_id NUMBER, p_status VARCHAR2) IS
  BEGIN
    INSERT INTO hr.job_audit(job_name, batch_id, status, created_at)
    VALUES ('ANTLR_CALL_SAMPLE', p_emp_id, p_status, SYSDATE);
  END write_audit;

  PROCEDURE process_employee(p_emp_id NUMBER) IS
    v_net NUMBER;
  BEGIN
    v_net := net_amount(p_emp_id);

    IF v_net > 0 THEN
      write_audit(p_emp_id, 'NET_OK');
    ELSE
      write_audit(p_emp_id, 'NET_ZERO');
    END IF;
  END process_employee;

END pkg_antlr_calls;
/
