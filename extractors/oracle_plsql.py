"""Oracle PL/SQL extractor.

Parses Oracle PL/SQL source files: packages, standalone procedures/functions,
triggers. Extracts SQL DML statements and links them to the enclosing
procedure or function via table-level edges only. Column detail belongs in
drill-down tables, not the main flow graph.

File extensions handled:
  .pks  — package specification
  .pkb  — package body
  .pck  — package (combined spec + body)
  .pls  — PL/SQL source
  .plb  — PL/SQL library (wrapped/unwrapped)
  .fnc  — standalone function
  .prc  — standalone procedure
  .trg  — trigger
  .sql  — if content contains CREATE OR REPLACE PACKAGE / PROCEDURE / FUNCTION / TRIGGER

Creates:
  - Class node  per package  (LABEL_CLASS,   object_type="PACKAGE")
  - Function node per proc/func inside package  (LABEL_FUNCTION)
  - Function node for trigger body              (LABEL_FUNCTION, proc_type="TRIGGER")
  - Table nodes referenced by SQL              (LABEL_TABLE)
    - Edges: BELONGS_TO, READS_FROM, INSERTS_INTO, UPDATES, DELETES_FROM, MERGES_INTO
    - Sequence nodes used by NEXTVAL/CURRVAL plus USES_SEQUENCE edges
  - Edge:  TRIGGERS (TriggerFn→Table  — the table that fires the trigger)
"""

from __future__ import annotations

import re
import logging
from pathlib import Path

from db import schema as S
from db.entities import ExtractionContext, ExtractionResult, GraphEdge, GraphNode
from extractors.base import BaseExtractor

logger = logging.getLogger(__name__)

# ── File type detection ───────────────────────────────────────────────────────
_PLSQL_EXTENSIONS = {".pks", ".pkb", ".pck", ".pls", ".plb", ".fnc", ".prc", ".trg"}
_SQL_EXTENSION = ".sql"

_HAS_PLSQL = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?"
    r"(?:PACKAGE|PROCEDURE|FUNCTION|TRIGGER)\b",
    re.IGNORECASE,
)

# ── Structural patterns ───────────────────────────────────────────────────────

# CREATE [OR REPLACE] PACKAGE [BODY] [schema.]name  AS|IS
_PKG_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?PACKAGE\s+(?:BODY\s+)?"
    r'(?:"?[\w$#]+"?\s*\.\s*)?"?([\w$#]+)"?\s*(?:AS|IS)\b',
    re.IGNORECASE,
)

# PROCEDURE name  (may be in spec or body — we want both for the span map)
_PROC_RE = re.compile(
    r"\bPROCEDURE\s+\"?([\w$#]+)\"?",
    re.IGNORECASE,
)

# FUNCTION name  RETURN ...
_FUNC_RE = re.compile(
    r"\bFUNCTION\s+\"?([\w$#]+)\"?\s*(?:\([^)]{0,300}\))?\s*RETURN\b",
    re.IGNORECASE | re.DOTALL,
)

# CREATE [OR REPLACE] TRIGGER name  BEFORE|AFTER|INSTEAD  dml_event  ON [schema.]table
_TRIGGER_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?TRIGGER\s+"
    r'(?:"?[\w$#]+"?\s*\.\s*)?"?([\w$#]+)"?\s*\n?'
    r"\s*(?:BEFORE|AFTER|INSTEAD\s+OF)\s+"
    r"(?:INSERT|UPDATE|DELETE|INSERT\s+OR\s+UPDATE|INSERT\s+OR\s+DELETE"
    r"|UPDATE\s+OR\s+DELETE|INSERT\s+OR\s+UPDATE\s+OR\s+DELETE)"
    r"(?:\s+OF\s+[\w$#,\s]+)?\s+ON\s+"
    r'(?:"?[\w$#]+"?\s*\.\s*)?"?([\w$#]+)"?',
    re.IGNORECASE | re.DOTALL,
)

# ── SQL DML patterns (Oracle-specific) ────────────────────────────────────────

# INSERT [ALL] INTO [schema.]table
_SQL_INSERT = re.compile(
    r"\bINSERT\s+(?:ALL\s+)?INTO\s+"
    r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# UPDATE [schema.]table  SET
_SQL_UPDATE = re.compile(
    r"\bUPDATE\s+(?:"
    r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?(?:@[\w$#]+)?)'
    r')(?:\s+"?[\w$#]+"?)?\s+SET\b',
    re.IGNORECASE,
)

# DELETE FROM [schema.]table
_SQL_DELETE = re.compile(
    r"\bDELETE\s+FROM\s+" r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# MERGE INTO [schema.]table
_SQL_MERGE = re.compile(
    r"\bMERGE\s+INTO\s+" r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# FROM [schema.]table  — excludes table-function calls e.g. TABLE(...)
_SQL_FROM = re.compile(
    r"\bFROM\s+" r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?(?:@[\w$#]+)?)' r"(?!\s*\()",
    re.IGNORECASE,
)

# [LEFT|RIGHT|INNER|OUTER|CROSS|FULL] JOIN [schema.]table
_SQL_JOIN = re.compile(
    r"\bJOIN\s+" r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# EXECUTE IMMEDIATE 'literal sql'  (not EXECUTE IMMEDIATE variable)
_EXEC_IMMEDIATE = re.compile(
    r"\bEXECUTE\s+IMMEDIATE\s+'([^']+)'",
    re.IGNORECASE,
)

_SEQ_USAGE = re.compile(
    r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?)\s*\.\s*(?:NEXTVAL|CURRVAL)\b',
    re.IGNORECASE,
)

_CONSTANT_RE = re.compile(
    r"^\s*\"?([A-Za-z_][\w$#]*)\"?\s+CONSTANT\s+(.+?)\s*(?::=\s*(.+?))?\s*;",
    re.IGNORECASE | re.MULTILINE,
)
_VARIABLE_RE = re.compile(
    r"^\s*\"?([A-Za-z_][\w$#]*)\"?\s+((?!(?:CONSTANT|TYPE|PROCEDURE|FUNCTION|CURSOR)\b).+?)\s*(?::=\s*(.+?))?\s*;",
    re.IGNORECASE | re.MULTILINE,
)
_TYPE_RE = re.compile(
    r"^\s*TYPE\s+\"?([A-Za-z_][\w$#]*)\"?\s+IS\s+(.+?);",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_DYNAMIC_SQL_ASSIGN_RE = re.compile(
    r"\b([A-Za-z_][\w$#]*)\s*:=\s*((?:'[^']*'\s*(?:\|\|\s*)?)+)\s*;",
    re.IGNORECASE,
)
_EXEC_IMMEDIATE_VAR = re.compile(
    r"\bEXECUTE\s+IMMEDIATE\s+([A-Za-z_][\w$#]*)\b",
    re.IGNORECASE,
)

# Inter-package call at statement level: PKG_NAME.PROC_NAME(
# Anchored to statement start (after ; or BEGIN/THEN/ELSE/LOOP/newline+spaces)
_PKG_CALL_RE = re.compile(
    r"(?:^|;|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b|\bRETURN\b)\s+"
    r"(?:[A-Z][A-Z0-9_$#]{1,29}\.)?([A-Z][A-Z0-9_$#]{2,29})\.([A-Z][A-Z0-9_$#]{1,29})\s*\(",
    re.IGNORECASE | re.MULTILINE,
)

# Oracle built-in packages to exclude from inter-package call detection
_ORACLE_BUILTIN_PKGS: frozenset[str] = frozenset(
    {
        "DBMS_OUTPUT",
        "DBMS_SQL",
        "DBMS_LOB",
        "DBMS_UTILITY",
        "DBMS_METADATA",
        "DBMS_LOCK",
        "DBMS_ALERT",
        "DBMS_PIPE",
        "DBMS_SCHEDULER",
        "DBMS_JOB",
        "DBMS_CRYPTO",
        "DBMS_RANDOM",
        "DBMS_TRANSACTION",
        "DBMS_XMLGEN",
        "UTL_FILE",
        "UTL_HTTP",
        "UTL_SMTP",
        "UTL_RAW",
        "UTL_I18N",
        "UTL_URL",
        "APEX_APPLICATION",
        "APEX_UTIL",
        "APEX_JSON",
        "APEX_ITEM",
        "SYS",
        "STANDARD",
    }
)

# PL/SQL keywords that can precede a dot and look like package names
_PLSQL_KW_PREFIXES: frozenset[str] = frozenset(
    {
        "IF",
        "END",
        "ELSIF",
        "EXCEPTION",
        "WHEN",
        "INTO",
        "FROM",
        "HAVING",
        "GROUP",
        "ORDER",
        "WHERE",
        "ON",
        "SET",
        "IN",
        "OUT",
    }
)

# Oracle objects to skip (pseudo-tables, system catalog, built-in functions)
_SKIP_NAMES: frozenset[str] = frozenset(
    {
        "DUAL",
        "ROWNUM",
        "ROWID",
        "SYSDATE",
        "SYSTIMESTAMP",
        "LEVEL",
        "XMLTABLE",
        "TABLE",
        "VIEW",
        "INDEX",
        "SELECT",
        "WITH",
    }
)
_SYS_PREFIX_RE = re.compile(
    r"^(?:SYS_|ALL_|DBA_|USER_|V\$|GV\$|DBMS_|UTL_|APEX_|MVIEW)",
    re.IGNORECASE,
)


class OraclePlSqlExtractor(BaseExtractor):
    def can_handle(self, file_path: str, text: str) -> bool:
        ext = Path(file_path).suffix.lower()
        if ext in _PLSQL_EXTENSIONS:
            return True
        if ext == _SQL_EXTENSION:
            return bool(_HAS_PLSQL.search(text[:3_000]))
        return False

    def extract(
        self, file_path: str, text: str, context: ExtractionContext
    ) -> ExtractionResult:
        result = ExtractionResult(
            source_file=file_path, extractor_name="OraclePlSqlExtractor"
        )
        repository = context.repository
        service = context.service_name or context.infer_service_from_path(file_path)

        # ── Package node ──────────────────────────────────────────────────────
        pkg_name: str | None = None
        pkg_qname: str | None = None
        pm = _PKG_RE.search(text)
        if pm:
            pkg_name = pm.group(1).upper()
            pkg_qname = f"{S.LABEL_PLSQL_PACKAGE}:{repository}:{pkg_name}"
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_PLSQL_PACKAGE,
                    key="qualified_name",
                    key_value=pkg_qname,
                    properties={
                        "qualified_name": pkg_qname,
                        "name": pkg_name,
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "object_type": "PACKAGE",
                        "layer": "logic",
                    },
                ),
            )

        # ── Procedure / Function spans ────────────────────────────────────────
        # spans: sorted list of (line_no, func_qname) used to assign SQL ops
        # span_labels: maps qname → label for building typed edges
        spans: list[tuple[int, str]] = []
        span_labels: dict[str, str] = {}

        for m in _PROC_RE.finditer(text):
            fn = m.group(1).upper()
            full = f"{pkg_name}.{fn}" if pkg_name else fn
            qname = f"{S.LABEL_PROCEDURE}:{repository}:{full}"
            line = _line_of(text, m.start())
            spans.append((line, qname))
            span_labels[qname] = S.LABEL_PROCEDURE
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_PROCEDURE,
                    key="qualified_name",
                    key_value=qname,
                    properties={
                        "qualified_name": qname,
                        "name": fn,
                        "package": pkg_name or "",
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "line": line + 1,
                        "proc_type": "PROCEDURE",
                        "layer": "logic",
                    },
                ),
            )
            if pkg_qname:
                result.edges.append(
                    GraphEdge(
                        from_label=S.LABEL_PROCEDURE,
                        from_key="qualified_name",
                        from_key_value=qname,
                        to_label=S.LABEL_PLSQL_PACKAGE,
                        to_key="qualified_name",
                        to_key_value=pkg_qname,
                        rel_type=S.REL_BELONGS_TO,
                    )
                )

        for m in _FUNC_RE.finditer(text):
            fn = m.group(1).upper()
            full = f"{pkg_name}.{fn}" if pkg_name else fn
            qname = f"{S.LABEL_SQL_FUNCTION}:{repository}:{full}"
            line = _line_of(text, m.start())
            spans.append((line, qname))
            span_labels[qname] = S.LABEL_SQL_FUNCTION
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_SQL_FUNCTION,
                    key="qualified_name",
                    key_value=qname,
                    properties={
                        "qualified_name": qname,
                        "name": fn,
                        "package": pkg_name or "",
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "line": line + 1,
                        "proc_type": "FUNCTION",
                        "layer": "logic",
                    },
                ),
            )
            if pkg_qname:
                result.edges.append(
                    GraphEdge(
                        from_label=S.LABEL_SQL_FUNCTION,
                        from_key="qualified_name",
                        from_key_value=qname,
                        to_label=S.LABEL_PLSQL_PACKAGE,
                        to_key="qualified_name",
                        to_key_value=pkg_qname,
                        rel_type=S.REL_BELONGS_TO,
                    )
                )

        spans.sort(key=lambda x: x[0])

        # Fallback when no procedures found (e.g. anonymous block or .fnc/.prc with single body)
        fallback_qname: str | None = None
        fallback_label: str = S.LABEL_PROCEDURE
        if not spans:
            stem = Path(file_path).stem.upper()
            fallback_qname = f"{S.LABEL_PROCEDURE}:{repository}:{stem}"
            span_labels[fallback_qname] = S.LABEL_PROCEDURE
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_PROCEDURE,
                    key="qualified_name",
                    key_value=fallback_qname,
                    properties={
                        "qualified_name": fallback_qname,
                        "name": stem,
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "layer": "logic",
                    },
                ),
            )

        ctx_schema = (context.extra_tags.get("schema", "") or "").upper()
        scan_text = _mask_string_literals(text)

        # ── Constants ───────────────────────────────────────────────────────
        for m in _CONSTANT_RE.finditer(text):
            name = m.group(1).upper()
            line_no = _line_of(text, m.start())
            owner_qname = _resolve(spans, line_no) or pkg_qname or fallback_qname
            owner_label = (
                span_labels.get(owner_qname, fallback_label)
                if owner_qname
                else S.LABEL_PLSQL_PACKAGE
            )
            scope = owner_qname.rsplit(":", 1)[-1] if owner_qname else pkg_name or Path(file_path).stem.upper()
            qname = f"{S.LABEL_PLSQL_CONSTANT}:{repository}:{scope}.{name}"
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_PLSQL_CONSTANT,
                    key="qualified_name",
                    key_value=qname,
                    properties={
                        "qualified_name": qname,
                        "name": name,
                        "data_type": re.sub(r"\s+", " ", (m.group(2) or "").strip()).upper(),
                        "value": (m.group(3) or "").strip(),
                        "package": pkg_name or "",
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "line": line_no + 1,
                        "layer": "logic",
                    },
                ),
            )
            if owner_qname:
                result.edges.append(
                    GraphEdge(
                        from_label=S.LABEL_PLSQL_CONSTANT,
                        from_key="qualified_name",
                        from_key_value=qname,
                        to_label=owner_label,
                        to_key="qualified_name",
                        to_key_value=owner_qname,
                        rel_type=S.REL_BELONGS_TO,
                        properties={"line": line_no + 1, "source_file": file_path},
                    )
                )

        for regex, label in ((_VARIABLE_RE, S.LABEL_PLSQL_VARIABLE), (_TYPE_RE, S.LABEL_PLSQL_TYPE)):
            for m in regex.finditer(text):
                name = m.group(1).upper()
                if name in {"BEGIN", "END", "IF", "LOOP", "NULL", "RETURN"}:
                    continue
                line_no = _line_of(text, m.start())
                owner_qname = _resolve(spans, line_no) or pkg_qname or fallback_qname
                owner_label = span_labels.get(owner_qname, fallback_label) if owner_qname else S.LABEL_PLSQL_PACKAGE
                scope = owner_qname.rsplit(":", 1)[-1] if owner_qname else pkg_name or Path(file_path).stem.upper()
                qname = f"{label}:{repository}:{scope}.{name}"
                _add_unique(
                    result,
                    GraphNode(
                        label=label,
                        key="qualified_name",
                        key_value=qname,
                        properties={
                            "qualified_name": qname,
                            "name": name,
                            "declaration": re.sub(r"\s+", " ", (m.group(2) or "").strip()).upper(),
                            "value": (m.group(3) or "").strip() if label == S.LABEL_PLSQL_VARIABLE else "",
                            "package": pkg_name or "",
                            "service": service,
                            "repository": repository,
                            "source_file": file_path,
                            "line": line_no + 1,
                            "layer": "logic",
                        },
                    ),
                )
                if owner_qname:
                    result.edges.append(
                        GraphEdge(
                            from_label=label,
                            from_key="qualified_name",
                            from_key_value=qname,
                            to_label=owner_label,
                            to_key="qualified_name",
                            to_key_value=owner_qname,
                            rel_type=S.REL_BELONGS_TO,
                            properties={"line": line_no + 1, "source_file": file_path},
                        )
                    )

        # ── SQL DML scan ─────────────────────────────────────────────────────
        # Collect table-level operations; column names stay edge metadata, not graph nodes.
        ops: list[tuple[int, str, str, list[str]]] = []

        for m in _SQL_INSERT.finditer(scan_text):
            t = _norm(m.group(1))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        t,
                        "INSERT",
                        _insert_columns(scan_text, m.end()),
                    )
                )

        for m in _SQL_UPDATE.finditer(scan_text):
            t = _norm(m.group(1))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        t,
                        "UPDATE",
                        _update_columns(scan_text, m.end()),
                    )
                )

        for m in _SQL_DELETE.finditer(scan_text):
            t = _norm(m.group(1))
            if not _skip(t):
                ops.append((_line_of(text, m.start()), t, "DELETE", []))

        for m in _SQL_MERGE.finditer(scan_text):
            t = _norm(m.group(1))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        t,
                        "MERGE",
                        _merge_columns(scan_text, m.end()),
                    )
                )

        for m in _SQL_FROM.finditer(scan_text):
            t = _norm(m.group(1))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        t,
                        "SELECT",
                        _read_columns(scan_text, m.start(), m.end()),
                    )
                )

        for m in _SQL_JOIN.finditer(scan_text):
            t = _norm(m.group(1))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        t,
                        "SELECT",
                        _read_columns(scan_text, m.start(), m.end()),
                    )
                )

        # EXECUTE IMMEDIATE with string literal — parse the embedded SQL
        for m in _EXEC_IMMEDIATE.finditer(text):
            for t, op in _tables_from_sql_literal(m.group(1)):
                ops.append((_line_of(text, m.start()), t, op, []))
        dynamic_sql_vars = _dynamic_sql_literals(text)
        for m in _EXEC_IMMEDIATE_VAR.finditer(text):
            for t, op in _tables_from_sql_literal(dynamic_sql_vars.get(m.group(1).upper(), "")):
                ops.append((_line_of(text, m.start()), t, op, []))

        # ── Assign each op to enclosing procedure/function ────────────────────
        seen_edges: set[tuple[str, str, str, int, str]] = set()
        for line_no, table_name, op, columns in ops:
            func_qname = _resolve(spans, line_no) or fallback_qname
            if not func_qname:
                continue
            func_label = span_labels.get(func_qname, fallback_label)

            # Apply schema prefix fallback: unqualified names → ctx_schema.NAME
            full_tbl_name = (
                f"{ctx_schema}.{table_name}"
                if ctx_schema and "." not in table_name
                else table_name
            )
            tbl_qname = context.table_qname(full_tbl_name)
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_TABLE,
                    key="qualified_name",
                    key_value=tbl_qname,
                    properties={
                        "qualified_name": tbl_qname,
                        "name": table_name,
                        "schema": ctx_schema or None,
                        "repository": repository,
                        "db_name": context.db_name,
                        "layer": "data",
                    },
                ),
            )
            rel = _rel_type(op)
            edge_key = (func_qname, tbl_qname, rel, line_no + 1, op)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            result.edges.append(
                GraphEdge(
                    from_label=func_label,
                    from_key="qualified_name",
                    from_key_value=func_qname,
                    to_label=S.LABEL_TABLE,
                    to_key="qualified_name",
                    to_key_value=tbl_qname,
                    rel_type=rel,
                    properties={
                        "operation": op,
                        "columns": columns,
                        "line": line_no + 1,
                        "source_file": file_path,
                    },
                )
            )

        for m in _SEQ_USAGE.finditer(scan_text):
            seq_name = _norm(m.group(1))
            if _skip(seq_name):
                continue
            line_no = _line_of(text, m.start())
            func_qname = _resolve(spans, line_no) or fallback_qname
            if not func_qname:
                continue
            func_label = span_labels.get(func_qname, fallback_label)
            full_seq_name = (
                f"{ctx_schema}.{seq_name}"
                if ctx_schema and "." not in seq_name
                else seq_name
            )
            seq_qname = (
                f"{S.LABEL_SEQUENCE}:{context.db_name}:{full_seq_name}"
                if context.db_name
                else f"{S.LABEL_SEQUENCE}:{full_seq_name}"
            )
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_SEQUENCE,
                    key="qualified_name",
                    key_value=seq_qname,
                    properties={
                        "qualified_name": seq_qname,
                        "name": seq_name,
                        "schema": ctx_schema or None,
                        "repository": repository,
                        "db_name": context.db_name,
                        "layer": "data",
                    },
                ),
            )
            edge_key = (
                func_qname,
                seq_qname,
                S.REL_USES_SEQUENCE,
                line_no + 1,
                "SEQUENCE",
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            result.edges.append(
                GraphEdge(
                    from_label=func_label,
                    from_key="qualified_name",
                    from_key_value=func_qname,
                    to_label=S.LABEL_SEQUENCE,
                    to_key="qualified_name",
                    to_key_value=seq_qname,
                    rel_type=S.REL_USES_SEQUENCE,
                    properties={
                        "operation": "SEQUENCE",
                        "line": line_no + 1,
                        "source_file": file_path,
                    },
                )
            )

        # ── Inter-package CALLS ───────────────────────────────────────────────
        seen_calls: set[tuple[str, str]] = set()
        for m in _PKG_CALL_RE.finditer(scan_text):
            pkg_ref = m.group(1).upper()
            proc_ref = m.group(2).upper()
            if pkg_ref in _ORACLE_BUILTIN_PKGS or pkg_ref in _PLSQL_KW_PREFIXES:
                continue
            if _skip(pkg_ref) or _skip(proc_ref):
                continue
            if pkg_name and pkg_ref == pkg_name:
                continue
            call_line = _line_of(text, m.start())
            caller_qname = _resolve(spans, call_line) or fallback_qname
            if not caller_qname:
                continue
            caller_label = span_labels.get(caller_qname, fallback_label)
            target_full = f"{pkg_ref}.{proc_ref}"
            # Default to PROCEDURE — gets MERGEd with the real node if loaded later
            target_qname = f"{S.LABEL_PROCEDURE}:{repository}:{target_full}"
            edge_key = (caller_qname, target_qname)
            if edge_key in seen_calls:
                continue
            seen_calls.add(edge_key)
            result.edges.append(
                GraphEdge(
                    from_label=caller_label,
                    from_key="qualified_name",
                    from_key_value=caller_qname,
                    to_label=S.LABEL_PROCEDURE,
                    to_key="qualified_name",
                    to_key_value=target_qname,
                    rel_type=S.REL_CALLS,
                    properties={
                        "call_type": "package_proc",
                        "line": call_line + 1,
                        "source_file": file_path,
                    },
                )
            )

        # ── Triggers ─────────────────────────────────────────────────────────
        for m in _TRIGGER_RE.finditer(text):
            trg_name = m.group(1).upper()
            fired_on = _norm(m.group(2))
            if _skip(fired_on):
                continue

            # Model the trigger as a Trigger node
            trg_fn_qname = f"{S.LABEL_TRIGGER}:{repository}:{trg_name}"
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_TRIGGER,
                    key="qualified_name",
                    key_value=trg_fn_qname,
                    properties={
                        "qualified_name": trg_fn_qname,
                        "name": trg_name,
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "proc_type": "TRIGGER",
                        "trigger_on_table": fired_on,
                        "layer": "logic",
                    },
                ),
            )

            # The table the trigger fires on — apply schema prefix from context
            full_fired_on = (
                f"{ctx_schema}.{fired_on}"
                if ctx_schema and "." not in fired_on
                else fired_on
            )
            tbl_qname = context.table_qname(full_fired_on)
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_TABLE,
                    key="qualified_name",
                    key_value=tbl_qname,
                    properties={
                        "qualified_name": tbl_qname,
                        "name": fired_on,
                        "schema": ctx_schema or None,
                        "repository": repository,
                        "db_name": context.db_name,
                        "layer": "data",
                    },
                ),
            )

            # TRIGGERS edge: trigger fires ON the table
            result.edges.append(
                GraphEdge(
                    from_label=S.LABEL_TRIGGER,
                    from_key="qualified_name",
                    from_key_value=trg_fn_qname,
                    to_label=S.LABEL_TABLE,
                    to_key="qualified_name",
                    to_key_value=tbl_qname,
                    rel_type=S.REL_TRIGGERS,
                    properties={
                        "framework": "Oracle Trigger",
                        "source_file": file_path,
                    },
                )
            )

        return result


# ── Helpers ───────────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    """Strip quotes/space, preserve schema and dblink."""
    return re.sub(r"\s+", "", name.strip().replace('"', "")).upper()


def _skip(name: str) -> bool:
    if not name or len(name) < 2:
        return True
    if name in _SKIP_NAMES:
        return True
    if bool(_SYS_PREFIX_RE.match(name)):
        return True
    if name.startswith("("):
        return True
    return False


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n")


def _mask_string_literals(text: str) -> str:
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            out.append(" ")
            if in_str and i + 1 < len(text) and text[i + 1] == "'":
                out.append(" ")
                i += 2
                continue
            in_str = not in_str
        else:
            out.append("\n" if ch == "\n" else (" " if in_str else ch))
        i += 1
    return "".join(out)


def _resolve(spans: list[tuple[int, str]], line_no: int) -> str | None:
    """Return the qname of the nearest procedure/function declared at or before line_no."""
    result = None
    for span_line, qname in spans:
        if span_line <= line_no:
            result = qname
        else:
            break
    return result


def _rel_type(op: str) -> str:
    return {
        "INSERT": S.REL_INSERTS_INTO,
        "UPDATE": S.REL_UPDATES,
        "DELETE": S.REL_DELETES_FROM,
        "MERGE": S.REL_MERGES_INTO,
    }.get(op, S.REL_READS_FROM)


def _insert_columns(text: str, pos: int) -> list[str]:
    chunk = text[pos : pos + 2_000]
    m = re.match(r"\s*\(([^)]*)\)", chunk, re.DOTALL)
    return _clean_columns(m.group(1).split(",")) if m else []


def _update_columns(text: str, pos: int) -> list[str]:
    chunk = re.split(
        r"\bWHERE\b|\bRETURNING\b|;", text[pos : pos + 4_000], 1, re.IGNORECASE
    )[0]
    return _clean_columns(part.split("=", 1)[0] for part in _split_top_level(chunk))


def _merge_columns(text: str, pos: int) -> list[str]:
    m = re.search(
        r"\bUPDATE\s+SET\s+(.+?)(?:\bWHEN\b|;)",
        text[pos : pos + 8_000],
        re.IGNORECASE | re.DOTALL,
    )
    return (
        _clean_columns(part.split("=", 1)[0] for part in _split_top_level(m.group(1)))
        if m
        else []
    )


def _read_columns(text: str, table_start: int, table_end: int) -> list[str]:
    alias = _table_alias(text, table_end)
    before = text[max(0, table_start - 4_000) : table_start]
    select_pos = before.upper().rfind("SELECT")
    select_list = before[select_pos + 6 :] if select_pos >= 0 else ""
    after = re.split(
        r"\bJOIN\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bLOOP\b|;",
        text[table_end : table_end + 2_000],
        1,
        re.IGNORECASE | re.DOTALL,
    )[0]
    if alias:
        return _clean_columns(
            m.group(1)
            for m in re.finditer(
                rf"\b{re.escape(alias)}\s*\.\s*([\w$#]+)\b",
                select_list + " " + after,
                re.IGNORECASE,
            )
        )
    return _select_columns(select_list)


def _table_alias(text: str, pos: int) -> str:
    m = re.match(
        r"\s+(?!WHERE\b|JOIN\b|ON\b|GROUP\b|ORDER\b|HAVING\b|LOOP\b|CONNECT\b)(?:AS\s+)?\"?([\w$#]+)\"?",
        text[pos : pos + 80],
        re.IGNORECASE,
    )
    return _norm(m.group(1)) if m else ""


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _clean_columns(items) -> list[str]:
    cols: list[str] = []
    for item in items:
        col = _norm(str(item).split(".")[-1])
        if col and not _skip(col) and col not in cols:
            cols.append(col)
    return cols


_SQL_FUNCTION_NAMES = {
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "NVL",
    "COALESCE",
    "ROUND",
    "TRUNC",
    "TO_CHAR",
    "TO_DATE",
    "TO_NUMBER",
    "DECODE",
    "CASE",
    "SYSDATE",
    "SYSTIMESTAMP",
}


def _select_columns(select_list: str) -> list[str]:
    cols: list[str] = []
    select_list = re.split(r"\bINTO\b", select_list, 1, re.IGNORECASE)[0]
    for expr in _split_top_level(select_list):
        if "*" in expr and not re.search(r"\.\s*\*", expr):
            continue
        for fn in _SQL_FUNCTION_NAMES:
            expr = re.sub(rf"\b{fn}\s*\(", "(", expr, flags=re.IGNORECASE)
        matches = re.findall(r"(?:\b[\w$#]+\s*\.\s*)?\b([\w$#]+)\b", expr)
        if not matches:
            continue
        candidate = _norm(matches[0] if len(matches) == 1 else matches[-2])
        if candidate and not _skip(candidate) and candidate not in cols:
            cols.append(candidate)
    return cols


def _add_unique(result: ExtractionResult, node: GraphNode) -> None:
    if node.key_value not in {n.key_value for n in result.nodes}:
        result.nodes.append(node)


# Mini SQL scanners for EXECUTE IMMEDIATE string content
_MINI_INSERT = re.compile(
    r"\bINSERT\s+(?:INTO\s+)?(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE
)
_MINI_UPDATE = re.compile(r"\bUPDATE\s+(?:[\w$#]+\.)?([\w$#]+)\s+SET\b", re.IGNORECASE)
_MINI_DELETE = re.compile(r"\bDELETE\s+FROM\s+(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE)
_MINI_MERGE = re.compile(r"\bMERGE\s+INTO\s+(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE)
_MINI_FROM = re.compile(r"\bFROM\s+(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE)


def _tables_from_sql_literal(sql: str) -> list[tuple[str, str]]:
    results = []
    for m in _MINI_INSERT.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "INSERT"))
    for m in _MINI_UPDATE.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "UPDATE"))
    for m in _MINI_DELETE.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "DELETE"))
    for m in _MINI_MERGE.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "MERGE"))
    for m in _MINI_FROM.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "SELECT"))
    return results

def _dynamic_sql_literals(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _DYNAMIC_SQL_ASSIGN_RE.finditer(text):
        parts = re.findall(r"'([^']*)'", m.group(2))
        if parts:
            out[m.group(1).upper()] = "".join(parts)
    return out
