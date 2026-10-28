"""Oracle built-in functions and procedures: calls to these are not references to user code."""
from __future__ import annotations

ORACLE_BUILTIN_CALLS = frozenset({
    "ABS", "ADD_MONTHS", "ASCII", "AVG", "CAST", "CEIL", "CHR", "COALESCE", "CONCAT", "COUNT", "DECODE",
    "DENSE_RANK", "EXTRACT", "FLOOR", "GREATEST", "INITCAP", "INSTR", "LAG", "LAST_DAY", "LEAD", "LEAST",
    "LENGTH", "LENGTHB", "LISTAGG", "LOWER", "LPAD", "LTRIM", "MAX", "MIN", "MOD", "MONTHS_BETWEEN",
    "NEXT_DAY", "NULLIF", "NVL", "NVL2", "POWER", "RAISE_APPLICATION_ERROR", "RANK", "REGEXP_INSTR",
    "REGEXP_LIKE", "REGEXP_REPLACE", "REGEXP_SUBSTR", "REPLACE", "ROUND", "ROW_NUMBER", "RPAD", "RTRIM",
    "SIGN", "SQRT", "SUBSTR", "SUBSTRB", "SUM", "SYSDATE", "SYSTIMESTAMP", "TO_CHAR", "TO_DATE",
    "TO_NUMBER", "TO_TIMESTAMP", "TRANSLATE", "TRIM", "TRUNC", "UPPER", "USER",
})
