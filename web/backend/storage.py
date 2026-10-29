"""Opening the extract output and the knowledge database, without Flask or .env.

The web API (``graph_routes.connection``) and the MCP server (stdio or HTTP)
share this: ``codemap-web.sqlite`` is opened read-only per use, so a new
extract can replace it, and the knowledge database people write to is
attached as ``kn`` with every schema created on first use.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from web.backend.api.business import KNOWLEDGE_SCHEMA as BUSINESS_SCHEMA

WEB_DATABASE_NAME = "codemap-web.sqlite"
RAG_DATABASE_NAME = "codemap-rag.sqlite"
# meta.schema_version written by code_tree_exporter/rag_export.py (RAG_SCHEMA_VERSION).
WEB_SCHEMA_VERSION = "2"

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

# Further schemas (the knowledge layer) register here so every opener creates them.
EXTRA_SCHEMAS: list[str] = [MCP_LOG_SCHEMA]


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


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


@contextmanager
def open_web(output: Path, knowledge: Path) -> Iterator[sqlite3.Connection]:
    """codemap-web.sqlite (read-only) with the knowledge database attached as ``kn``."""
    path = output / WEB_DATABASE_NAME
    if not path.is_file():
        raise GraphNotReady(path)
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if _has_table(db, "meta") else None
        if not version or version[0] != WEB_SCHEMA_VERSION:
            raise GraphNotReady(path, f"{path} was written by an older extract; run the extract (or rag-export) again")
        knowledge.parent.mkdir(parents=True, exist_ok=True)
        db.execute("ATTACH DATABASE ? AS kn", (str(knowledge),))
        db.executescript(GRAPH_KNOWLEDGE_SCHEMA + BUSINESS_SCHEMA + "".join(EXTRA_SCHEMAS))
        yield db
    finally:
        db.close()


@contextmanager
def open_rag(output: Path) -> Iterator[sqlite3.Connection]:
    """codemap-rag.sqlite (read-only): the graph shaped for retrieval."""
    path = output / RAG_DATABASE_NAME
    if not path.is_file():
        raise GraphNotReady(path)
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield db
    finally:
        db.close()
