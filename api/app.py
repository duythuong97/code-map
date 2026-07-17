"""Flask web app for local SQLite code lineage.

Run:
    .venv/bin/python api/app.py --db code_map.db --port 8000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template, request, send_from_directory
from common.config import load_app_config, resolve_path
from common.source_text import read_source_text
from db.writer import ensure_db_schema

APP_CONFIG_PATH = resolve_path(os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"), ROOT)
APP_CONFIG = load_app_config(APP_CONFIG_PATH)
DB_PATH = resolve_path(APP_CONFIG["db"], APP_CONFIG_PATH.parent)
WEB_DIST = ROOT / "webapp" / "dist"
URL_PREFIX = APP_CONFIG["url_prefix"]

app = Flask(__name__, template_folder="templates", static_folder="static")

class PrefixMiddleware:
    def __init__(self, wrapped, prefix: str):
        self.wrapped = wrapped
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if not self.prefix:
            return self.wrapped(environ, start_response)
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(f"{self.prefix}/"):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wrapped(environ, start_response)

app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)

NODE_STYLES = {
    "Table": {"kind": "table", "icon": "database", "className": "table", "visible": True},
    "Procedure": {"kind": "code", "icon": "code", "className": "code", "visible": True},
    "SQLFunction": {"kind": "code", "icon": "box", "className": "code", "visible": True},
    "Function": {"kind": "code", "icon": "box", "className": "code", "visible": True},
    "Trigger": {"kind": "trigger", "icon": "git-branch", "className": "trigger", "visible": True},
    "PLSQLPackage": {"kind": "package", "icon": "file-code", "className": "file", "visible": True},
    "Sequence": {"kind": "sequence", "icon": "hash", "className": "sequence", "visible": True},
    "Cursor": {"kind": "detail", "icon": "file-code", "className": "file", "visible": False},
    "Column": {"kind": "detail", "icon": "list", "className": "detail", "visible": False},
}
EDGE_STYLES = {
    "READS": {"color": "#2563eb", "visible": True},
    "WRITES": {"color": "#16a34a", "visible": True},
    "CALLS": {"color": "#f97316", "visible": True},
    "TRIGGERS": {"color": "#dc2626", "visible": True},
    "USES": {"color": "#14b8a6", "visible": True},
    "REMOTE_READS": {"color": "#0891b2", "visible": True},
    "DERIVES": {"color": "#9333ea", "visible": False},
    "DETAIL": {"color": "#64748b", "visible": False},
}


def conn() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_db_schema(db)
    return db


def db_ready(db: sqlite3.Connection) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
    )


def db_error() -> dict[str, str]:
    return {
        "error": f"Database chưa có dữ liệu. Chạy: python -m extractors.run_all --config {APP_CONFIG_PATH}"
    }

TABLE_DEF_COLUMNS = """
SELECT code, name_ja, name_en, description
FROM table_definitions
WHERE db_name=? AND schema_name=? AND table_name=?
"""
COLUMN_DEF_COLUMNS = """
SELECT id, COALESCE(code, name) AS code, name, name_ja, name_en, description, comment
FROM table_columns
WHERE db_name=? AND schema_name=? AND table_name=?
ORDER BY code, name
"""

@app.get("/api/graph-contract")
def graph_contract():
    return jsonify({"nodes": NODE_STYLES, "edges": EDGE_STYLES})


@app.get("/")
def index():
    if (WEB_DIST / "index.html").exists():
        return send_from_directory(WEB_DIST, "index.html")
    return render_template("index.html")


@app.get("/app-config.js")
def app_config_js():
    return "window.CODE_MAP_CONFIG = " + json.dumps({"urlPrefix": URL_PREFIX}) + ";", 200, {"Content-Type": "application/javascript"}


@app.get("/assets/<path:path>")
def web_assets(path: str):
    return send_from_directory(WEB_DIST / "assets", path)


@app.get("/api/stats")
def stats():
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error())
        return jsonify(
            {
                "nodes": db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "edges": db.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                "tables": db.execute(
                    "SELECT COUNT(*) FROM nodes WHERE label='Table'"
                ).fetchone()[0],
                "files": db.execute(
                    "SELECT COUNT(DISTINCT source_file) FROM edges WHERE source_file IS NOT NULL"
                ).fetchone()[0],
            }
        )


@app.get("/api/roots")
def roots():
    labels = tuple(label for label, style in NODE_STYLES.items() if style["visible"])
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error())
        rows = db.execute(
            f"""
            SELECT label, qualified_name, name
            FROM nodes
            WHERE label IN ({','.join('?' for _ in labels)})
                            AND NOT (label='Table' AND upper(name) IN ('NEW','OLD','R','SRC'))
            ORDER BY CASE label
                WHEN 'PLSQLPackage' THEN 0
                WHEN 'Procedure' THEN 1
                WHEN 'SQLFunction' THEN 2
                WHEN 'Table' THEN 3
                ELSE 4
            END, name
            """,
            labels,
        ).fetchall()
        return jsonify([dict(r) for r in rows])

@app.get("/api/schemas")
def schemas():
    with conn() as db:
        if not db_ready(db):
            return jsonify([])
        rows = db.execute("SELECT qualified_name, properties_json FROM nodes").fetchall()
    found = set()
    for row in rows:
        qn = row["qualified_name"] or ""
        if qn.startswith("Table:"):
            _, schema_name, _ = split_table_qname(qn)
            if schema_name:
                found.add(schema_name)
        try:
            schema_name = json.loads(row["properties_json"] or "{}").get("schema")
            if schema_name:
                found.add(str(schema_name))
        except Exception:
            pass
    return jsonify(sorted(found))


@app.get("/api/table-definition")
def table_definition():
    qn = request.args.get("qname", "")
    db_name, schema_name, table_name = split_table_qname(qn)
    if not table_name:
        return jsonify([])
    with conn() as db:
        table = db.execute(TABLE_DEF_COLUMNS, (db_name, schema_name, table_name)).fetchone()
        rows = db.execute(
            COLUMN_DEF_COLUMNS,
            (db_name, schema_name, table_name),
        ).fetchall()
        return jsonify({"table": dict(table) if table else {}, "columns": [dict(r) for r in rows]})

def table_display_name(db: sqlite3.Connection, qn: str, fallback: str) -> str:
    db_name, schema_name, table_name = split_table_qname(qn)
    if not table_name:
        return fallback
    row = db.execute(TABLE_DEF_COLUMNS, (db_name, schema_name, table_name)).fetchone()
    if not row:
        return fallback
    code = row["code"] or table_name or fallback
    name_ja = row["name_ja"] or "-"
    name_en = row["name_en"] or "-"
    return f"{code} ({name_ja} - {name_en})"

def enrich_table_node(db: sqlite3.Connection, row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    if data.get("label") == "Table":
        data["name"] = table_display_name(db, data.get("qualified_name", ""), data.get("name", ""))
    return data

@app.get("/api/node-detail")
def node_detail():
    qn = request.args.get("qname", "")
    with conn() as db:
        if not db_ready(db):
            return jsonify({"node": None, "code": "", "columns": []})
        row = db.execute(
            "SELECT label, qualified_name, name, properties_json FROM nodes WHERE qualified_name=?",
            (qn,),
        ).fetchone()
        if not row:
            return jsonify({"node": None, "code": "", "columns": []})
        data = dict(row)
        try:
            props = json.loads(data.pop("properties_json") or "{}")
        except Exception:
            props = {}
        line = int(props.get("line") or 1)
        code = "" if data["label"] == "Table" else source_object_snippet(props.get("source_file") or "", line, data["name"], data["label"])
        columns = []
        table = {}
        if data["label"] == "Table":
            db_name, schema_name, table_name = split_table_qname(qn)
            table_row = db.execute(TABLE_DEF_COLUMNS, (db_name, schema_name, table_name)).fetchone()
            table = dict(table_row) if table_row else {}
            columns = [
                dict(r)
                for r in db.execute(
                    COLUMN_DEF_COLUMNS,
                    (db_name, schema_name, table_name),
                ).fetchall()
            ]
        impact = table_impact_data(db, qn) if data["label"] == "Table" else {"columns": []}
        return jsonify({"node": api_node(enrich_table_node(db, data)), "properties": props, "code": code, "table": table, "columns": columns, "impact": impact})

@app.get("/api/table-impact")
def table_impact():
    qn = request.args.get("qname", "")
    with conn() as db:
        if not db_ready(db):
            return jsonify({"columns": []})
        return jsonify(table_impact_data(db, qn))


@app.get("/api/flow")
def flow():
    qn = request.args.get("qname", "")
    direction = request.args.get("direction", "both").lower()
    requested_types = {
        item.strip().upper()
        for item in request.args.get("types", "").split(",")
        if item.strip()
    }
    with conn() as db:
        if not db_ready(db):
            return jsonify({"node": None, "flows": [], "evidence": []})
        node = db.execute(
            "SELECT label, qualified_name, name FROM nodes WHERE qualified_name=?",
            (qn,),
        ).fetchone()
        hidden_labels = tuple(label for label, style in NODE_STYLES.items() if not style["visible"])
        if direction == "in":
            qname_clause = "e.to_qname=?"
            qname_params = (qn,)
        elif direction == "out":
            qname_clause = "e.from_qname=?"
            qname_params = (qn,)
        else:
            qname_clause = "(e.from_qname=? OR e.to_qname=?)"
            qname_params = (qn, qn)
        rows = db.execute(
            """
            SELECT e.from_qname,e.to_qname,e.rel_type,e.source_file,e.line,e.properties_json,
                   nf.label from_label,nf.name from_name,nt.label to_label,nt.name to_name
            FROM edges e
            LEFT JOIN nodes nf ON nf.qualified_name=e.from_qname
            LEFT JOIN nodes nt ON nt.qualified_name=e.to_qname
                        WHERE {qname_clause}
                            AND e.rel_type NOT IN ('CONTAINS','BELONGS_TO')
                            AND COALESCE(nf.label, '') NOT IN ({hidden})
                            AND COALESCE(nt.label, '') NOT IN ({hidden})
            ORDER BY e.line LIMIT 240
            """.format(qname_clause=qname_clause, hidden=','.join('?' for _ in hidden_labels)),
            (*qname_params, *hidden_labels, *hidden_labels),
        ).fetchall()
        flows = []
        for row in rows:
            item = flow_dict(row, qn)
            if not item["visible"]:
                continue
            item_type = item["flow_type"].upper()
            if requested_types and item_type not in requested_types and not (item_type == "REMOTE_READS" and "READS" in requested_types):
                continue
            item["from_name"] = table_display_name(db, item["from_qname"], item.get("from_name") or item["from_qname"])
            item["to_name"] = table_display_name(db, item["to_qname"], item.get("to_name") or item["to_qname"])
            flows.append(item)
        return jsonify({"node": api_node(enrich_table_node(db, node)) if node else None, "flows": flows, "contract": {"nodes": NODE_STYLES, "edges": EDGE_STYLES}})


@app.get("/api/search")
def search():
    q = request.args.get("q", "")
    label = request.args.get("label", "")
    schema = request.args.get("schema", "")
    like = f"%{q.upper()}%"
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error())
        visible_labels = tuple(label for label, style in NODE_STYLES.items() if style["visible"])
        label_clause = "AND label=?" if label else ""
        params = (like, like, *visible_labels, label) if label else (like, like, *visible_labels)
        rows = db.execute(
            f"""
            SELECT label, qualified_name, name
            FROM nodes
            WHERE (upper(qualified_name) LIKE ? OR upper(name) LIKE ?)
              AND label IN ({','.join('?' for _ in visible_labels)}) {label_clause}
            ORDER BY CASE label WHEN 'Table' THEN 0 ELSE 1 END, name
            LIMIT 80
            """,
            params,
        ).fetchall()
        items = [enrich_table_node(db, r) for r in rows]
        if schema:
            prefix = f":{schema.upper()}."
            items = [item for item in items if prefix in item["qualified_name"].upper()]
        return jsonify(items)


@app.get("/api/lineage")
def lineage():
    qn = request.args.get("qname", "")
    with conn() as db:
        if not db_ready(db):
            return jsonify(
                {
                    "node": None,
                    "upstream": [],
                    "downstream": [],
                    "evidence": [db_error()["error"]],
                }
            )
        node = db.execute(
            "SELECT label, qualified_name, name FROM nodes WHERE qualified_name=?",
            (qn,),
        ).fetchone()
        upstream = db.execute(
            """
            SELECT e.rel_type,e.source_file,e.line,e.properties_json,n.qualified_name other_qname,n.name other_name
            FROM edges e JOIN nodes n ON n.qualified_name=e.from_qname
            WHERE e.to_qname=? ORDER BY e.line LIMIT 160
            """,
            (qn,),
        ).fetchall()
        downstream = db.execute(
            """
            SELECT e.rel_type,e.source_file,e.line,e.properties_json,n.qualified_name other_qname,n.name other_name
            FROM edges e JOIN nodes n ON n.qualified_name=e.to_qname
            WHERE e.from_qname=? ORDER BY e.line LIMIT 160
            """,
            (qn,),
        ).fetchall()
        ups = [edge_dict(r) for r in upstream]
        dns = [edge_dict(r) for r in downstream]
        evidence = [
            snippet(e["source_file"], int(e["line"]))
            for e in ups + dns
            if e.get("source_file") and e.get("line")
        ]
        return jsonify(
            {
                "node": dict(node) if node else None,
                "upstream": ups,
                "downstream": dns,
                "evidence": [x for x in evidence if x][:10],
            }
        )


def edge_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        props = json.loads(data.pop("properties_json") or "{}")
    except Exception:
        props = {}
    data["expression"] = props.get("expression", "")
    data["operation"] = props.get("operation", "")
    data["columns"] = props.get("columns", [])
    data["confidence"] = props.get("confidence", "")
    return data

def table_impact_data(db: sqlite3.Connection, qn: str) -> dict:
    rows = db.execute(
        """
        SELECT e.rel_type,e.source_file,e.line,e.properties_json,n.qualified_name actor_qname,n.name actor_name,n.label actor_label
        FROM edges e
        LEFT JOIN nodes n ON n.qualified_name=e.from_qname
        WHERE e.to_qname=? AND e.rel_type IN ('READS_FROM','INSERTS_INTO','UPDATES','DELETES_FROM','MERGES_INTO')
        ORDER BY e.line
        """,
        (qn,),
    ).fetchall()
    buckets: dict[str, dict] = {}
    rel_key = {
        "READS_FROM": "reads",
        "INSERTS_INTO": "inserts",
        "UPDATES": "updates",
        "DELETES_FROM": "deletes",
        "MERGES_INTO": "merges",
    }
    for row in rows:
        props = json.loads(row["properties_json"] or "{}")
        columns = props.get("columns") or (["*"] if row["rel_type"] == "DELETES_FROM" else [])
        for col in columns:
            item = buckets.setdefault(col, {"name": col, "reads": [], "inserts": [], "updates": [], "deletes": [], "merges": []})
            item[rel_key[row["rel_type"]]].append(
                {
                    "node": row["actor_name"] or row["actor_qname"],
                    "qname": row["actor_qname"],
                    "label": row["actor_label"],
                    "line": row["line"],
                    "source_file": row["source_file"],
                }
            )
    return {"table": qn, "columns": [buckets[k] for k in sorted(buckets)]}


def flow_dict(row: sqlite3.Row, selected_qn: str) -> dict:
    data = dict(row)
    try:
        props = json.loads(data.pop("properties_json") or "{}")
    except Exception:
        props = {}
    direction = "out" if data["from_qname"] == selected_qn else "in"
    rel = data["rel_type"]
    ftype = flow_type(rel, props)
    style = EDGE_STYLES.get(ftype, {"color": "#94a3b8", "visible": True})
    data.update(
        {
            "direction": direction,
            "flow_type": ftype,
            "visible": bool(style.get("visible", True)),
            "style": style,
            "operation": props.get("operation", ""),
            "expression": props.get("expression", ""),
            "columns": props.get("columns", []),
            "confidence": props.get("confidence", ""),
            "code": (
                snippet(data.get("source_file") or "", int(data["line"]))
                if data.get("line")
                else ""
            ),
        }
    )
    return data


def flow_type(rel: str, props: dict) -> str:
    if rel in {"WRITES_TO", "INSERTS_INTO", "UPDATES", "DELETES_FROM", "MERGES_INTO"}:
        return "WRITES"
    if rel in {"READS_FROM", "READS_COLUMN"}:
        return "REMOTE_READS" if "@" in json.dumps(props).upper() else "READS"
    if rel in {"DERIVES_FROM", "POPULATES"}:
        return "DERIVES"
    if rel == "CALLS":
        return "CALLS"
    if rel == "TRIGGERS":
        return "TRIGGERS"
    if rel in {"USES_SEQUENCE", "USES"}:
        return "USES"
    return rel

def api_node(row: dict) -> dict:
    label = row.get("label") or "Unknown"
    fallback = {"kind": "unknown", "icon": "box", "className": "unknown", "visible": True}
    return {**row, "style": NODE_STYLES.get(label, fallback)}


def split_table_qname(qn: str) -> tuple[str, str, str]:
    if not qn.startswith("Table:"):
        return "", "", ""
    parts = qn.split(":", 2)
    if len(parts) != 3:
        return "", "", ""
    db_name, full = parts[1], parts[2]
    schema_name, _, table_name = full.partition(".")
    if not table_name:
        return db_name, "", schema_name
    return db_name, schema_name, table_name


def snippet(path: str, line: int) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    lines = read_source_text(p).splitlines()
    start = max(0, line - 4)
    end = min(len(lines), line + 5)
    return "\n".join(f"{i + 1:5d}: {lines[i]}" for i in range(start, end))

def source_object_snippet(path: str, line: int, name: str, label: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    lines = read_source_text(p).splitlines()
    if label not in {"Procedure", "SQLFunction", "Function", "Trigger"}:
        return snippet(path, line)
    start = max(0, line - 1)
    end_re = re.compile(rf"^\s*END\s+{re.escape(name)}\s*;", re.IGNORECASE)
    anon_end_re = re.compile(r"^\s*END\s*;", re.IGNORECASE)
    for i in range(start + 1, len(lines)):
        if end_re.search(lines[i]) or (label == "Trigger" and anon_end_re.search(lines[i])):
            return "\n".join(f"{n + 1:5d}: {lines[n]}" for n in range(start, i + 1))
    return "\n".join(f"{n + 1:5d}: {lines[n]}" for n in range(start, len(lines)))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--host", default=APP_CONFIG["host"])
    parser.add_argument("--port", type=int, default=int(APP_CONFIG["port"]))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.db:
        DB_PATH = resolve_path(args.db, Path.cwd())
    app.run(host=args.host, port=args.port, debug=False)
