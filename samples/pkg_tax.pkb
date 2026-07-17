CREATE OR REPLACE PACKAGE BODY hr.pkg_tax AS

  FUNCTION calc_tax(p_emp_id NUMBER, p_income NUMBER) RETURN NUMBER IS
    v_rate NUMBER := 0;
    v_country VARCHAR2(2);
  BEGIN
    SELECT e.country_code
    INTO v_country
    FROM hr.employee e
    WHERE e.emp_id = p_emp_id;

    SELECT tr.rate
    INTO v_rate
    FROM fin.tax_rate@fin_dblink tr
    WHERE tr.country_code = v_country
      AND p_income BETWEEN tr.min_income AND tr.max_income;

    INSERT INTO hr.tax_calc_log(emp_id, income_amount, tax_rate, created_at)
    VALUES (p_emp_id, p_income, v_rate, SYSDATE);

    RETURN ROUND(p_income * v_rate, 2);
  END calc_tax;

END pkg_tax;
/
