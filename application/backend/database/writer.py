from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from contract.entities import ExtractionResult, MetadataFact
from application.backend.database.state import Lease, assert_lease, utc_now

DDL = """
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  qualified_name TEXT NOT NULL UNIQUE,
  name TEXT,
  properties_json TEXT
);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_qname TEXT NOT NULL,
  to_qname TEXT NOT NULL,
  rel_type TEXT NOT NULL,
  properties_json TEXT,
  source_file TEXT,
  line INTEGER
);
CREATE TABLE IF NOT EXISTS table_columns (
  id TEXT PRIMARY KEY,
  db_name TEXT,
  schema_name TEXT,
  table_name TEXT,
    code TEXT,
  name TEXT NOT NULL,
    name_ja TEXT,
    name_en TEXT,
  description TEXT,
  comment TEXT
);
CREATE TABLE IF NOT EXISTS table_definitions (
    id TEXT PRIMARY KEY,
    db_name TEXT,
    schema_name TEXT,
    table_name TEXT,
    code TEXT,
    name_ja TEXT,
    name_en TEXT,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_label_name ON nodes(label, name);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_qname, rel_type);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_qname, rel_type);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel_type);
CREATE INDEX IF NOT EXISTS idx_table_columns_table ON table_columns(db_name, schema_name, table_name);
CREATE INDEX IF NOT EXISTS idx_table_definitions_table ON table_definitions(db_name, schema_name, table_name);
CREATE TABLE IF NOT EXISTS node_facts (
  file_id INTEGER NOT NULL,
  qname TEXT NOT NULL,
  label TEXT NOT NULL,
  name TEXT,
  properties_json TEXT NOT NULL,
  source TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(file_id, qname)
);
CREATE TABLE IF NOT EXISTS edge_facts (
  file_id INTEGER NOT NULL,
  fact_key TEXT NOT NULL,
  from_qname TEXT NOT NULL,
  to_qname TEXT NOT NULL,
  rel_type TEXT NOT NULL,
  properties_json TEXT NOT NULL,
  source_path TEXT,
  line INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(file_id, fact_key)
);
CREATE TABLE IF NOT EXISTS metadata_file_facts (
  file_id INTEGER NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  row_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(file_id, entity_kind, entity_id, row_order)
);
CREATE INDEX IF NOT EXISTS idx_node_facts_qname ON node_facts(qname);
CREATE INDEX IF NOT EXISTS idx_edge_facts_relation ON edge_facts(from_qname, to_qname, rel_type);
CREATE INDEX IF NOT EXISTS idx_metadata_facts_entity ON metadata_file_facts(entity_kind, entity_id);
"""


def ensure_db_schema(db: sqlite3.Connection) -> None:
    db.executescript(DDL)
    existing = {row[1] for row in db.execute("PRAGMA table_info(table_columns)").fetchall()}
    for name, ddl in {
        "code": "ALTER TABLE table_columns ADD COLUMN code TEXT",
        "name_ja": "ALTER TABLE table_columns ADD COLUMN name_ja TEXT",
        "name_en": "ALTER TABLE table_columns ADD COLUMN name_en TEXT",
    }.items():
        if name not in existing:
            db.execute(ddl)


def open_db(path: str | Path, reset: bool = False) -> sqlite3.Connection:
    if reset:
        raise ValueError("reset extraction is unsafe; use incremental rebuild")
    db = sqlite3.connect(path)
    ensure_db_schema(db)
    return db


def write_result(db: sqlite3.Connection, result: ExtractionResult) -> None:
    for node in result.nodes:
        qn = str(node.properties.get("qualified_name") or node.key_value)
        db.execute(
            """
            INSERT INTO nodes(label, qualified_name, name, properties_json)
            VALUES(?,?,?,?)
            ON CONFLICT(qualified_name) DO UPDATE SET
              label=excluded.label,
              name=excluded.name,
              properties_json=excluded.properties_json
            """,
            (node.label, qn, node.properties.get("name"), json.dumps(node.properties, ensure_ascii=False, default=str)),
        )
    for edge in result.edges:
        line = edge.properties.get("line")
        db.execute(
            """
            INSERT INTO edges(from_qname, to_qname, rel_type, properties_json, source_file, line)
            VALUES(?,?,?,?,?,?)
            """,
            (
                edge.from_key_value,
                edge.to_key_value,
                edge.rel_type,
                json.dumps(edge.properties, ensure_ascii=False, default=str),
                edge.properties.get("source_file"),
                line if isinstance(line, int) else None,
            ),
        )
    seed_table_columns(db)


def seed_table_columns(db: sqlite3.Connection) -> None:
    for qn, raw in db.execute("SELECT qualified_name, properties_json FROM nodes WHERE label='Column'").fetchall():
        props = json.loads(raw or "{}")
        table_name = props.get("table_name") or ""
        schema_name = props.get("schema") or ""
        if "." in table_name:
            schema_name, table_name = table_name.rsplit(".", 1)
        db.execute(
            """
            INSERT OR IGNORE INTO table_columns(id, db_name, schema_name, table_name, code, name, name_ja, name_en, description, comment)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (qn, props.get("db_name") or "", schema_name, table_name, props.get("name") or qn.rsplit(":", 1)[-1], props.get("name") or qn.rsplit(":", 1)[-1], "", "", "", ""),
        )




def _assert_file_write(
    db: sqlite3.Connection,
    lease: Lease,
    run_id: int,
    file_id: int,
    expected_handler: str | None = None,
) -> None:
    if lease.run_id != run_id:
        raise ValueError("Writer run_id does not match lease owner")
    row = db.execute(
        "SELECT handler_name FROM extraction_files WHERE id=?", (file_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown extraction file_id: {file_id}")
    if expected_handler and row[0] != expected_handler:
        raise ValueError(
            f"File {file_id} handler must be {expected_handler}, got {row[0]}"
        )


def replace_file_result(
    db: sqlite3.Connection,
    *,
    lease: Lease,
    file_id: int,
    run_id: int,
    result: ExtractionResult,
    size: int,
    mtime_ns: int,
    sha256: str,
    rule_context_hash: str,
    handler_version: str,
    device: int | None = None,
    inode: int | None = None,
    ctime_ns: int | None = None,
    priority: int = 0,
) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        _assert_file_write(db, lease, run_id, file_id)
        old_nodes = {
            row[0]
            for row in db.execute(
                "SELECT qname FROM node_facts WHERE file_id=?", (file_id,)
            )
        }
        old_relations = {
            tuple(row)
            for row in db.execute(
                "SELECT DISTINCT from_qname,to_qname,rel_type FROM edge_facts WHERE file_id=?",
                (file_id,),
            )
        }
        db.execute("DELETE FROM node_facts WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM edge_facts WHERE file_id=?", (file_id,))

        nodes = _fold_file_nodes(result)
        for qname, node in nodes.items():
            properties = dict(node.properties)
            properties.setdefault("qualified_name", qname)
            db.execute(
                """
                INSERT INTO node_facts(file_id,qname,label,name,properties_json,source,priority)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    file_id,
                    qname,
                    node.label,
                    properties.get("name"),
                    _json(properties),
                    node.source,
                    priority,
                ),
            )

        edge_rows: dict[str, tuple[Any, ...]] = {}
        for edge in result.edges:
            properties = dict(edge.properties)
            source_path = str(
                properties.get("source_path") or properties.get("source_file") or ""
            )
            line = properties.get("line")
            line = line if isinstance(line, int) else None
            payload = _json(properties)
            fact_key = _digest(
                edge.from_key_value,
                edge.to_key_value,
                edge.rel_type,
                payload,
                source_path,
                str(line or ""),
            )
            edge_rows[fact_key] = (
                file_id,
                fact_key,
                edge.from_key_value,
                edge.to_key_value,
                edge.rel_type,
                payload,
                source_path,
                line,
                priority,
            )
        db.executemany(
            """
            INSERT INTO edge_facts(
              file_id,fact_key,from_qname,to_qname,rel_type,properties_json,
              source_path,line,priority
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            edge_rows.values(),
        )

        new_relations = {
            (row[2], row[3], row[4]) for row in edge_rows.values()
        }
        _materialize_nodes(db, old_nodes | set(nodes))
        _materialize_edges(db, old_relations | new_relations)
        db.execute(
            """
            UPDATE extraction_files SET
              successful_size=?, successful_mtime_ns=?, successful_device=?,
              successful_inode=?, successful_ctime_ns=?, successful_sha256=?,
              successful_rule_context_hash=?, successful_handler_version=?,
              status='success', last_success_run_id=?, node_count=?, edge_count=?,
              last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (
                size,
                mtime_ns,
                device,
                inode,
                ctime_ns,
                sha256,
                rule_context_hash,
                handler_version,
                run_id,
                len(nodes),
                len(edge_rows),
                utc_now(),
                file_id,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def delete_file_result(
    db: sqlite3.Connection, *, lease: Lease, file_id: int, run_id: int
) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        _assert_file_write(db, lease, run_id, file_id)
        affected_nodes = {
            row[0]
            for row in db.execute(
                "SELECT qname FROM node_facts WHERE file_id=?", (file_id,)
            )
        }
        affected_relations = {
            tuple(row)
            for row in db.execute(
                "SELECT DISTINCT from_qname,to_qname,rel_type FROM edge_facts WHERE file_id=?",
                (file_id,),
            )
        }
        metadata = [
            tuple(row)
            for row in db.execute(
                "SELECT DISTINCT entity_kind,entity_id FROM metadata_file_facts WHERE file_id=?",
                (file_id,),
            )
        ]
        db.execute("DELETE FROM node_facts WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM edge_facts WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM metadata_file_facts WHERE file_id=?", (file_id,))
        _materialize_nodes(db, affected_nodes)
        _materialize_edges(db, affected_relations)
        _materialize_metadata(db, metadata)
        db.execute(
            """
            UPDATE extraction_files SET status='deleted', last_success_run_id=?,
              node_count=0, edge_count=0, last_error=NULL, updated_at=? WHERE id=?
            """,
            (run_id, utc_now(), file_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def replace_metadata_file(
    db: sqlite3.Connection,
    *,
    lease: Lease,
    file_id: int,
    run_id: int,
    facts: Iterable[MetadataFact],
    size: int,
    mtime_ns: int,
    sha256: str,
    rule_context_hash: str,
    handler_version: str,
    device: int | None = None,
    inode: int | None = None,
    ctime_ns: int | None = None,
    priority: int = 0,
) -> None:
    rows = list(facts)
    for fact in rows:
        if fact.entity_kind not in {"table", "column"}:
            raise ValueError(f"Unsupported metadata entity kind: {fact.entity_kind}")
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        _assert_file_write(
            db, lease, run_id, file_id, expected_handler="table_definitions"
        )
        affected = {
            tuple(row)
            for row in db.execute(
                "SELECT DISTINCT entity_kind,entity_id FROM metadata_file_facts WHERE file_id=?",
                (file_id,),
            )
        }
        db.execute("DELETE FROM metadata_file_facts WHERE file_id=?", (file_id,))
        for fact in rows:
            db.execute(
                """
                INSERT INTO metadata_file_facts(
                  file_id,entity_kind,entity_id,payload_json,priority,row_order
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    file_id,
                    fact.entity_kind,
                    fact.entity_id,
                    _json(fact.payload),
                    priority,
                    fact.row_order,
                ),
            )
            affected.add((fact.entity_kind, fact.entity_id))
        _materialize_metadata(db, affected)
        db.execute(
            """
            UPDATE extraction_files SET
              successful_size=?, successful_mtime_ns=?, successful_device=?,
              successful_inode=?, successful_ctime_ns=?, successful_sha256=?,
              successful_rule_context_hash=?, successful_handler_version=?,
              status='success', last_success_run_id=?, node_count=?, edge_count=0,
              last_error=NULL, updated_at=? WHERE id=?
            """,
            (
                size,
                mtime_ns,
                device,
                inode,
                ctime_ns,
                sha256,
                rule_context_hash,
                handler_version,
                run_id,
                len(rows),
                utc_now(),
                file_id,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _fold_file_nodes(result: ExtractionResult) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for node in result.nodes:
        qname = str(node.properties.get("qualified_name") or node.key_value)
        existing = nodes.get(qname)
        if not existing:
            nodes[qname] = node
            continue
        merged = dict(existing.properties)
        merged.update({key: value for key, value in node.properties.items() if value not in (None, "")})
        existing.properties = merged
    return nodes


def _materialize_nodes(db: sqlite3.Connection, qnames: Iterable[str]) -> None:
    for qname in sorted(set(qnames)):
        rows = db.execute(
            """
            SELECT label,name,properties_json,source,priority,file_id
            FROM node_facts WHERE qname=? ORDER BY priority DESC,file_id ASC
            """,
            (qname,),
        ).fetchall()
        if not rows:
            referenced = db.execute(
                "SELECT 1 FROM edge_facts WHERE from_qname=? OR to_qname=? LIMIT 1",
                (qname, qname),
            ).fetchone()
            current = db.execute(
                "SELECT properties_json FROM nodes WHERE qualified_name=?", (qname,)
            ).fetchone()
            if not referenced and current:
                properties = json.loads(current[0] or "{}")
                if properties.get("_fact_materialized"):
                    db.execute("DELETE FROM nodes WHERE qualified_name=?", (qname,))
            continue
        label, name, raw, source, _, _ = rows[0]
        properties = json.loads(raw or "{}")
        properties["_fact_materialized"] = True
        properties["evidence_count"] = len(rows)
        db.execute(
            """
            INSERT INTO nodes(label,qualified_name,name,properties_json)
            VALUES(?,?,?,?)
            ON CONFLICT(qualified_name) DO UPDATE SET
              label=excluded.label,name=excluded.name,properties_json=excluded.properties_json
            """,
            (label, qname, name, _json(properties)),
        )


def _materialize_edges(
    db: sqlite3.Connection, relations: Iterable[tuple[str, str, str]]
) -> None:
    evidence_keys = {
        "source_file",
        "source_path",
        "source_id",
        "line",
        "extractor_name",
        "evidence",
        "evidence_count",
        "evidence_truncated",
    }
    for from_qname, to_qname, rel_type in sorted(set(relations)):
        db.execute(
            "DELETE FROM edges WHERE from_qname=? AND to_qname=? AND rel_type=?",
            (from_qname, to_qname, rel_type),
        )
        facts = db.execute(
            """
            SELECT e.properties_json,e.source_path,e.line,e.priority,
                   f.source_id,e.file_id
            FROM edge_facts e
            JOIN extraction_files f ON f.id=e.file_id
            WHERE e.from_qname=? AND e.to_qname=? AND e.rel_type=?
            ORDER BY e.priority DESC,f.source_id ASC,e.source_path ASC,
                     e.line ASC,e.fact_key ASC
            """,
            (from_qname, to_qname, rel_type),
        ).fetchall()
        if not facts:
            continue
        canonical: dict[str, Any] = {}
        columns: set[str] = set()
        evidence: list[dict[str, Any]] = []
        for raw, source_path, line, _, source_id, _ in facts:
            occurrence = json.loads(raw or "{}")
            for key, value in occurrence.items():
                if key not in evidence_keys and value not in (None, "", []):
                    canonical.setdefault(key, value)
            columns.update(str(value) for value in occurrence.get("columns") or [])
            item = {
                key: value
                for key, value in occurrence.items()
                if key not in {"source_file", "source_path", "evidence"}
                and value not in (None, "")
            }
            item["source_id"] = source_id
            item["source_path"] = source_path or ""
            if line is not None:
                item["line"] = line
            evidence.append(item)
        if columns:
            canonical["columns"] = sorted(columns)
        first_path = str(facts[0][1] or "")
        first_line = facts[0][2]
        canonical["evidence"] = evidence[:100]
        canonical["evidence_count"] = len(evidence)
        canonical["evidence_truncated"] = len(evidence) > 100
        canonical["source_file"] = first_path
        if first_line is not None:
            canonical["line"] = first_line
        db.execute(
            """
            INSERT INTO edges(from_qname,to_qname,rel_type,properties_json,source_file,line)
            VALUES(?,?,?,?,?,?)
            """,
            (
                from_qname,
                to_qname,
                rel_type,
                _json(canonical),
                first_path,
                first_line,
            ),
        )


def _materialize_metadata(
    db: sqlite3.Connection, entities: Iterable[tuple[str, str]]
) -> None:
    for kind, entity_id in sorted(set(entities)):
        if kind not in {"table", "column"}:
            raise ValueError(f"Unsupported metadata entity kind: {kind}")
        table = "table_definitions" if kind == "table" else "table_columns"
        rows = db.execute(
            """
            SELECT m.payload_json,m.priority,m.row_order,f.source_id
            FROM metadata_file_facts m
            JOIN extraction_files f ON f.id=m.file_id
            WHERE m.entity_kind=? AND m.entity_id=?
            ORDER BY m.priority ASC,f.source_id ASC,f.relative_path ASC,
                     m.row_order ASC
            """,
            (kind, entity_id),
        ).fetchall()
        if not rows:
            db.execute(f"DELETE FROM {table} WHERE id=?", (entity_id,))
            continue
        payload: dict[str, Any] = {}
        for raw, _, _, _ in rows:
            payload.update(
                {key: value for key, value in json.loads(raw).items() if value not in (None, "")}
            )
        if kind == "table":
            db.execute(
                """
                INSERT INTO table_definitions(id,db_name,schema_name,table_name,code,name_ja,name_en,description)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  db_name=excluded.db_name,schema_name=excluded.schema_name,
                  table_name=excluded.table_name,code=excluded.code,
                  name_ja=excluded.name_ja,name_en=excluded.name_en,
                  description=excluded.description
                """,
                (
                    entity_id,
                    payload.get("db_name", ""),
                    payload.get("schema_name", ""),
                    payload.get("table_name", ""),
                    payload.get("code", ""),
                    payload.get("name_ja", ""),
                    payload.get("name_en", ""),
                    payload.get("description", ""),
                ),
            )
        else:
            db.execute(
                """
                INSERT INTO table_columns(id,db_name,schema_name,table_name,code,name,name_ja,name_en,description,comment)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  db_name=excluded.db_name,schema_name=excluded.schema_name,
                  table_name=excluded.table_name,code=excluded.code,name=excluded.name,
                  name_ja=excluded.name_ja,name_en=excluded.name_en,
                  description=excluded.description,comment=excluded.comment
                """,
                (
                    entity_id,
                    payload.get("db_name", ""),
                    payload.get("schema_name", ""),
                    payload.get("table_name", ""),
                    payload.get("code", ""),
                    payload.get("name", ""),
                    payload.get("name_ja", ""),
                    payload.get("name_en", ""),
                    payload.get("description", ""),
                    payload.get("comment", ""),
                ),
            )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
