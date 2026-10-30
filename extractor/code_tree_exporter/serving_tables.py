"""The ``graph_*`` tables of ``codemap.sqlite``: what the web app queries and the search index is built from.

The web app reads the extract output directly (no import step). It keeps the
vocabulary it was built on:

* node and edge ids are stable string ids;
* engine edges get their serving names (``contract/vocabulary.py``:
  ``READS_FROM`` → ``READS``/``REMOTE_READS``, ``WRITES_TO`` →
  ``INSERTS``/``UPDATES``/..., ``HANDLES_API`` → ``HANDLED_BY``);
* complete engine flows become ``graph_paths`` rows plus ``DATA_FLOW`` edges
  (actor → table);
* table/column names from ``inputData`` become ``graph_localization`` rows.

``semantic_tree`` can be megabytes per node, so it is left out of
``graph_nodes.properties_json``; the web app reads it from ``graph.sqlite``
when a node's detail is opened.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import logging
import re
import sqlite3
from pathlib import Path

from .decoding import read_text_auto

from .contract.graph_contract import canonical_edge_id
from .contract.vocabulary import READ_EDGES, WRITE_EDGES, serving_edge_type

SOURCE_PREFIX = "code-tree:"
# graph_paths keep a flow whose last edge reads or writes.
READ_TERMINALS = READ_EDGES
WRITE_TERMINALS = WRITE_EDGES
_HEAVY_PROPERTIES = ("semantic_tree",)

SCHEMA = """
CREATE TABLE graph_sources(source_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE graph_nodes(node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, technical_name TEXT NOT NULL,
  qualified_name TEXT NOT NULL, default_display_name TEXT NOT NULL, system_key TEXT, database_key TEXT,
  repository_key TEXT, graph_role TEXT NOT NULL, confidence REAL NOT NULL, properties_json TEXT NOT NULL,
  source_id TEXT NOT NULL);
CREATE TABLE graph_edges(edge_id TEXT PRIMARY KEY, source_node_id TEXT NOT NULL, target_node_id TEXT NOT NULL,
  edge_type TEXT NOT NULL, graph_layer TEXT NOT NULL, raw_operation TEXT NOT NULL, confidence REAL NOT NULL,
  properties_json TEXT NOT NULL, source_id TEXT NOT NULL);
CREATE TABLE graph_evidence(evidence_id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
  source_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER, start_column INTEGER, end_column INTEGER,
  evidence_kind TEXT NOT NULL, extractor_name TEXT NOT NULL, confidence REAL NOT NULL, snippet TEXT NOT NULL,
  properties_json TEXT NOT NULL, source_id TEXT NOT NULL);
CREATE TABLE graph_issues(issue_id TEXT PRIMARY KEY, issue_type TEXT NOT NULL, severity TEXT NOT NULL,
  source_node_id TEXT, raw_reference TEXT, database_key TEXT, source_path TEXT, start_line INTEGER,
  message TEXT NOT NULL, properties_json TEXT NOT NULL, source_id TEXT NOT NULL);
CREATE TABLE graph_paths(path_id TEXT PRIMARY KEY, actor_node_id TEXT NOT NULL, table_node_id TEXT NOT NULL,
  operation TEXT NOT NULL, edge_path_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL, source_id TEXT NOT NULL);
CREATE TABLE graph_localization(target_type TEXT NOT NULL, target_id TEXT NOT NULL, field_name TEXT NOT NULL,
  locale TEXT NOT NULL, value TEXT NOT NULL, source_kind TEXT NOT NULL, review_status TEXT NOT NULL,
  author_name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(target_id, field_name, locale));
CREATE TABLE graph_business_semantics(semantic_id TEXT NOT NULL, locale TEXT NOT NULL, domain TEXT NOT NULL,
  label TEXT NOT NULL, definition TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
  source_kind TEXT NOT NULL DEFAULT 'IMPORTED', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(semantic_id, locale));
"""

INDEXES = """
CREATE INDEX graph_nodes_search ON graph_nodes(default_display_name, technical_name, node_type);
CREATE INDEX graph_nodes_type ON graph_nodes(node_type);
CREATE INDEX graph_nodes_technical_name ON graph_nodes(technical_name);
CREATE INDEX graph_edges_source ON graph_edges(source_node_id, edge_type);
CREATE INDEX graph_edges_target ON graph_edges(target_node_id, edge_type);
CREATE INDEX graph_evidence_target_id ON graph_evidence(target_id, evidence_id);
CREATE INDEX graph_evidence_path ON graph_evidence(source_path);
CREATE INDEX graph_issues_source ON graph_issues(source_node_id, issue_type);
CREATE INDEX graph_paths_actor ON graph_paths(actor_node_id, operation, table_node_id);
CREATE INDEX graph_paths_table ON graph_paths(table_node_id, operation, actor_node_id);
CREATE INDEX graph_localization_value ON graph_localization(value);
"""


def write_serving_tables(graph: sqlite3.Connection, db: sqlite3.Connection, input_root: Path | None) -> dict[int, str]:
    """Write the graph_* tables; returns engine node id -> stable id."""
    db.executescript(SCHEMA)
    created = str(_metadata(graph).get("generated_at") or _now())
    _sources(graph, db, created)
    node_ids, node_types = _nodes(graph, db)
    edge_ids = _edges(graph, db, node_ids, node_types)
    _evidence(graph, db, node_ids, edge_ids)
    _issues(graph, db, node_ids)
    _flows(graph, db, node_ids, edge_ids)
    _localization(db, input_root)
    _semantics(db, input_root)
    return node_ids


def finish_serving_tables(db: sqlite3.Connection) -> None:
    db.executescript(INDEXES)


def add_business_node(db: sqlite3.Connection, stable_id: str, node_type: str, name: str, properties: dict) -> None:
    db.execute(
        "INSERT OR IGNORE INTO graph_sources VALUES(?,?,?)", (SOURCE_PREFIX + "business", SOURCE_PREFIX + "business", _now())
    )
    db.execute(
        "INSERT OR IGNORE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (stable_id, node_type, name, stable_id, name, None, None, None, "MAIN", 1.0,
         json.dumps(properties, ensure_ascii=False, sort_keys=True), SOURCE_PREFIX + "business"),
    )


def add_business_edge(db: sqlite3.Connection, source: str, target: str, edge_type: str, operation: str = "") -> None:
    db.execute(
        "INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",
        (canonical_edge_id(source, edge_type, target, operation, "TECHNICAL"), source, target, edge_type,
         "TECHNICAL", operation, 1.0, "{}", SOURCE_PREFIX + "business"),
    )


def _metadata(graph: sqlite3.Connection) -> dict[str, object]:
    return {key: json.loads(value) for key, value in graph.execute("SELECT key, value_json FROM metadata")}


def _source_id(package_key: str | None) -> str:
    return SOURCE_PREFIX + (package_key or "global")


def _sources(graph: sqlite3.Connection, db: sqlite3.Connection, created: str) -> None:
    keys = {row[0] for table in ("nodes", "edges", "evidence", "issues") for row in graph.execute(f"SELECT DISTINCT package_key FROM {table}")}
    db.executemany(
        "INSERT OR REPLACE INTO graph_sources VALUES(?,?,?)",
        [(_source_id(key), _source_id(key), created) for key in sorted(keys, key=str)],
    )


def _restore_references(raw: str | None, node_ids: dict[int, str]) -> str:
    """graph.sqlite stores node ids inside properties (semantic_tree ref_node_id, candidate_node_ids, ...) as
    compact integers; the serving database speaks stable ids, so map them back."""
    raw = raw or "{}"
    if raw == "{}" or not node_ids:
        return raw
    try:
        properties = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    changed = False

    def restore(item):
        nonlocal changed
        # Compact ids are 63-bit hashes; small integers (lines, counts) are never node ids.
        if isinstance(item, int) and not isinstance(item, bool) and abs(item) > 2**31 and item in node_ids:
            changed = True
            return node_ids[item]
        if isinstance(item, str) and len(item) > 9 and item.lstrip("-").isdigit() and int(item) in node_ids:
            changed = True
            return node_ids[int(item)]
        if isinstance(item, list):
            return [restore(value) for value in item]
        if isinstance(item, dict):
            return {key: restore(value) for key, value in item.items()}
        return item

    restored = restore(properties)
    return json.dumps(restored, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if changed else raw


def _nodes(graph: sqlite3.Connection, db: sqlite3.Connection) -> tuple[dict[int, str], dict[str, str]]:
    node_ids: dict[int, str] = dict(graph.execute("SELECT node_id, stable_id FROM nodes"))
    node_types: dict[str, str] = {}
    rows = []
    for row in graph.execute(
        "SELECT node_id, stable_id, node_type, technical_name, qualified_name, default_display_name, system_key, "
        "database_key, repository_key, graph_role, confidence, properties_json, package_key FROM nodes ORDER BY stable_id"
    ):
        node_types[row[1]] = row[2]
        rows.append((*row[1:11], _light_properties(_restore_references(row[11], node_ids)), _source_id(row[12])))
    db.executemany("INSERT OR IGNORE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return node_ids, node_types


def _light_properties(raw: str | None) -> str:
    raw = raw or "{}"
    if len(raw) <= 1024 or not any(f'"{key}"' in raw for key in _HEAVY_PROPERTIES):
        return raw
    properties = json.loads(raw)
    for key in _HEAVY_PROPERTIES:
        properties.pop(key, None)
    return json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _edges(graph, db, node_ids: dict[int, str], node_types: dict[str, str]) -> dict[int, str]:
    edge_ids: dict[int, str] = {}
    identities: dict[tuple, str] = {}
    rows = []
    for row in graph.execute(
        "SELECT edge_id, source_node_id, target_node_id, edge_type, graph_layer, raw_operation, confidence, "
        "properties_json, package_key FROM edges ORDER BY stable_id"
    ):
        source, target = node_ids.get(row[1]), node_ids.get(row[2])
        if source is None or target is None:
            continue
        raw_operation = row[5] or ""
        edge_type = serving_edge_type(row[3], raw_operation, node_types.get(target, ""))
        # DATA_FLOW is rebuilt from flows below; engine data-flow edges stay technical.
        layer = "TECHNICAL" if row[4] == "DATA_FLOW" else row[4]
        identity = (source, target, edge_type, layer, raw_operation)
        if identity in identities:
            edge_ids[row[0]] = identities[identity]
            continue
        edge_id = canonical_edge_id(source, edge_type, target, raw_operation, layer)
        properties = json.loads(_restore_references(row[7], node_ids))
        if edge_type != row[3] or layer != row[4]:
            properties.setdefault("engine_edge_type", row[3])
            properties.setdefault("engine_graph_layer", row[4])
        identities[identity] = edge_id
        edge_ids[row[0]] = edge_id
        rows.append((
            edge_id, source, target, edge_type, layer, raw_operation, row[6],
            json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":")), _source_id(row[8]),
        ))
    db.executemany("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return edge_ids


def _evidence(graph, db, node_ids: dict[int, str], edge_ids: dict[int, str]) -> None:
    rows = []
    for row in graph.execute(
        "SELECT stable_id, target_type, target_id, source_path, start_line, end_line, start_column, end_column, "
        "evidence_kind, extractor_name, confidence, snippet, properties_json, package_key FROM evidence ORDER BY stable_id"
    ):
        target = (node_ids if row[1] == "NODE" else edge_ids).get(row[2])
        if target is None:
            continue
        start_line = row[4] if row[4] and row[4] > 0 else None
        rows.append((row[0], row[1], target, row[3], start_line, *row[5:11], row[11] or "", row[12] or "{}", _source_id(row[13])))
    db.executemany("INSERT OR IGNORE INTO graph_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _issues(graph, db, node_ids: dict[int, str]) -> None:
    db.executemany(
        "INSERT OR IGNORE INTO graph_issues VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (row[0], row[1], row[2], node_ids.get(row[3]) if row[3] is not None else None, *row[4:9], _restore_references(row[9], node_ids), _source_id(row[10]))
            for row in graph.execute(
                "SELECT stable_id, issue_type, severity, source_node_id, raw_reference, database_key, source_path, "
                "start_line, message, properties_json, package_key FROM issues ORDER BY stable_id"
            )
        ],
    )


def _flows(graph, db, node_ids: dict[int, str], edge_ids: dict[int, str]) -> None:
    """graph_paths and actor → table DATA_FLOW edges from the engine's complete flows."""
    steps: dict[int, list[int | None]] = {}
    for flow_id, incoming in graph.execute("SELECT flow_id, incoming_edge_id FROM flow_steps ORDER BY flow_id, step_index"):
        steps.setdefault(flow_id, []).append(incoming)
    edges = {
        row[0]: row
        for row in db.execute("SELECT edge_id, source_node_id, target_node_id, edge_type, source_id, raw_operation, properties_json FROM graph_edges")
    }
    evidence_by_edge: dict[str, list[str]] = {}
    for target_id, evidence_id in db.execute("SELECT target_id, evidence_id FROM graph_evidence WHERE target_type='EDGE'"):
        evidence_by_edge.setdefault(target_id, []).append(evidence_id)
    for flow_id, start, target in graph.execute(
        "SELECT flow_id, start_node_id, target_node_id FROM flows WHERE complete=1 ORDER BY stable_id"
    ).fetchall():
        actor, table = node_ids.get(start), node_ids.get(target)
        path = [edge_ids.get(edge) for edge in steps.get(flow_id, []) if edge is not None]
        if actor is None or table is None or not path or None in path or path[-1] not in edges:
            continue
        terminal = edges[path[-1]]
        operation = "R" if terminal[3] in READ_TERMINALS else "W" if terminal[3] in WRITE_TERMINALS else ""
        if not operation:
            continue
        evidence = sorted({item for edge in path for item in evidence_by_edge.get(edge, ())})
        path_id = "path:" + hashlib.sha256(f"{actor}|{table}|{operation}|{'|'.join(path)}".encode()).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO graph_paths VALUES(?,?,?,?,?,?,?)",
            (path_id, actor, table, operation, json.dumps(path), json.dumps(evidence), terminal[4]),
        )
        data_edge_type = "READS" if operation == "R" else "WRITES"
        terminal_operation = terminal[5] or terminal[3]
        properties = {
            "path_id": path_id, "edge_path": path, "terminal_edge_id": terminal[0], "terminal_edge_type": terminal[3],
            "terminal_raw_operation": terminal[5], "operation": operation,
        }
        terminal_properties = json.loads(terminal[6] or "{}")
        if "semantic" in terminal_properties:
            properties["semantic"] = terminal_properties["semantic"]
        db.execute(
            "INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",
            (canonical_edge_id(actor, data_edge_type, table, terminal_operation, "DATA_FLOW"), actor, table,
             data_edge_type, "DATA_FLOW", terminal_operation, 1.0, json.dumps(properties, sort_keys=True), terminal[4]),
        )


def _rows(root: Path | None, relative: str) -> list[dict[str, str]]:
    if root is None or not (root / relative).is_file():
        return []
    text = read_text_auto(root / relative)
    return [{key: (value or "").strip() for key, value in row.items() if key} for row in csv.DictReader(io.StringIO(text, newline=""))]


def _localization(db: sqlite3.Connection, input_root: Path | None) -> None:
    """ja/en/vi names of tables and columns, matched by table/column code in any database."""
    tables: dict[str, list[str]] = {}
    columns: dict[tuple[str, str], list[str]] = {}
    for node_id, node_type, name, properties, qualified in db.execute(
        "SELECT node_id, node_type, technical_name, properties_json, qualified_name FROM graph_nodes WHERE node_type IN ('TABLE','COLUMN')"
    ):
        if node_type == "TABLE":
            tables.setdefault(name.upper(), []).append(node_id)
        else:
            table = str(json.loads(properties or "{}").get("table") or "").upper()
            if not table:
                parts = qualified.split(".")
                table = parts[-2].upper() if len(parts) >= 2 else ""
            columns.setdefault((table, name.upper()), []).append(node_id)
    now = _now()
    rows: dict[tuple[str, str], tuple] = {}

    def add(target_ids: list[str], locale: str, value: str, source_kind: str, review: str, author: str) -> None:
        for target_id in target_ids:
            if value:
                rows[(target_id, locale)] = ("NODE", target_id, "name", locale, value, source_kind, review, author, now, now, SOURCE_PREFIX + "localization")

    for table in _rows(input_root, "tables.csv"):
        code = table.get("table_code", "").upper()
        for locale in ("ja", "en", "vi"):
            add(tables.get(code, []), locale, table.get(f"table_name_{locale}", ""), "IMPORTED", "approved", "tables")
        for column in _rows(input_root, f"tables/{table.get('table_code', '')}.csv"):
            key = (code, column.get("column_code", "").upper())
            for locale in ("ja", "en", "vi"):
                add(columns.get(key, []), locale, column.get(f"column_name_{locale}", ""), "IMPORTED", "approved", "tables")
    for row in _rows(input_root, "localized-metadata.csv"):
        if row.get("field_name") != "name":
            continue
        parts = row.get("target_id", "").split(":")
        if parts[0] == "table" and len(parts) >= 3:
            targets = tables.get(parts[-1].upper(), [])
        elif parts[0] == "column" and len(parts) >= 4:
            targets = columns.get((parts[-2].upper(), parts[-1].upper()), [])
        else:
            targets = [row.get("target_id", "")] if db.execute(
                "SELECT 1 FROM graph_nodes WHERE node_id=?", (row.get("target_id", ""),)
            ).fetchone() else []
        add(targets, row.get("locale", ""), row.get("value", ""), row.get("source_kind") or "IMPORTED",
            row.get("review_status") or "approved", row.get("author_name") or "")
    db.executemany("INSERT OR REPLACE INTO graph_localization VALUES(?,?,?,?,?,?,?,?,?,?,?)", list(rows.values()))


def _semantics(db: sqlite3.Connection, input_root: Path | None) -> None:
    now = _now()
    rows = []
    for row in _rows(input_root, "semantic-dictionary.csv"):
        semantic_id = row.get("semantic_id", "")
        status = (row.get("status") or "approved").lower()
        if not re.fullmatch(r"semantic:[a-z0-9][a-z0-9:_-]*", semantic_id) or status not in {"draft", "approved", "deprecated"}:
            # One bad row must not throw away an extract that already ran for minutes; skip it loudly.
            logging.getLogger(__name__).warning("Skipping invalid semantic dictionary row: %s", semantic_id or "<missing>")
            continue
        aliases = [alias.strip() for alias in row.get("aliases", "").split("|") if alias.strip()]
        rows.append((semantic_id, (row.get("locale") or "en").lower(), row.get("domain", ""), row.get("label", ""),
                     row.get("definition", ""), json.dumps(aliases, ensure_ascii=False), status, "IMPORTED", now, now))
    db.executemany("INSERT OR REPLACE INTO graph_business_semantics VALUES(?,?,?,?,?,?,?,?,?,?)", rows)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
