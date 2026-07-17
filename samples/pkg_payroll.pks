CREATE OR REPLACE PACKAGE hr.pkg_payroll AS
  PROCEDURE load_month(p_period VARCHAR2);
  PROCEDURE close_month(p_period VARCHAR2);
END pkg_payroll;
/
