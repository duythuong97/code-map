"""Opening the extract output and the knowledge database, without Flask or .env.

The web API (``graph_routes.connection``) and the MCP server (stdio or HTTP)
share this: ``codemap.sqlite`` is opened read-only per use, so a new
extract can replace it, and the knowledge database people write to is
attached as ``kn`` with every schema created on first use.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from code_tree_exporter.serving import DATABASE_NAME as SERVING_DATABASE_NAME
from code_tree_exporter.serving import SCHEMA_VERSION as SERVING_SCHEMA_VERSION

from codemap.business import KNOWLEDGE_SCHEMA as BUSINESS_SCHEMA

GRAPH_KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kn.graph_knowledge(knowledge_id INTEGER PRIMARY KEY,target_id TEXT NOT NULL,body TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('draft','pending','approved','rejected')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS kn.graph_knowledge_target ON graph_knowledge(target_id);
CREATE TABLE IF NOT EXISTS kn.semantic_mappings(target_id TEXT PRIMARY KEY,semantic_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('approved','rejected')),source_kind TEXT NOT NULL DEFAULT 'USER',
  author_name TEXT NOT NULL DEFAULT '',confidence REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TEMP VIEW graph_knowledge AS SELECT * FROM kn.graph_knowledge;
-- A person's mapping wins over the dictionary's alias links for the same node.
CREATE TEMP VIEW graph_business_semantic_mappings AS
  SELECT target_id,semantic_id,status,source_kind,author_name,confidence,created_at,updated_at FROM kn.semantic_mappings
  UNION ALL
  SELECT l.target_id,l.semantic_id,'approved','DICTIONARY_ALIAS','',0.9,'',''
  FROM main.graph_business_semantic_links l
  WHERE NOT EXISTS (SELECT 1 FROM kn.semantic_mappings m WHERE m.target_id=l.target_id);
"""

# Every MCP tool call, so the team can see what agents ask and where the graph has no answer.
MCP_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS kn.mcp_calls(call_id INTEGER PRIMARY KEY,called_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  user_name TEXT NOT NULL DEFAULT '',client TEXT NOT NULL DEFAULT '',tool TEXT NOT NULL,arguments_json TEXT NOT NULL,
  result_chars INTEGER NOT NULL,empty INTEGER NOT NULL,error TEXT NOT NULL DEFAULT '',elapsed_ms REAL NOT NULL);
CREATE INDEX IF NOT EXISTS kn.mcp_calls_tool ON mcp_calls(tool, called_at);
"""

# Business rules, decisions and questions people own (web/backend/knowledge.py).
KNOWLEDGE_LAYER_SCHEMA = """
CREATE TABLE IF NOT EXISTS kn.biz_rules(rule_id TEXT PRIMARY KEY,domain TEXT NOT NULL,title TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',status TEXT NOT NULL CHECK(status IN('draft','approved','needs_review','retired')),
  source TEXT NOT NULL CHECK(source IN('code','sme','doc','ai')),evidence TEXT NOT NULL DEFAULT '',
  node_ids TEXT NOT NULL DEFAULT '[]',requirement_refs TEXT NOT NULL DEFAULT '[]',test_refs TEXT NOT NULL DEFAULT '[]',
  fingerprint TEXT NOT NULL DEFAULT '',review_reason TEXT NOT NULL DEFAULT '',proposed_by TEXT NOT NULL DEFAULT '',
  approved_by TEXT NOT NULL DEFAULT '',approved_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS kn.biz_decisions(decision_id TEXT PRIMARY KEY,title TEXT NOT NULL,context TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]',decision TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN('proposed','accepted','rejected','superseded')),node_ids TEXT NOT NULL DEFAULT '[]',
  rule_ids TEXT NOT NULL DEFAULT '[]',superseded_by TEXT NOT NULL DEFAULT '',proposed_by TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL DEFAULT '',decided_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS kn.biz_questions(question_id TEXT PRIMARY KEY,text TEXT NOT NULL,context TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]',answer TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN('open','answered','dropped')),node_ids TEXT NOT NULL DEFAULT '[]',
  rule_ids TEXT NOT NULL DEFAULT '[]',flow_id TEXT NOT NULL DEFAULT '',asked_by TEXT NOT NULL DEFAULT '',
  answered_by TEXT NOT NULL DEFAULT '',answered_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS kn.knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""

EXTRA_SCHEMAS: list[str] = [MCP_LOG_SCHEMA, KNOWLEDGE_LAYER_SCHEMA]

# Wall-clock seconds one request (web) or tool call (MCP) may spend in codemap.sqlite. Past it the next
# SQL step fails with "interrupted" instead of holding one of the server's few threads.
QUERY_SECONDS = float(os.environ.get("CODE_MAP_QUERY_SECONDS", "30"))
# SQLite steps between two looks at the clock.
PROGRESS_OPCODES = 10_000


def timed_out(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and str(exc) == "interrupted"


def set_deadline(db: sqlite3.Connection) -> None:
    """Interrupt SQL on ``db`` once QUERY_SECONDS have passed from now."""
    deadline = time.monotonic() + QUERY_SECONDS
    db.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_OPCODES)


def clear_deadline(db: sqlite3.Connection) -> None:
    """No time limit on ``db`` (a load done once per extract, not per request)."""
    db.set_progress_handler(None, 0)


@contextmanager
def open_knowledge(knowledge: Path) -> Iterator[sqlite3.Connection]:
    """The knowledge database alone (attached as ``kn`` like in open_web), for writes that need no graph."""
    knowledge.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("ATTACH DATABASE ? AS kn", (str(knowledge),))
        db.executescript(BUSINESS_SCHEMA + "".join(EXTRA_SCHEMAS))
        yield db
    finally:
        db.close()


class GraphNotReady(Exception):
    def __init__(self, path: Path, message: str = "") -> None:
        super().__init__(message or f"{path} not found")
        self.message = message


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _open_serving(output: Path) -> sqlite3.Connection:
    """codemap.sqlite read-only, refused when an older extract wrote it (or wrote the old two files)."""
    path = output / SERVING_DATABASE_NAME
    if not path.is_file():
        if (output / "codemap-web.sqlite").is_file():
            raise GraphNotReady(path, f"{output} was written by an older extract (codemap-web.sqlite); run the extract again")
        raise GraphNotReady(path)
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if _has_table(db, "meta") else None
    if not version or version[0] != SERVING_SCHEMA_VERSION:
        db.close()
        raise GraphNotReady(path, f"{path} was written by an older extract; run the extract (or serving-export) again")
    set_deadline(db)
    return db


@contextmanager
def open_web(output: Path, knowledge: Path) -> Iterator[sqlite3.Connection]:
    """codemap.sqlite (read-only) with the knowledge database attached as ``kn``."""
    db = _open_serving(output)
    db.row_factory = sqlite3.Row
    try:
        knowledge.parent.mkdir(parents=True, exist_ok=True)
        db.execute("ATTACH DATABASE ? AS kn", (str(knowledge),))
        db.executescript(GRAPH_KNOWLEDGE_SCHEMA + BUSINESS_SCHEMA + "".join(EXTRA_SCHEMAS))
        yield db
    finally:
        db.close()


@contextmanager
def open_search(output: Path) -> Iterator[sqlite3.Connection]:
    """codemap.sqlite (read-only) for the search index (search_* tables), without the knowledge db."""
    db = _open_serving(output)
    try:
        yield db
    finally:
        db.close()
