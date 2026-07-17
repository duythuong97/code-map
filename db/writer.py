from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from db.entities import ExtractionResult

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
    db = sqlite3.connect(path)
    ensure_db_schema(db)
    if reset:
        db.executescript("DELETE FROM edges; DELETE FROM nodes;")
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
