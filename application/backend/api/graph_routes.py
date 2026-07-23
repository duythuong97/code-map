"""Flask blueprint backed by the CSV graph database."""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from flask import Blueprint, current_app, jsonify, request

ROOT = Path(__file__).resolve().parents[3]

def _workspace_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()

bp = Blueprint("graph_api", __name__, url_prefix=os.environ.get("CODE_MAP_GRAPH_API_PREFIX", "/api/graph"))
DB_PATH = _workspace_path(os.environ.get("CODE_MAP_GRAPH_DB", "data/code-flow-demo.sqlite"))
SAFE_SOURCE_ROOTS = tuple(_workspace_path(value) for value in os.environ.get("CODE_MAP_SAFE_SOURCE_ROOTS", str(ROOT / "demo-sources")).split(os.pathsep) if value)
KNOWLEDGE_STATUSES = {"draft","pending","approved","rejected"}
KNOWLEDGE_MAX_BYTES = 16_384
SEMANTIC_MAX_BYTES = 4_096
MAX_IMPORT_BODY_BYTES = int(os.environ.get("CODE_MAP_MAX_CONTENT_LENGTH", str(16 * 1024 * 1024)))
FLOW_MODES = {"1","W","R","E"}
WRITE_TERMINALS = {"INSERTS","UPDATES","DELETES","MERGES","WRITES"}
READ_TERMINALS = {"READS","REMOTE_READS"}
FLOW_INTERMEDIATE = {"CALLS_API","STARTS","CALLS","HANDLED_BY","RESOLVES_TO","TRIGGERS"}

@contextmanager
def connection():
    try:
        db_path = current_app.config.get("CODE_MAP_GRAPH_DB_PATH", DB_PATH)
    except RuntimeError:
        db_path = DB_PATH
    db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        db.close()

def error(code: str, message: str, status: int = 400, **details: Any):
    return jsonify({"error": {"code": code, "message": message, "details": details or {}}}), status

def _json(value, fallback=None):
    try:
        return json.loads(value or "")
    except Exception:
        return {} if fallback is None else fallback

def _rows_by_ids(db, table, id_column, ids):
    ordered=[value for value in ids if value]
    if not ordered: return []
    placeholders=','.join('?'*len(ordered))
    rows=db.execute(f"SELECT * FROM {table} WHERE {id_column} IN ({placeholders})",ordered).fetchall()
    by_id={row[id_column]:dict(row) for row in rows}
    return [by_id[value] for value in ordered if value in by_id]

def _decode_path_rows(rows):
    decoded=[]
    for row in rows:
        item=dict(row)
        item["edge_path"]=_json(item.pop("edge_path_json","[]"),[])
        item["evidence_ids"]=_json(item.pop("evidence_ids_json","[]"),[])
        decoded.append(item)
    return decoded

def _enrich_path_facts(db, paths):
    edge_ids=_unique_values([edge_id for path in paths for edge_id in path.get("edge_path",[])])
    edges={row["edge_id"]:dict(row) for row in _rows_by_ids(db,"graph_edges","edge_id",edge_ids)}
    for path in paths:
        path_edges=[edges[edge_id] for edge_id in path.get("edge_path",[]) if edge_id in edges]
        terminals=[edge for edge in path_edges if edge["edge_type"] in READ_TERMINALS | WRITE_TERMINALS]
        terminal=terminals[-1] if terminals else None
        intermediates=[]
        for edge in path_edges:
            if edge["edge_type"] not in FLOW_INTERMEDIATE:
                continue
            properties=_json(edge.get("properties_json"),{})
            intermediates.append({
                "edge_id":edge["edge_id"],"edge_type":edge["edge_type"],
                "source_node_id":edge["source_node_id"],"target_node_id":edge["target_node_id"],
                "raw_operation":edge.get("raw_operation") or "","properties":properties,
            })
        path["terminal_edge_type"]=terminal["edge_type"] if terminal else ""
        path["terminal_raw_operation"]=terminal.get("raw_operation") or "" if terminal else ""
        path["intermediate_path"]=intermediates
        path["transformation_exact_available"]=bool(path["terminal_raw_operation"] or any(edge["raw_operation"] or edge["properties"] for edge in intermediates))
    return paths

def _unique_values(values):
    return [value for value in dict.fromkeys(values or []) if value]

def _edge_trace_ids(db, edge_id, edge_props=None):
    props = edge_props
    if props is None:
        edge = db.execute("SELECT properties_json FROM graph_edges WHERE edge_id=?", (edge_id,)).fetchone()
        if not edge:
            return []
        props = _json(edge["properties_json"], {})
    edge_path = props.get("edge_path") or []
    if not edge_path and props.get("path_id"):
        path_row = db.execute("SELECT edge_path_json FROM graph_paths WHERE path_id=?", (props["path_id"],)).fetchone()
        if path_row:
            edge_path = _json(path_row["edge_path_json"], [])
    return _unique_values(edge_path)

def _count_evidence_for_targets(db, target_ids):
    ids = _unique_values(target_ids)
    if not ids:
        return 0
    placeholders = ','.join('?' * len(ids))
    return db.execute(f"SELECT count(*) FROM graph_evidence WHERE target_id IN ({placeholders})", ids).fetchone()[0]

def _positive_int_arg(name: str, default: int, maximum: int) -> int | None:
    raw = request.args.get(name, str(default))
    try:
        return max(1, min(maximum, int(raw)))
    except (TypeError, ValueError):
        return None

def _requested_node_types(*names: str) -> list[str]:
    raw_values: list[str] = []
    for name in names:
        raw_values.extend(request.args.getlist(name))
    values: list[str] = []
    for raw in raw_values:
        for item in str(raw or "").split(","):
            node_type = item.strip().upper()
            if node_type and re.fullmatch(r"[A-Z0-9_]+", node_type):
                values.append(node_type)
    return list(dict.fromkeys(values))[:50]

def _semantic_tokens(value: object) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(token) > 1}

def _semantic_context(node: sqlite3.Row, localization: list[sqlite3.Row]) -> tuple[str, set[str]]:
    properties = _json(node["properties_json"], {})
    tree = properties.get("semantic_tree") or {}
    raw = " ".join(str(value or "") for value in (
        node["technical_name"], node["qualified_name"], node["default_display_name"],
        tree.get("label"), tree.get("summary"), *(row["value"] for row in localization),
    ))
    return raw.lower(), _semantic_tokens(raw)

def _business_semantic_rows(db: sqlite3.Connection, target_id: str, locale: str = "en") -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT m.target_id,m.status AS mapping_status,m.confidence,m.author_name,
               COALESCE(pref.semantic_id,en.semantic_id) AS semantic_id,
               COALESCE(pref.domain,en.domain) AS domain,
               COALESCE(pref.locale,en.locale) AS locale,
               COALESCE(pref.label,en.label) AS label,
               COALESCE(pref.definition,en.definition) AS definition
        FROM graph_business_semantic_mappings m
        LEFT JOIN graph_business_semantics pref ON pref.semantic_id=m.semantic_id AND pref.locale=?
        LEFT JOIN graph_business_semantics en ON en.semantic_id=m.semantic_id AND en.locale='en'
        WHERE m.target_id=? AND m.status='approved' AND COALESCE(pref.semantic_id,en.semantic_id) IS NOT NULL
        """,
        (locale,target_id),
    ).fetchall()
    return [dict(row) for row in rows]

def _semantic_tree_target_ids(target_id: str, tree: object) -> list[str]:
    target_ids = {target_id}
    def collect(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("ref_node_id"), str): target_ids.add(value["ref_node_id"])
            for child in value.values(): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(tree)
    return list(target_ids)[:200]

def _semantic_tree_business_semantics(db: sqlite3.Connection, target_ids: list[str], locale: str) -> dict[str, dict[str, Any]]:
    result = {}
    for semantic_target_id in target_ids:
        rows = _business_semantic_rows(db, semantic_target_id, locale)
        if rows: result[semantic_target_id] = rows[0]
    return result

def _semantic_suggestions(db: sqlite3.Connection, target_id: str, locale: str, limit: int) -> list[dict[str, Any]]:
    node = db.execute("SELECT * FROM graph_nodes WHERE node_id=?", (target_id,)).fetchone()
    if not node:
        return []
    localization = db.execute("SELECT value FROM graph_localization WHERE target_id=? AND UPPER(review_status)='APPROVED'", (target_id,)).fetchall()
    raw_context, context_tokens = _semantic_context(node, localization)
    rows = db.execute(
        """
        SELECT COALESCE(pref.semantic_id,en.semantic_id) semantic_id,
               COALESCE(pref.domain,en.domain) domain,COALESCE(pref.locale,en.locale) locale,
               COALESCE(pref.label,en.label) label,COALESCE(pref.definition,en.definition) definition,
               COALESCE(pref.aliases_json,en.aliases_json,'[]') aliases_json
        FROM (SELECT DISTINCT semantic_id FROM graph_business_semantics WHERE status='approved') ids
        LEFT JOIN graph_business_semantics pref ON pref.semantic_id=ids.semantic_id AND pref.locale=? AND pref.status='approved'
        LEFT JOIN graph_business_semantics en ON en.semantic_id=ids.semantic_id AND en.locale='en' AND en.status='approved'
        WHERE COALESCE(pref.semantic_id,en.semantic_id) IS NOT NULL
        """,
        (locale,),
    ).fetchall()
    suggestions = []
    for row in rows:
        aliases = _json(row["aliases_json"], [])
        terms = [row["label"], *aliases]
        exact = next((term for term in terms if str(term).strip().lower() and str(term).strip().lower() in raw_context), "")
        candidate_tokens = _semantic_tokens(" ".join(str(term) for term in terms))
        shared = context_tokens & candidate_tokens
        if exact:
            score = min(0.99, 0.84 + min(len(_semantic_tokens(exact)), 3) * 0.05)
            reasons = [f'Matched code context: "{exact}"']
        elif shared:
            score = min(0.79, 0.42 + len(shared) / max(1, len(candidate_tokens)) * 0.37)
            reasons = ["Shared terms: " + ", ".join(sorted(shared)[:5])]
        else:
            continue
        suggestions.append({
            "semantic_id":row["semantic_id"],"domain":row["domain"],"locale":row["locale"],
            "label":row["label"],"definition":row["definition"],"confidence":round(score,2),
            "status":"suggested","reasons":reasons,
        })
    return sorted(suggestions,key=lambda item:(-item["confidence"],item["label"].lower(),item["semantic_id"]))[:limit]

def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type IN('table','view') AND name=?", (table,)).fetchone())

def graph_db_ready(db: sqlite3.Connection | None = None) -> bool:
    if db is not None:
        return _table_exists(db, "graph_nodes") and _table_exists(db, "graph_edges")
    try:
        with connection() as check_db:
            return graph_db_ready(check_db)
    except sqlite3.Error:
        return False

def _safe_relative_path(value: object) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute():
        return None
    parts = [part for part in path.parts if part not in ("", ".")]
    if ".." in parts or any(part.startswith(".") for part in parts):
        return None
    return "/".join(parts)

def _semantic_level():
    semantic=(request.args.get("semantic_level") or request.args.get("semantic") or "TECHNICAL").upper()
    if semantic not in {"SYSTEM","APPLICATION","TECHNICAL"}:
        return None
    return semantic

def _edge_visible(edge, semantic):
    if semantic == "SYSTEM":
        return edge["graph_layer"] == "DATA_FLOW"
    if semantic == "APPLICATION":
        return edge["graph_layer"] != "STRUCTURAL"
    return True

def _wr_edge_visible(edge, semantic):
    if semantic == "TECHNICAL" and edge["graph_layer"] == "DATA_FLOW":
        return False
    return _edge_visible(edge, semantic)

def _wr_allowed_edge(edge_type, mode):
    terminals = WRITE_TERMINALS if mode == "W" else READ_TERMINALS
    return edge_type in terminals or edge_type in FLOW_INTERMEDIATE

def _semantic_project(nodes, edges, root_node_id, semantic):
    if semantic == "TECHNICAL":
        return nodes, edges
    allowed={node["node_id"] for node in nodes if node["graph_role"]=="MAIN"}
    allowed.add(root_node_id)
    projected_nodes=[node for node in nodes if node["node_id"] in allowed]
    projected_edges=[edge for edge in edges if edge["source_node_id"] in allowed and edge["target_node_id"] in allowed]
    return projected_nodes, projected_edges

def _walk_read_write(db, node_id, mode, direction, limit, semantic):
    terminals = WRITE_TERMINALS if mode == "W" else READ_TERMINALS
    pending=[(node_id,[],False,frozenset({node_id}))]; visited=set(); node_ids={node_id}; edges_by_id={}; selected_paths=[]; truncated=False
    reverse = direction == "in"
    def add_path(path):
        nonlocal truncated
        path_node_ids={endpoint for edge in path for endpoint in (edge["source_node_id"],edge["target_node_id"])} | {node_id}
        if len(node_ids | path_node_ids) > limit:
            truncated=True
            return
        node_ids.update(path_node_ids)
        for edge in path:
            edges_by_id[edge["edge_id"]]=edge
        selected_paths.append([edge["edge_id"] for edge in (reversed(path) if reverse else path)])
    while pending and len(node_ids) <= limit:
        current,path,seen_terminal,path_seen=pending.pop(0)
        state=(current,seen_terminal,path_seen)
        if state in visited:
            continue
        visited.add(state)
        if reverse:
            rows=db.execute("SELECT * FROM graph_edges WHERE target_node_id=? ORDER BY edge_id",(current,)).fetchall()
        else:
            rows=db.execute("SELECT * FROM graph_edges WHERE source_node_id=? ORDER BY edge_id",(current,)).fetchall()
        for row in rows:
            if not _wr_allowed_edge(row["edge_type"], mode) or not _wr_edge_visible(row, semantic):
                continue
            item=dict(row); next_node=row["source_node_id"] if reverse else row["target_node_id"]
            if next_node in path_seen:
                continue
            next_path=path+[item]
            is_terminal=item["edge_type"] in terminals
            if is_terminal or seen_terminal:
                add_path(next_path)
            if (item["edge_type"] in FLOW_INTERMEDIATE or reverse and is_terminal) and len(next_path) < 64:
                pending.append((next_node,next_path,seen_terminal or is_terminal,path_seen | {next_node}))
    return node_ids, edges_by_id, selected_paths, truncated or bool(pending)

def _read_write_flow(db, node_id, mode, direction, limit, semantic):
    directions=["in","out"] if direction == "both" else [direction]
    node_ids={node_id}; edges_by_id={}; paths=[]; truncated=False
    for item_direction in directions:
        walk_nodes, walk_edges, walk_paths, walk_truncated = _walk_read_write(db,node_id,mode,item_direction,limit,semantic)
        if len(node_ids | walk_nodes) > limit:
            truncated=True
            continue
        node_ids.update(walk_nodes); edges_by_id.update(walk_edges); paths.extend(walk_paths); truncated = truncated or walk_truncated
    nodes=[dict(row) for row in db.execute(f"SELECT * FROM graph_nodes WHERE node_id IN ({','.join('?' * len(node_ids))})", sorted(node_ids))]
    edges=[edges_by_id[key] for key in sorted(edges_by_id)]
    nodes, edges = _semantic_project(nodes, edges, node_id, semantic)
    if semantic != "TECHNICAL":
        visible_edge_ids={edge["edge_id"] for edge in edges}
        paths=[path for path in ([edge_id for edge_id in path if edge_id in visible_edge_ids] for path in paths) if path]
    return {"nodes":nodes,"edges":edges,"paths":paths,"truncated":truncated,"max_nodes":limit,"semantic":semantic,"semantic_level":semantic}

@bp.get("/graph-contract")
def graph_contract():
    return jsonify({
        "nodes": [row for row in ("DATABASE","TABLE","COLUMN","SCREEN","UI_ACTION","API_APPLICATION","API_OPERATION","JOB_NETWORK","JOB","EXECUTABLE","PROCEDURE","FUNCTION","SQL_FILE")],
        "edges": [row for row in ("CALLS_API","STARTS","CALLS","READS","WRITES","CONTAINS","RESOLVES_TO")],
    })

@bp.get("/stats")
def stats():
    with connection() as db:
        return jsonify({name: db.execute(f"SELECT COUNT(*) FROM graph_{name}").fetchone()[0] for name in ("nodes","edges","evidence","issues","paths")})

@bp.get("/search")
def search():
    query = request.args.get("q", "").strip()
    database = request.args.get("database", "").strip()
    locale = (request.args.get("locale") or "en").strip().lower() or "en"
    limit = _positive_int_arg("limit", 100, 500)
    if limit is None:
        return error("invalid_request", "limit must be an integer")
    node_types = _requested_node_types("node_types", "node_type", "types")
    excluded_node_types = _requested_node_types("exclude_node_types", "exclude_node_type")
    with connection() as db:
        clauses=[]; params=[]
        if query:
            like=f"%{query}%"
            clauses.append("""
                (n.default_display_name LIKE ? COLLATE NOCASE
                 OR n.technical_name LIKE ? COLLATE NOCASE
                 OR n.qualified_name LIKE ? COLLATE NOCASE
                 OR n.node_id LIKE ? COLLATE NOCASE
                 OR l.value LIKE ? COLLATE NOCASE)
            """)
            params.extend([like,like,like,like,like])
        if database:
            clauses.append("n.database_key=?")
            params.append(database)
        if node_types:
            clauses.append(f"n.node_type IN ({','.join('?' for _ in node_types)})")
            params.extend(node_types)
        if excluded_node_types:
            clauses.append(f"n.node_type NOT IN ({','.join('?' for _ in excluded_node_types)})")
            params.extend(excluded_node_types)
        where=" AND ".join(clauses) if clauses else "1=1"
        rows = db.execute(f"""
            SELECT DISTINCT n.*,
                   COALESCE(pref.value,en.value,n.default_display_name) AS localized_display_name
            FROM graph_nodes n
              LEFT JOIN graph_localization l ON l.target_id=n.node_id
                 AND UPPER(l.review_status)='APPROVED' AND l.locale IN (?, 'en')
              LEFT JOIN graph_localization pref ON pref.target_id=n.node_id
                 AND pref.field_name='name' AND pref.locale=? AND UPPER(pref.review_status)='APPROVED'
              LEFT JOIN graph_localization en ON en.target_id=n.node_id
                 AND en.field_name='name' AND en.locale='en' AND UPPER(en.review_status)='APPROVED'
            WHERE {where}
            ORDER BY CASE
                WHEN n.node_id=? THEN 0
                WHEN n.technical_name=? COLLATE NOCASE THEN 1
                WHEN n.default_display_name=? COLLATE NOCASE THEN 2
                ELSE 3 END,
                localized_display_name COLLATE NOCASE,
                n.node_id
            LIMIT ?
        """, (locale,locale,*params,query,query,query,limit)).fetchall()
        return jsonify([dict(row) for row in rows])

@bp.get("/localization")
def localization():
    target_ids=[value for value in request.args.getlist("target_id") if value][:1000]
    if not target_ids: return jsonify({"items":{}})
    placeholders=','.join('?'*len(target_ids))
    with connection() as db:
        rows=db.execute(f"SELECT target_id,field_name,locale,value FROM graph_localization WHERE target_id IN ({placeholders}) AND locale IN ('ja', 'en') AND UPPER(review_status)='APPROVED'",target_ids).fetchall()
        items={}
        for row in rows: items.setdefault(row["target_id"],{}).setdefault(row["locale"],{})[row["field_name"]]=row["value"]
        for row in db.execute(f"SELECT node_id,default_display_name FROM graph_nodes WHERE node_id IN ({placeholders})",target_ids):
            items.setdefault(row["node_id"],{}).setdefault("en",{}).setdefault("name",row["default_display_name"])
        return jsonify({"items":items})

@bp.get("/nodes/<path:node_id>")
def node(node_id):
    with connection() as db:
        row = db.execute("SELECT * FROM graph_nodes WHERE node_id=?", (node_id,)).fetchone()
        if not row: return error("node_not_found", "Node not found", 404)
        return jsonify({"node": dict(row), "issues": [dict(x) for x in db.execute("SELECT * FROM graph_issues WHERE source_node_id=?", (node_id,))]})

@bp.get("/node-detail")
@bp.get("/edge-detail")
@bp.get("/detail")
def detail():
    target_id=request.args.get("target_id","") or request.args.get("node_id","") or request.args.get("edge_id","") or request.args.get("qname","")
    target_type=(request.args.get("target_type") or ("EDGE" if request.path.endswith("edge-detail") else "NODE")).upper()
    if not target_id: return error("invalid_request","target_id is required")
    with connection() as db:
        if target_type=="EDGE":
            edge=db.execute("SELECT * FROM graph_edges WHERE edge_id=?",(target_id,)).fetchone()
            if not edge: return error("edge_not_found","Edge not found",404)
            edge_item=dict(edge); props=_json(edge_item.get("properties_json"),{})
            source=db.execute("SELECT * FROM graph_nodes WHERE node_id=?",(edge_item["source_node_id"],)).fetchone()
            target=db.execute("SELECT * FROM graph_nodes WHERE node_id=?",(edge_item["target_node_id"],)).fetchone()
            path_edges=[]; path_nodes=[]; related_paths=[]
            edge_path=props.get("edge_path") or []
            if not edge_path and props.get("path_id"):
                path_row=db.execute("""
                    SELECT p.*, (
                        SELECT e.edge_id FROM graph_edges e
                        WHERE e.source_node_id=p.actor_node_id AND e.target_node_id=p.table_node_id
                          AND json_extract(e.properties_json,'$.path_id')=p.path_id
                        LIMIT 1
                    ) AS flow_edge_id
                    FROM graph_paths p WHERE p.path_id=?
                """,(props["path_id"],)).fetchone()
                if path_row:
                    related_paths=_decode_path_rows([path_row])
                    edge_path=related_paths[0].get("edge_path",[])
            if edge_path:
                path_edges=_rows_by_ids(db,"graph_edges","edge_id",edge_path)
                node_ids=[]
                for path_edge in path_edges:
                    node_ids.extend([path_edge["source_node_id"],path_edge["target_node_id"]])
                path_nodes=_rows_by_ids(db,"graph_nodes","node_id",list(dict.fromkeys(node_ids)))
            issues=[dict(row) for row in db.execute("SELECT * FROM graph_issues WHERE source_node_id IN (?,?) ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, issue_type LIMIT 100",(edge_item["source_node_id"],edge_item["target_node_id"]))]
            evidence_count=db.execute("SELECT count(*) FROM graph_evidence WHERE target_id=?",(target_id,)).fetchone()[0]
            trace_evidence_count=_count_evidence_for_targets(db,[edge_id for edge_id in edge_path if edge_id != target_id])
            return jsonify({"target_type":"EDGE","edge":edge_item,"source":dict(source) if source else None,"target":dict(target) if target else None,"properties":props,"path_edges":path_edges,"path_nodes":path_nodes,"related_paths":related_paths,"issues":issues,"counts":{"evidence":evidence_count,"direct_evidence":evidence_count,"trace_evidence":trace_evidence_count,"related_evidence":evidence_count+trace_evidence_count,"issues":len(issues),"path_edges":len(path_edges)}})

        node=db.execute("SELECT * FROM graph_nodes WHERE node_id=?",(target_id,)).fetchone()
        if not node: return error("node_not_found","Node not found",404)
        node_item=dict(node); node_type=node_item["node_type"]
        outgoing=[dict(row) for row in db.execute("""
            SELECT e.*, n.node_id AS other_node_id, n.node_type AS other_node_type,
                   n.default_display_name AS other_display_name, n.technical_name AS other_technical_name,
                   n.database_key AS other_database_key, n.repository_key AS other_repository_key
            FROM graph_edges e JOIN graph_nodes n ON n.node_id=e.target_node_id
            WHERE e.source_node_id=? ORDER BY e.graph_layer,e.edge_type,n.default_display_name LIMIT 500
        """,(target_id,))]
        incoming=[dict(row) for row in db.execute("""
            SELECT e.*, n.node_id AS other_node_id, n.node_type AS other_node_type,
                   n.default_display_name AS other_display_name, n.technical_name AS other_technical_name,
                   n.database_key AS other_database_key, n.repository_key AS other_repository_key
            FROM graph_edges e JOIN graph_nodes n ON n.node_id=e.source_node_id
            WHERE e.target_node_id=? ORDER BY e.graph_layer,e.edge_type,n.default_display_name LIMIT 500
        """,(target_id,))]
        issues=[dict(row) for row in db.execute("SELECT * FROM graph_issues WHERE source_node_id=? ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, issue_type LIMIT 200",(target_id,))]
        columns=[]; table_context=[]
        if node_type=="TABLE":
            columns=[dict(row) for row in db.execute("""
                SELECT c.* FROM graph_edges e JOIN graph_nodes c ON c.node_id=e.target_node_id
                WHERE e.source_node_id=? AND e.edge_type='CONTAINS' AND c.node_type='COLUMN'
                ORDER BY CAST(json_extract(c.properties_json,'$.ordinal_position') AS INTEGER), c.technical_name
            """,(target_id,))]
        elif node_type=="COLUMN":
            table_context=[dict(row) for row in db.execute("""
                SELECT t.* FROM graph_edges e JOIN graph_nodes t ON t.node_id=e.source_node_id
                WHERE e.target_node_id=? AND e.edge_type='CONTAINS' AND t.node_type='TABLE' LIMIT 1
            """,(target_id,))]
        impact_table_id=target_id; dedupe_column_impacts=False
        if node_type=="COLUMN":
            impact_table_id=table_context[0]["node_id"] if table_context else ""
            dedupe_column_impacts=True
        impacts=_enrich_path_facts(db,_decode_path_rows(db.execute("""
                 SELECT p.*, a.node_type AS actor_type, a.default_display_name AS actor_name,
                   a.technical_name AS actor_technical_name, a.database_key AS actor_database_key,
                     a.repository_key AS actor_repository_key,
                     (
                      SELECT e.edge_id FROM graph_edges e
                      WHERE e.source_node_id=p.actor_node_id AND e.target_node_id=p.table_node_id
                        AND json_extract(e.properties_json,'$.path_id')=p.path_id
                      LIMIT 1
                     ) AS flow_edge_id
            FROM graph_paths p JOIN graph_nodes a ON a.node_id=p.actor_node_id
            WHERE p.table_node_id=? ORDER BY p.operation,a.default_display_name LIMIT 500
        """,(impact_table_id,)).fetchall())) if node_type in {"TABLE","COLUMN"} and impact_table_id else []
        if dedupe_column_impacts:
            unique={}
            for path in impacts:
                key=(path["actor_node_id"],path["operation"])
                previous=unique.get(key)
                if previous is None or len(path.get("edge_path",[]))<len(previous.get("edge_path",[])):
                    unique[key]=path
            impacts=sorted(unique.values(),key=lambda path:(path["operation"],path.get("actor_name") or path["actor_node_id"]))
        paths=_enrich_path_facts(db,_decode_path_rows(db.execute("""
                 SELECT p.*, t.node_type AS table_type, t.default_display_name AS table_name,
                     t.technical_name AS table_technical_name, t.database_key AS table_database_key,
                     (
                      SELECT e.edge_id FROM graph_edges e
                      WHERE e.source_node_id=p.actor_node_id AND e.target_node_id=p.table_node_id
                        AND json_extract(e.properties_json,'$.path_id')=p.path_id
                      LIMIT 1
                     ) AS flow_edge_id
            FROM graph_paths p JOIN graph_nodes t ON t.node_id=p.table_node_id
            WHERE p.actor_node_id=? ORDER BY p.operation,t.default_display_name LIMIT 500
        """,(target_id,)).fetchall()))
        evidence_count=db.execute("SELECT count(*) FROM graph_evidence WHERE target_id=?",(target_id,)).fetchone()[0]
        counts={
            "incoming":len(incoming),"outgoing":len(outgoing),"relations":len(incoming)+len(outgoing),
            "columns":len(columns),"issues":len(issues),"evidence":evidence_count,
            "impacts":len(impacts),"paths":len(paths)
        }
        locale=(request.args.get("locale") or "en").strip().lower() or "en"
        properties=_json(node_item.get("properties_json"),{})
        tree_target_ids=_semantic_tree_target_ids(target_id,properties.get("semantic_tree"))
        business_semantics=_business_semantic_rows(db,target_id,locale)
        tree_business_semantics=_semantic_tree_business_semantics(db,tree_target_ids,locale)
        tree_nodes={row["node_id"]:dict(row) for row in _rows_by_ids(db,"graph_nodes","node_id",tree_target_ids)}
        return jsonify({"target_type":"NODE","node":node_item,"properties":properties,"business_semantics":business_semantics,"semantic_tree_business_semantics":tree_business_semantics,"semantic_tree_nodes":tree_nodes,"incoming":incoming,"outgoing":outgoing,"columns":columns,"table_context":table_context,"impacts":impacts,"paths":paths,"issues":issues,"counts":counts})

@bp.get("/business-semantic-suggestions")
def business_semantic_suggestions():
    target_id=request.args.get("target_id","").strip()
    locale=(request.args.get("locale") or "en").strip().lower() or "en"
    limit=_positive_int_arg("limit",3,10)
    if not target_id or limit is None:
        return error("invalid_request","target_id and integer limit are required")
    with connection() as db:
        if not db.execute("SELECT 1 FROM graph_nodes WHERE node_id=?",(target_id,)).fetchone():
            return error("node_not_found","Node not found",404)
        selected=_business_semantic_rows(db,target_id,locale)
        return jsonify({"target_id":target_id,"selected":selected,"items":_semantic_suggestions(db,target_id,locale,limit)})

@bp.put("/business-semantic-mappings/<path:target_id>")
def business_semantic_mapping(target_id):
    if not request.is_json or request.content_length is None or request.content_length>SEMANTIC_MAX_BYTES:
        return error("invalid_semantic_mapping","Bounded JSON body required",415)
    body=request.get_json(silent=True)
    if not isinstance(body,dict) or set(body)!={"semantic_id","author_name"}:
        return error("invalid_semantic_mapping","Exact semantic_id and author_name required")
    semantic_id=body["semantic_id"] if isinstance(body["semantic_id"],str) else ""
    author_name=body["author_name"] if isinstance(body["author_name"],str) else ""
    if not semantic_id or not author_name.strip() or len(author_name)>100:
        return error("invalid_semantic_mapping","Valid semantic_id and author_name required")
    with connection() as db:
        target_exists=db.execute("SELECT 1 FROM graph_nodes WHERE node_id=?",(target_id,)).fetchone()
        semantic_exists=db.execute("SELECT 1 FROM graph_business_semantics WHERE semantic_id=? AND status='approved' LIMIT 1",(semantic_id,)).fetchone()
        if not target_exists or not semantic_exists:
            return error("invalid_semantic_mapping","Existing node and approved semantic required")
        db.execute(
            """
            INSERT INTO graph_business_semantic_mappings(target_id,semantic_id,status,source_kind,author_name,confidence)
            VALUES(?,?,'approved','USER',?,1.0)
            ON CONFLICT(target_id) DO UPDATE SET semantic_id=excluded.semantic_id,status='approved',source_kind='USER',
              author_name=excluded.author_name,confidence=1.0,updated_at=datetime('now')
            """,
            (target_id,semantic_id,author_name.strip()),
        )
        db.commit()
        return jsonify({"target_id":target_id,"items":_business_semantic_rows(db,target_id,(request.args.get("locale") or "en").strip().lower() or "en")})

@bp.get("/flow-all")
@bp.get("/lineage")
@bp.get("/flow")
def flow():
    node_id = request.args.get("node_id", "") or request.args.get("qname", "")
    mode = request.args.get("mode", "1").upper()
    direction = request.args.get("direction", "both").lower()
    if mode not in FLOW_MODES: return error("invalid_request","invalid mode")
    if direction not in {"in","out","both"}: return error("invalid_request","invalid direction")
    try: limit = max(1, min(1000, int(request.args.get("max_nodes", 500))))
    except ValueError: return error("invalid_request", "max_nodes must be an integer")
    semantic=_semantic_level()
    if not semantic: return error("invalid_request","invalid semantic")
    with connection() as db:
        if mode in {"W", "R"}:
            return jsonify(_read_write_flow(db,node_id,mode,direction,limit,semantic))
        pending = [node_id]; visited = set(); edges = []; node_ids={node_id}; truncated=False
        while pending and len(node_ids) <= limit:
            current = pending.pop(0); visited.add(current)
            if direction == "in":
                rows = db.execute("SELECT * FROM graph_edges WHERE target_node_id=? ORDER BY edge_id", (current,)).fetchall()
            elif direction == "out":
                rows = db.execute("SELECT * FROM graph_edges WHERE source_node_id=? ORDER BY edge_id", (current,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM graph_edges WHERE source_node_id=? OR target_node_id=? ORDER BY edge_id", (current, current)).fetchall()
            for row in rows:
                item=dict(row); candidate={item["source_node_id"],item["target_node_id"]}
                if not _edge_visible(item, semantic): continue
                if len(node_ids|candidate)>limit: truncated=True; continue
                if item not in edges: edges.append(item)
                node_ids.update(candidate)
                if mode == "E":
                    other = row["source_node_id"] if direction == "in" else row["target_node_id"] if direction == "out" else row["target_node_id"] if row["source_node_id"] == current else row["source_node_id"]
                    if other not in visited and other not in pending: pending.append(other)
            if mode == "1": break
        truncated=truncated or bool(pending)
        nodes = [dict(row) for row in db.execute(f"SELECT * FROM graph_nodes WHERE node_id IN ({','.join('?' * len(node_ids))})", sorted(node_ids))]
        nodes, edges = _semantic_project(nodes, edges, node_id, semantic)
        return jsonify({"nodes": nodes, "edges": edges, "truncated": truncated, "max_nodes": limit, "semantic":semantic,"semantic_level":semantic})

@bp.get("/edge-evidence")
@bp.get("/evidence")
def evidence():
    target_id = request.args.get("target_id", "") or request.args.get("edge_id", "")
    cursor = request.args.get("cursor", "")
    limit = _positive_int_arg("limit", 50, 100)
    if limit is None:
        return error("invalid_request", "limit must be an integer")
    with connection() as db:
        include_related = (request.args.get("include_related") or "").lower()
        trace_ids = _edge_trace_ids(db, target_id) if include_related in {"trace","path","paths"} else []
        related_target_ids = _unique_values([target_id, *trace_ids]) if trace_ids else [target_id]
        placeholders = ','.join('?' * len(related_target_ids))
        total = db.execute(f"SELECT count(*) FROM graph_evidence WHERE target_id IN ({placeholders})", related_target_ids).fetchone()[0]
        rows = db.execute(f"SELECT * FROM graph_evidence WHERE target_id IN ({placeholders}) AND evidence_id>? ORDER BY evidence_id LIMIT ?", (*related_target_ids, cursor, limit + 1)).fetchall()
        items=[dict(row) for row in rows[:limit]]
        truncated=len(rows)>limit
        direct_total = db.execute("SELECT count(*) FROM graph_evidence WHERE target_id=?", (target_id,)).fetchone()[0]
        trace_total = _count_evidence_for_targets(db,[edge_id for edge_id in trace_ids if edge_id != target_id])
        return jsonify({
            "items":items,
            "count":len(items),
            "truncated":truncated,
            "cursor":items[-1]["evidence_id"] if truncated and items else None,
            "evidence_count":total,
            "evidence_truncated":truncated,
            "evidence_scope":"trace" if include_related in {"trace","path","paths"} else "direct",
            "direct_evidence_count":direct_total,
            "trace_evidence_count":trace_total,
            "related_target_ids":related_target_ids,
        })

@bp.get("/snippet")
def snippet():
    relative = _safe_relative_path(request.args.get("path", ""))
    target_id = request.args.get("target_id", "") or request.args.get("edge_id", "")
    if not relative:
        return jsonify(_snippet_payload("invalid_source", None, None)), 403
    path = (ROOT / relative).resolve()
    with connection() as db:
        if target_id:
            evidence_row=db.execute("""
                SELECT source_path,start_line,end_line FROM graph_evidence
                WHERE source_path=? AND target_id=? ORDER BY evidence_id LIMIT 1
            """,(relative,target_id)).fetchone()
        else:
            evidence_row=db.execute("""
                SELECT source_path,start_line,end_line FROM graph_evidence
                WHERE source_path=? ORDER BY evidence_id LIMIT 1
            """,(relative,)).fetchone()
    safe=any(path==root or root in path.parents for root in SAFE_SOURCE_ROOTS)
    hidden=path.is_relative_to(ROOT) and any(part.startswith('.') for part in path.relative_to(ROOT).parts)
    if not evidence_row or not safe or hidden:
        return jsonify(_snippet_payload("invalid_source", relative, evidence_row)), 403
    if path.suffix.lower() in {".db",".sqlite",".sqlite3",".env",".ini",".toml",".yaml",".yml",".json"}:
        return jsonify(_snippet_payload("invalid_source", relative, evidence_row)), 403
    if not path.exists() or not path.is_file():
        return jsonify(_snippet_payload("not_found", relative, evidence_row))
    if not os.access(path, os.R_OK):
        return jsonify(_snippet_payload("unreadable", relative, evidence_row))
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return jsonify(_snippet_payload("unreadable", relative, evidence_row))
    return jsonify(_snippet_payload("available", relative, evidence_row, content))

def _snippet_payload(status: str, relative: str | None, evidence_row: sqlite3.Row | None, content: str | None = None) -> dict[str, Any]:
    start_line = _positive_line(evidence_row["start_line"] if evidence_row else None)
    end_line = _positive_line(evidence_row["end_line"] if evidence_row and evidence_row["end_line"] else start_line)
    focus_line = start_line or end_line or 1
    payload = {
        "status": status,
        "path": relative,
        "source_path": relative,
        "focus_line": focus_line,
        "start_line": start_line,
        "end_line": end_line,
        "content": content,
        "text": content,
    }
    return payload

def _positive_line(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None

@bp.route("/knowledge", methods=["GET", "POST"])
def knowledge():
    """GET is public read-only; POST requires header-token auth, never cookies."""
    with connection() as db:
        if request.method == "POST":
            token=os.environ.get("CODE_MAP_KNOWLEDGE_TOKEN","")
            supplied=request.headers.get("X-Code-Map-Token","")
            if not token or not hmac.compare_digest(supplied,token): return error("forbidden","Knowledge token required",403)
            if not request.is_json or request.content_length is None or request.content_length>KNOWLEDGE_MAX_BYTES: return error("invalid_knowledge","Bounded JSON body required",415)
            body=request.get_json(silent=True)
            if not isinstance(body,dict) or set(body)!={"target_id","body","status"}: return error("invalid_knowledge","Exact target_id, body and status required")
            text=body["body"] if isinstance(body["body"],str) else ""
            target=body["target_id"] if isinstance(body["target_id"],str) else ""
            exists=db.execute("SELECT 1 FROM graph_nodes WHERE node_id=? UNION ALL SELECT 1 FROM graph_edges WHERE edge_id=? LIMIT 1",(target,target)).fetchone()
            if body["status"] not in KNOWLEDGE_STATUSES or not text.strip() or len(text.encode("utf-8"))>KNOWLEDGE_MAX_BYTES or not exists: return error("invalid_knowledge","Existing target, bounded body and valid status required")
            row=db.execute("INSERT INTO graph_knowledge(target_id,body,status) VALUES(?,?,?) RETURNING *",(target,text.strip(),body["status"])).fetchone(); db.commit()
            return jsonify(dict(row)),201
        return jsonify([dict(row) for row in db.execute("SELECT * FROM graph_knowledge WHERE target_id=? ORDER BY knowledge_id", (request.args.get("target_id", ""),))])

@bp.post("/imports/validate")
def validate_imports():
    roots_or_response = _import_roots_from_request()
    if not isinstance(roots_or_response, list):
        return roots_or_response
    try:
        from application.backend.importer import pipeline
        from application.backend.importer.package_validator import validate_package

        input_root = _workspace_path(os.environ.get("CODE_MAP_INPUT_ROOT", "input-data"))
        allowed_ids = pipeline.validation_ids(roots_or_response, input_root)
        packages = [validate_package(root, allowed_ids, workspace_root=ROOT) for root in roots_or_response]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return error("validation_failed", "Import package validation failed", 400, reason=_safe_error_text(exc))
    return jsonify({
        "status":"valid",
        "package_count":len(packages),
        "packages":[_package_summary(package, root) for package, root in zip(packages, roots_or_response)],
    })

@bp.post("/imports")
def imports_create():
    roots_or_response = _import_roots_from_request()
    if not isinstance(roots_or_response, list):
        return roots_or_response
    try:
        from application.backend.importer import pipeline

        input_root = _workspace_path(os.environ.get("CODE_MAP_INPUT_ROOT", "input-data"))
        counts = pipeline.import_roots(roots_or_response, Path(_graph_db_path()), input_root=input_root)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as exc:
        return error("import_failed", "Import package execution failed", 400, reason=_safe_error_text(exc))
    return jsonify({"status":"imported","package_count":len(roots_or_response),"counts":counts}), 201

def _graph_db_path() -> Path:
    try:
        return Path(current_app.config.get("CODE_MAP_GRAPH_DB_PATH", DB_PATH))
    except RuntimeError:
        return DB_PATH

def _import_roots_from_request():
    if request.content_length is not None and request.content_length > MAX_IMPORT_BODY_BYTES:
        return error("body_too_large", "Request body exceeds configured limit", 413)
    if request.files:
        return error("unsupported_upload", "File upload imports are not supported; provide local package paths", 400)
    if not request.is_json:
        return error("unsupported_upload", "JSON body with local package paths is required", 400)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("invalid_request", "JSON object with local package paths is required")
    raw_roots: list[object] = []
    for key in ("roots", "paths", "package_paths", "packagePaths"):
        value = body.get(key)
        if isinstance(value, list):
            raw_roots.extend(value)
    for key in ("root", "path", "package_path", "packagePath"):
        value = body.get(key)
        if value:
            raw_roots.append(value)
    roots=[]
    for value in raw_roots:
        root = _local_package_root(value)
        if root is None:
            return error("invalid_path", "Package paths must be local workspace-relative directories")
        roots.append(root)
    roots = list(dict.fromkeys(roots))
    if not roots:
        return error("invalid_request", "At least one local package path is required")
    return roots[:25]

def _local_package_root(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    raw = Path(text).expanduser()
    path = raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        return None
    return path if path.is_dir() else None

def _package_summary(package: dict[str, object], root: Path) -> dict[str, object]:
    manifest = dict(package.get("manifest") or {})
    return {
        "root":_repo_relative(root),
        "package_id":manifest.get("packageId"),
        "source_id":manifest.get("sourceId"),
        "contract_version":manifest.get("contractVersion"),
        "counts":manifest.get("counts", {}),
    }

def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return path.name

def _safe_error_text(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    text = text.replace(str(ROOT), "<workspace>")
    return re.sub(r"/[^\s:'\"]+", "<path>", text)

@bp.get("/imports")
def imports_list():
    with connection() as db:
        return jsonify([dict(row) for row in db.execute("SELECT source_id,package_id,created_at FROM graph_sources ORDER BY source_id")])

@bp.get("/imports/<run_id>")
def imports_detail(run_id):
    with connection() as db:
        row=db.execute("SELECT source_id,package_id,created_at FROM graph_sources WHERE source_id=?",(run_id,)).fetchone()
        if not row: return error("import_not_found","Import not found",404)
        return jsonify(dict(row))
