"""Flask web app for local SQLite code lineage.

Run:
    .venv/bin/python api/app.py --db code_map.db --port 8000
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template, request, send_from_directory
from common.config import load_app_config, resolve_path
from common.source_text import decode_source_bytes
from db.writer import ensure_db_schema

APP_CONFIG_PATH = resolve_path(
    os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"), ROOT
)
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
            environ["PATH_INFO"] = path[len(self.prefix) :] or "/"
        return self.wrapped(environ, start_response)


app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)

NODE_STYLES = {
    "Table": {
        "kind": "table",
        "icon": "database",
        "className": "table",
        "visible": True,
    },
    "Procedure": {"kind": "code", "icon": "code", "className": "code", "visible": True},
    "SQLFunction": {
        "kind": "code",
        "icon": "box",
        "className": "code",
        "visible": True,
    },
    "Function": {"kind": "code", "icon": "box", "className": "code", "visible": True},
    "Trigger": {
        "kind": "trigger",
        "icon": "git-branch",
        "className": "trigger",
        "visible": True,
    },
    "PLSQLPackage": {
        "kind": "package",
        "icon": "file-code",
        "className": "file",
        "visible": True,
    },
    "Sequence": {
        "kind": "sequence",
        "icon": "hash",
        "className": "sequence",
        "visible": True,
    },
    "SourceFile": {
        "kind": "source-file",
        "icon": "file-code",
        "className": "source-file",
        "visible": True,
    },
    "Repository": {
        "kind": "repository",
        "icon": "code",
        "className": "repository",
        "visible": True,
    },
    "Application": {
        "kind": "application",
        "icon": "box",
        "className": "application",
        "visible": True,
    },
    "Cursor": {
        "kind": "detail",
        "icon": "file-code",
        "className": "file",
        "visible": False,
    },
    "Column": {
        "kind": "detail",
        "icon": "list",
        "className": "detail",
        "visible": False,
    },
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


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        ensure_db_schema(db)
        yield db
    finally:
        db.close()


def db_ready(db: sqlite3.Connection) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
    )


def db_error() -> dict[str, Any]:
    return {
        "error": {
            "code": "database_not_ready",
            "message": f"Database chưa có dữ liệu. Chạy: python -m extractors.run_all --config {APP_CONFIG_PATH}",
        }
    }

def api_error(code: str, message: str, status: int, **details: Any):
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return jsonify(payload), status

@app.errorhandler(404)
def route_not_found(_error):
    return api_error("not_found", "API route not found", 404)

@app.errorhandler(405)
def method_not_allowed(_error):
    return api_error("method_not_allowed", "HTTP method not allowed", 405)


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
        response = send_from_directory(WEB_DIST, "index.html")
    else:
        response = render_template("index.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/app-config.js")
def app_config_js():
    return (
        "window.CODE_MAP_CONFIG = " + json.dumps({"urlPrefix": URL_PREFIX}) + ";",
        200,
        {"Content-Type": "application/javascript"},
    )


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
            SELECT label, qualified_name, name, properties_json
            FROM nodes
            WHERE label IN ({','.join('?' for _ in labels)})
                            AND NOT (label='Table' AND upper(name) IN ('NEW','OLD','R','SRC'))
            ORDER BY CASE label
                WHEN 'PLSQLPackage' THEN 0
                WHEN 'Procedure' THEN 1
                WHEN 'SQLFunction' THEN 2
                WHEN 'SourceFile' THEN 3
                WHEN 'Repository' THEN 4
                WHEN 'Application' THEN 5
                WHEN 'Table' THEN 6
                ELSE 7
            END, name, qualified_name
            """,
            labels,
        ).fetchall()
        return jsonify([api_node(enrich_node(db, row)) for row in rows])


@app.get("/api/schemas")
def schemas():
    with conn() as db:
        if not db_ready(db):
            return jsonify([])
        rows = db.execute(
            "SELECT qualified_name, properties_json FROM nodes"
        ).fetchall()
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
        table = db.execute(
            TABLE_DEF_COLUMNS, (db_name, schema_name, table_name)
        ).fetchone()
        rows = db.execute(
            COLUMN_DEF_COLUMNS,
            (db_name, schema_name, table_name),
        ).fetchall()
        return jsonify(
            {"table": dict(table) if table else {}, "columns": [dict(r) for r in rows]}
        )


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


def enrich_node(db: sqlite3.Connection, row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    try:
        properties = json.loads(data.pop("properties_json", None) or "{}")
    except (TypeError, ValueError):
        properties = {}
    if data.get("label") == "Table":
        data["name"] = table_display_name(
            db, data.get("qualified_name", ""), data.get("name", "")
        )
    data["display"] = node_display(data, properties)
    return data

def node_display(node: dict, properties: dict) -> dict[str, str | None]:
    label = node.get("label") or "Unknown"
    name = str(node.get("name") or "")
    qname = str(node.get("qualified_name") or "")
    repository = _optional_text(properties.get("repository"))
    project = _optional_text(properties.get("project") or properties.get("project_name"))
    namespace = _optional_text(properties.get("namespace"))
    source_path = _optional_text(properties.get("source_path"))
    if label == "SourceFile":
        return {
            "title": Path(source_path or name or "Source file").name,
            "subtitle": source_path,
            "scope": " / ".join(value for value in (repository, project) if value) or None,
        }
    if label == "Repository":
        return {"title": name or "Repository", "subtitle": namespace, "scope": project}
    if label == "Application":
        return {
            "title": project or name or "Application",
            "subtitle": repository,
            "scope": "Project fallback",
        }
    if label == "Table":
        db_name, schema_name, _ = split_table_qname(qname)
        return {
            "title": name or qname,
            "subtitle": schema_name or None,
            "scope": db_name or None,
        }
    return {
        "title": name or qname,
        "subtitle": _optional_text(properties.get("schema") or properties.get("package")),
        "scope": repository or _optional_text(properties.get("db_name")),
    }

def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


@app.get("/api/node-detail")
def node_detail():
    qn = request.args.get("qname", "").strip()
    if not qn:
        return api_error("invalid_request", "qname is required", 400)
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error()), 503
        row = db.execute(
            "SELECT label, qualified_name, name, properties_json FROM nodes WHERE qualified_name=?",
            (qn,),
        ).fetchone()
        if not row:
            return api_error("node_not_found", "Node not found", 404, qname=qn)
        raw = dict(row)
        try:
            props = json.loads(raw.get("properties_json") or "{}")
        except (TypeError, ValueError):
            props = {}
        sources = node_sources(db, qn, props)
        relation_count = db.execute(
            "SELECT COUNT(*) FROM edges WHERE from_qname=? OR to_qname=?", (qn, qn)
        ).fetchone()[0]
        occurrence_count = db.execute(
            "SELECT COUNT(*) FROM edge_facts WHERE from_qname=? OR to_qname=?", (qn, qn)
        ).fetchone()[0]
        warnings = node_warnings(db, qn, props)
        table = {}
        columns = []
        if raw["label"] == "Table":
            db_name, schema_name, table_name = split_table_qname(qn)
            table_row = db.execute(
                TABLE_DEF_COLUMNS, (db_name, schema_name, table_name)
            ).fetchone()
            table = dict(table_row) if table_row else {}
            columns = [
                dict(item)
                for item in db.execute(
                    COLUMN_DEF_COLUMNS, (db_name, schema_name, table_name)
                ).fetchall()
            ]
        return jsonify(
            {
                "node": api_node(enrich_node(db, row)),
                "properties": readable_node_properties(
                    raw["label"], props, sources, raw.get("name")
                ),
                "sources": sources,
                "counts": {
                    "sources": len(sources),
                    "relations": relation_count,
                    "occurrences": occurrence_count,
                },
                "warnings": warnings,
                "table": table,
                "columns": columns,
                "impact": table_impact_data(db, qn) if raw["label"] == "Table" else {"columns": []},
                "code": None,
            }
        )

def node_sources(db: sqlite3.Connection, qname: str, fallback: dict) -> list[dict]:
    rows = db.execute(
        """
        SELECT DISTINCT f.source_id, f.relative_path, f.project_name,
               f.handler_name, s.source_root
        FROM node_facts n
        JOIN extraction_files f ON f.id=n.file_id
        LEFT JOIN extraction_sources s ON s.source_id=f.source_id
        WHERE n.qname=? AND f.status<>'deleted'
        ORDER BY f.source_id, f.relative_path, f.handler_name
        """,
        (qname,),
    ).fetchall()
    sources = [source_item(*row) for row in rows]
    if sources:
        return sources
    source_id = _optional_text(fallback.get("source_id"))
    source_path = _optional_text(fallback.get("source_path") or fallback.get("source_file"))
    if not source_id or not source_path:
        return []
    root_row = db.execute(
        "SELECT source_root FROM extraction_sources WHERE source_id=?", (source_id,)
    ).fetchone()
    return [
        source_item(
            source_id,
            source_path,
            _optional_text(fallback.get("project")) or "",
            _optional_text(fallback.get("extractor_name")) or "",
            root_row[0] if root_row else None,
        )
    ]

def source_item(
    source_id: str, source_path: str, project: str, extractor: str, source_root: str | None
) -> dict:
    locator = resolve_source_locator(source_root, source_path)
    return {
        "source_id": source_id,
        "source_path": safe_relative_source_path(source_path),
        "project": project or None,
        "extractor": extractor or None,
        "language": source_language(source_path),
        "availability": locator[0],
    }

def node_warnings(db: sqlite3.Connection, qname: str, properties: dict) -> list[dict]:
    unresolved = bool(properties.get("unresolved_sql"))
    if not unresolved:
        for row in db.execute("SELECT properties_json FROM node_facts WHERE qname=?", (qname,)):
            try:
                unresolved = unresolved or bool(json.loads(row[0] or "{}").get("unresolved_sql"))
            except (TypeError, ValueError):
                continue
    return (
        [{"code": "dynamic_sql_unresolved", "message": "Dynamic SQL unresolved; graph targets may be incomplete."}]
        if unresolved
        else []
    )

def readable_node_properties(
    label: str, properties: dict, sources: list[dict], object_name: str | None = None
) -> dict:
    primary_source = sources[0] if sources else {}
    values = {
        "repository": _optional_text(properties.get("repository")),
        "project": _optional_text(
            properties.get("project")
            or properties.get("project_name")
            or primary_source.get("project")
        ),
    }
    if label == "SourceFile":
        source = primary_source
        values.update(
            {
                "source_id": source.get("source_id") or _optional_text(properties.get("source_id")),
                "source_path": source.get("source_path") or _optional_text(properties.get("source_path")),
                "language": source.get("language"),
                "extractor": source.get("extractor") or _optional_text(properties.get("extractor_name")),
                "source_availability": source.get("availability") or "invalid_source",
            }
        )
    elif label == "Repository":
        values["class_name"] = _optional_text(object_name)
        values["namespace"] = _optional_text(properties.get("namespace"))
        values["extractors"] = sorted({item["extractor"] for item in sources if item.get("extractor")})
    elif label == "Application":
        values["project_fallback"] = True
        values["extractors"] = sorted({item["extractor"] for item in sources if item.get("extractor")})
    else:
        values.update(
            {
                "schema": _optional_text(properties.get("schema")),
                "db_name": _optional_text(properties.get("db_name")),
            }
        )
    return {key: value for key, value in values.items() if value is not None}


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
            "SELECT label, qualified_name, name, properties_json FROM nodes WHERE qualified_name=?",
            (qn,),
        ).fetchone()
        if not node:
            return api_error("node_not_found", "Node not found", 404, qname=qn)
        flows = query_flow_rows(db, qn, direction, requested_types)
        return jsonify(
            {
                "node": api_node(enrich_node(db, node)),
                "flows": flows,
                "truncated": False,
                "contract": {"nodes": NODE_STYLES, "edges": EDGE_STYLES},
            }
        )


@app.get("/api/flow-all")
def flow_all():
    qn = request.args.get("qname", "")
    direction = request.args.get("direction", "both").lower()
    requested_types = {
        item.strip().upper()
        for item in request.args.get("types", "").split(",")
        if item.strip()
    }
    try:
        max_nodes = max(1, min(int(request.args.get("max_nodes", 500) or 500), 1000))
    except (TypeError, ValueError):
        return api_error("invalid_request", "max_nodes must be an integer", 400)
    with conn() as db:
        if not db_ready(db):
            return jsonify({"node": None, "flows": [], "evidence": []})
        node = db.execute(
            "SELECT label, qualified_name, name, properties_json FROM nodes WHERE qualified_name=?",
            (qn,),
        ).fetchone()
        if not node:
            return api_error("node_not_found", "Node not found", 404, qname=qn)
        pending = [qn]
        visited = set()
        flows_by_id = {}
        while pending and len(visited) < max_nodes:
            current_qn = pending.pop(0)
            if current_qn in visited:
                continue
            visited.add(current_qn)
            rows = query_flow_rows(db, current_qn, direction, requested_types)
            for item in rows:
                flows_by_id[edge_key(item)] = item
                if direction == "in":
                    next_qn = item["from_qname"]
                elif direction == "out":
                    next_qn = item["to_qname"]
                else:
                    next_qn = (
                        item["to_qname"]
                        if item["from_qname"] == current_qn
                        else item["from_qname"]
                    )
                if next_qn and next_qn not in visited:
                    pending.append(next_qn)
        return jsonify(
            {
                "node": api_node(enrich_node(db, node)),
                "flows": list(flows_by_id.values()),
                "truncated": bool(pending),
                "max_nodes": max_nodes,
                "contract": {"nodes": NODE_STYLES, "edges": EDGE_STYLES},
            }
        )


@app.get("/api/search")
def search():
    q = request.args.get("q", "")
    label = request.args.get("label", "")
    schema = request.args.get("schema", "")
    like = f"%{q.upper()}%"
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error())
        visible_labels = tuple(
            label for label, style in NODE_STYLES.items() if style["visible"]
        )
        label_clause = "AND label=?" if label else ""
        rows = db.execute(
            f"""
            SELECT label, qualified_name, name, properties_json
            FROM nodes
            WHERE (upper(qualified_name) LIKE ? OR upper(name) LIKE ?
                   OR upper(COALESCE(properties_json,'')) LIKE ?)
              AND label IN ({','.join('?' for _ in visible_labels)}) {label_clause}
            ORDER BY CASE label
                WHEN 'SourceFile' THEN 0 WHEN 'Repository' THEN 1
                WHEN 'Application' THEN 2 WHEN 'Table' THEN 3 ELSE 4
            END, name, qualified_name
            LIMIT 80
            """,
            ((like, like, like, *visible_labels, label) if label else (like, like, like, *visible_labels)),
        ).fetchall()
        items = [api_node(enrich_node(db, row)) for row in rows]
        if schema:
            prefix = f":{schema.upper()}."
            items = [item for item in items if prefix in item["qualified_name"].upper()]
        return jsonify(items)


@app.get("/api/edge-evidence")
def edge_evidence():
    relation = relation_args()
    if isinstance(relation, tuple) and len(relation) == 2:
        return relation
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 100))
        offset = decode_cursor(request.args.get("cursor", ""))
    except (TypeError, ValueError):
        return api_error("invalid_pagination", "cursor or limit is invalid", 400)
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error()), 503
        page = relation_evidence_page(db, *relation, offset=offset, limit=limit)
        if page["evidence_count"] == 0 and not relation_exists(db, *relation):
            return api_error("relation_not_found", "Relation not found", 404)
        return jsonify(page)

@app.get("/api/snippet")
def occurrence_snippet():
    relation = relation_args()
    if isinstance(relation, tuple) and len(relation) == 2:
        return relation
    occurrence_id = request.args.get("occurrence_id", "").strip()
    if not re.fullmatch(r"occ_[0-9a-f]{32}", occurrence_id):
        return api_error("invalid_occurrence", "occurrence_id is invalid", 400)
    with conn() as db:
        if not db_ready(db):
            return jsonify(db_error()), 503
        occurrence = find_occurrence(db, *relation, occurrence_id)
        if not occurrence:
            return api_error("occurrence_not_found", "Occurrence not found for relation", 404)
        status, path = resolve_source_locator(
            occurrence.get("source_root"), occurrence.get("source_path")
        )
        if status != "available" or path is None:
            return jsonify(snippet_state(status, occurrence))
        return jsonify(read_bounded_snippet(path, occurrence))

def relation_args():
    relation = tuple(request.args.get(name, "").strip() for name in ("from_qname", "to_qname", "rel_type"))
    if not all(relation):
        return api_error("invalid_relation", "from_qname, to_qname and rel_type are required", 400)
    return relation

def relation_exists(db: sqlite3.Connection, from_qname: str, to_qname: str, rel_type: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM edges WHERE from_qname=? AND to_qname=? AND rel_type=?",
        (from_qname, to_qname, rel_type),
    ).fetchone())

def relation_evidence_page(
    db: sqlite3.Connection, from_qname: str, to_qname: str, rel_type: str, *, offset: int, limit: int
) -> dict:
    total = db.execute(
        """SELECT COUNT(*) FROM edge_facts e JOIN extraction_files f ON f.id=e.file_id
        WHERE e.from_qname=? AND e.to_qname=? AND e.rel_type=? AND f.status<>'deleted'""",
        (from_qname, to_qname, rel_type),
    ).fetchone()[0]
    rows = db.execute(
        """SELECT e.fact_key,e.source_path,e.line,e.properties_json,f.source_id,f.handler_name
        FROM edge_facts e JOIN extraction_files f ON f.id=e.file_id
        WHERE e.from_qname=? AND e.to_qname=? AND e.rel_type=? AND f.status<>'deleted'
        ORDER BY e.priority DESC,f.source_id ASC,e.source_path ASC,e.line ASC,e.fact_key ASC
        LIMIT ? OFFSET ?""",
        (from_qname, to_qname, rel_type, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        try:
            properties = json.loads(row["properties_json"] or "{}")
        except (TypeError, ValueError):
            properties = {}
        properties.update({
            "source_id": row["source_id"], "source_path": row["source_path"] or "",
            "line": row["line"], "extractor_name": properties.get("extractor_name") or row["handler_name"],
        })
        items.append(normalize_evidence(properties, fact_key=row["fact_key"]))
    next_offset = offset + len(items)
    return {
        "evidence": items, "evidence_count": total, "evidence_truncated": next_offset < total,
        "next_cursor": encode_cursor(next_offset) if next_offset < total else None,
    }

def find_occurrence(
    db: sqlite3.Connection, from_qname: str, to_qname: str, rel_type: str, occurrence_id: str
) -> dict | None:
    rows = db.execute(
        """SELECT e.fact_key,e.source_path,e.line,e.properties_json,
        f.source_id,f.handler_name,s.source_root
        FROM edge_facts e JOIN extraction_files f ON f.id=e.file_id
        LEFT JOIN extraction_sources s ON s.source_id=f.source_id
        WHERE e.from_qname=? AND e.to_qname=? AND e.rel_type=? AND f.status<>'deleted'
        ORDER BY e.priority DESC,f.source_id ASC,e.source_path ASC,e.line ASC,e.fact_key ASC""",
        (from_qname, to_qname, rel_type),
    ).fetchall()
    for row in rows:
        candidate = stable_occurrence_id(row["source_id"], row["source_path"] or "", row["fact_key"])
        if candidate != occurrence_id:
            continue
        try:
            properties = json.loads(row["properties_json"] or "{}")
        except (TypeError, ValueError):
            properties = {}
        properties.update({
            "occurrence_id": candidate, "source_id": row["source_id"],
            "source_path": row["source_path"] or "", "line": row["line"],
            "source_root": row["source_root"],
            "extractor_name": properties.get("extractor_name") or row["handler_name"],
        })
        return properties
    return None

def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")

def decode_cursor(cursor: str) -> int:
    if not cursor:
        return 0
    if len(cursor) > 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ValueError("invalid cursor")
    offset = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii"))
    if offset < 0:
        raise ValueError("negative cursor")
    return offset

def source_language(source_path: Any) -> str | None:
    return {
        ".cs": "csharp", ".xml": "xml", ".sql": "sql", ".pks": "plsql",
        ".pkb": "plsql", ".pls": "plsql", ".csv": "csv",
    }.get(PurePosixPath(str(source_path or "")).suffix.lower())

def safe_relative_source_path(source_path: Any) -> str | None:
    path = PurePosixPath(str(source_path or ""))
    if not source_path or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()

def resolve_source_locator(source_root: str | None, source_path: str | None) -> tuple[str, Path | None]:
    if not source_root or not source_path:
        return "invalid_source", None
    relative = PurePosixPath(source_path)
    if relative.is_absolute() or ".." in relative.parts:
        return "invalid_source", None
    try:
        root = Path(source_root).resolve(strict=True)
        candidate = (root / Path(*relative.parts)).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return "invalid_source", None
    if not candidate.exists() or not candidate.is_file():
        return "not_found", None
    if not os.access(candidate, os.R_OK):
        return "unreadable", None
    return "available", candidate

def snippet_state(status: str, occurrence: dict) -> dict:
    line = positive_line(occurrence.get("line"))
    return {
        "status": status,
        "source_path": safe_relative_source_path(occurrence.get("source_path")),
        "focus_line": line, "start_line": None, "end_line": None,
        "language": source_language(str(occurrence.get("source_path") or "")), "text": None,
    }

def read_bounded_snippet(path: Path, occurrence: dict) -> dict:
    focus = positive_line(occurrence.get("line"))
    try:
        text = decode_source_bytes(path.read_bytes()[: 8 * 1024 * 1024])
    except OSError:
        return snippet_state("unreadable", occurrence)
    lines = text.splitlines()
    if not lines:
        return snippet_state("unreadable", occurrence)
    focus = min(focus, len(lines))
    start = max(1, focus - 6)
    end = min(len(lines), focus + 12, start + 24)
    selected = "\n".join(lines[start - 1 : end])
    encoded = selected.encode("utf-8")
    if len(encoded) > 64 * 1024:
        selected = decode_source_bytes(encoded[: 64 * 1024])
    return {
        "status": "available", "source_path": safe_relative_source_path(occurrence.get("source_path")),
        "focus_line": focus, "start_line": start, "end_line": end,
        "language": source_language(str(occurrence.get("source_path") or "")), "text": selected,
    }

def positive_line(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1

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
            SELECT e.from_qname,e.to_qname,e.rel_type,e.source_file,e.line,e.properties_json,
                   n.qualified_name other_qname,n.name other_name
            FROM edges e JOIN nodes n ON n.qualified_name=e.from_qname
            WHERE e.to_qname=? ORDER BY e.line LIMIT 160
            """,
            (qn,),
        ).fetchall()
        downstream = db.execute(
            """
            SELECT e.from_qname,e.to_qname,e.rel_type,e.source_file,e.line,e.properties_json,
                   n.qualified_name other_qname,n.name other_name
            FROM edges e JOIN nodes n ON n.qualified_name=e.to_qname
            WHERE e.from_qname=? ORDER BY e.line LIMIT 160
            """,
            (qn,),
        ).fetchall()
        if not node:
            return api_error("node_not_found", "Node not found", 404, qname=qn)
        return jsonify(
            {
                "node": api_node(enrich_node(db, node)),
                "upstream": [edge_dict(db, row) for row in upstream],
                "downstream": [edge_dict(db, row) for row in downstream],
            }
        )


def edge_dict(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        props = json.loads(data.pop("properties_json") or "{}")
    except Exception:
        props = {}
    data.update(relation_properties(data.get("rel_type", ""), props))
    data.update(
        relation_evidence_page(
            db,
            data.get("from_qname", ""),
            data.get("to_qname", ""),
            data.get("rel_type", ""),
            offset=0,
            limit=100,
        )
    )
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
        columns = props.get("columns") or (
            ["*"] if row["rel_type"] == "DELETES_FROM" else []
        )
        for col in columns:
            item = buckets.setdefault(
                col,
                {
                    "name": col,
                    "reads": [],
                    "inserts": [],
                    "updates": [],
                    "deletes": [],
                    "merges": [],
                },
            )
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


def query_flow_rows(
    db: sqlite3.Connection, qn: str, direction: str, requested_types: set[str]
) -> list[dict]:
    hidden_labels = tuple(
        label for label, style in NODE_STYLES.items() if not style["visible"]
    )
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
               nf.label from_label,nf.name from_name,nf.properties_json from_properties_json,
               nt.label to_label,nt.name to_name,nt.properties_json to_properties_json
        FROM edges e
        LEFT JOIN nodes nf ON nf.qualified_name=e.from_qname
        LEFT JOIN nodes nt ON nt.qualified_name=e.to_qname
        WHERE {qname_clause}
          AND e.rel_type NOT IN ('CONTAINS','BELONGS_TO')
          AND COALESCE(nf.label, '') NOT IN ({hidden})
          AND COALESCE(nt.label, '') NOT IN ({hidden})
        ORDER BY e.line LIMIT 240
        """.format(
            qname_clause=qname_clause, hidden=",".join("?" for _ in hidden_labels)
        ),
        (*qname_params, *hidden_labels, *hidden_labels),
    ).fetchall()
    flows = []
    for row in rows:
        item = flow_dict(row, qn)
        if not item["visible"]:
            continue
        item_type = item["flow_type"].upper()
        if (
            requested_types
            and item_type not in requested_types
            and not (item_type == "REMOTE_READS" and "READS" in requested_types)
        ):
            continue
        from_node = enrich_node(
            db,
            {
                "label": item.get("from_label"),
                "qualified_name": item["from_qname"],
                "name": item.get("from_name"),
                "properties_json": item.pop("from_properties_json", "{}"),
            },
        )
        to_node = enrich_node(
            db,
            {
                "label": item.get("to_label"),
                "qualified_name": item["to_qname"],
                "name": item.get("to_name"),
                "properties_json": item.pop("to_properties_json", "{}"),
            },
        )
        item.update(
            {
                "from_name": from_node["display"]["title"],
                "from_display": from_node["display"],
                "to_name": to_node["display"]["title"],
                "to_display": to_node["display"],
            }
        )
        item.update(
            relation_evidence_page(
                db, item["from_qname"], item["to_qname"], item["rel_type"], offset=0, limit=100
            )
        )
        flows.append(item)
    return flows


def edge_key(item: dict) -> str:
    return "|".join(
        [
            item.get("from_qname") or "",
            item.get("to_qname") or "",
            item.get("rel_type") or "",
            str(item.get("line") or ""),
            item.get("source_file") or "",
        ]
    )


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
            **relation_properties(rel, props),
        }
    )
    return data


def relation_properties(rel_type: str, properties: dict) -> dict:
    evidence = [normalize_evidence(item) for item in properties.get("evidence") or []]
    return {
        "rel_type": rel_type,
        "flow_type": flow_type(rel_type, properties),
        "operation": _optional_text(properties.get("operation")) or relation_operation(rel_type),
        "expression": _optional_text(properties.get("expression")),
        "columns": sorted({str(item) for item in properties.get("columns") or []}),
        "confidence": _optional_text(properties.get("confidence")),
        "evidence": evidence,
        "evidence_count": int(properties.get("evidence_count") or len(evidence)),
        "evidence_truncated": bool(properties.get("evidence_truncated")),
    }

def normalize_evidence(properties: dict, *, fact_key: str = "") -> dict:
    source_id = str(properties.get("source_id") or "")
    raw_source_path = str(properties.get("source_path") or "")
    source_path = safe_relative_source_path(raw_source_path)
    occurrence_id = stable_occurrence_id(source_id, raw_source_path, fact_key or _json_stable(properties))
    keys = (
        "line", "operation", "query_id", "mapper_tag", "call_type", "expression",
        "columns", "confidence", "extractor_name", "language",
    )
    item = {key: properties[key] for key in keys if properties.get(key) not in (None, "", [])}
    item.update({"occurrence_id": occurrence_id, "source_id": source_id, "source_path": source_path})
    item.setdefault("operation", None)
    item.setdefault("columns", [])
    item.setdefault("language", source_language(source_path))
    return item

def stable_occurrence_id(source_id: str, source_path: str, fact_key: str) -> str:
    digest = hashlib.sha256(
        "\0".join((source_id, PurePosixPath(source_path).as_posix(), fact_key)).encode("utf-8")
    ).hexdigest()
    return f"occ_{digest[:32]}"

def relation_operation(rel_type: str) -> str:
    return {
        "READS_FROM": "SELECT", "READS_COLUMN": "SELECT", "INSERTS_INTO": "INSERT",
        "UPDATES": "UPDATE", "DELETES_FROM": "DELETE", "MERGES_INTO": "MERGE", "CALLS": "CALL",
    }.get(rel_type, rel_type)

def _json_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

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
    fallback = {
        "kind": "unknown",
        "icon": "box",
        "className": "unknown",
        "visible": True,
    }
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
