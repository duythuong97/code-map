"""CSV-first database import, resolver and flow materialization pipeline."""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable

from application.backend.importer.package_validator import CSV_HEADERS, validate_package
from contract.graph_contract import canonical_edge_id, column_id, normalize_http_route, normalize_oracle_identifier, table_id

SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS systems(system_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS databases(database_id TEXT PRIMARY KEY,system_id TEXT,technical_name TEXT NOT NULL,qualified_name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS repositories(repository_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS processing_runs(run_id TEXT PRIMARY KEY,run_status TEXT NOT NULL CHECK(run_status IN('RUNNING','COMPLETED','COMPLETED_WITH_ERRORS','FAILED','INTERRUPTED')),started_at TEXT NOT NULL,completed_at TEXT,package_count INTEGER NOT NULL DEFAULT 0,node_count INTEGER NOT NULL DEFAULT 0,edge_count INTEGER NOT NULL DEFAULT 0,evidence_count INTEGER NOT NULL DEFAULT 0,issue_count INTEGER NOT NULL DEFAULT 0,warning_count INTEGER NOT NULL DEFAULT 0,error_count INTEGER NOT NULL DEFAULT 0,properties_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(properties_json)));
CREATE TABLE IF NOT EXISTS import_packages(run_id TEXT NOT NULL REFERENCES processing_runs(run_id) ON DELETE CASCADE,source_id TEXT NOT NULL,package_id TEXT NOT NULL,contract_version TEXT NOT NULL,source_created_at TEXT NOT NULL,import_status TEXT NOT NULL CHECK(import_status IN('RUNNING','COMPLETED','COMPLETED_WITH_ERRORS','FAILED','INTERRUPTED')),nodes_count INTEGER NOT NULL DEFAULT 0,edges_count INTEGER NOT NULL DEFAULT 0,evidence_count INTEGER NOT NULL DEFAULT 0,issues_count INTEGER NOT NULL DEFAULT 0,warnings_count INTEGER NOT NULL DEFAULT 0,errors_count INTEGER NOT NULL DEFAULT 0,manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(run_id,source_id));
CREATE TABLE IF NOT EXISTS source_artifacts(artifact_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_path TEXT NOT NULL,artifact_kind TEXT NOT NULL DEFAULT 'SOURCE_FILE',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(source_id,source_path));
CREATE TABLE IF NOT EXISTS graph_sources(source_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_nodes(node_id TEXT PRIMARY KEY,node_type TEXT NOT NULL,technical_name TEXT NOT NULL,qualified_name TEXT NOT NULL,default_display_name TEXT NOT NULL,system_key TEXT,database_key TEXT,repository_key TEXT,graph_role TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_nodes_search ON graph_nodes(default_display_name,technical_name,node_type);
CREATE INDEX IF NOT EXISTS graph_nodes_type ON graph_nodes(node_type);
CREATE INDEX IF NOT EXISTS graph_nodes_database_type ON graph_nodes(database_key,node_type);
CREATE INDEX IF NOT EXISTS graph_nodes_technical_name ON graph_nodes(technical_name);
CREATE TABLE IF NOT EXISTS graph_edges(edge_id TEXT PRIMARY KEY,source_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),target_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),edge_type TEXT NOT NULL,graph_layer TEXT NOT NULL,raw_operation TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_edges_source ON graph_edges(source_node_id,edge_type);
CREATE INDEX IF NOT EXISTS graph_edges_target ON graph_edges(target_node_id,edge_type);
CREATE INDEX IF NOT EXISTS graph_edges_source_layer ON graph_edges(source_node_id,edge_type,graph_layer);
CREATE INDEX IF NOT EXISTS graph_edges_target_layer ON graph_edges(target_node_id,edge_type,graph_layer);
CREATE UNIQUE INDEX IF NOT EXISTS graph_edges_identity ON graph_edges(source_node_id,target_node_id,edge_type,graph_layer,raw_operation);
CREATE TABLE IF NOT EXISTS graph_evidence(evidence_id TEXT PRIMARY KEY,target_type TEXT NOT NULL CHECK(target_type IN('NODE','EDGE')),target_id TEXT NOT NULL,source_path TEXT NOT NULL,start_line INTEGER CHECK(start_line IS NULL OR start_line>0),end_line INTEGER,start_column INTEGER,end_column INTEGER,evidence_kind TEXT NOT NULL,extractor_name TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),snippet TEXT NOT NULL,properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_evidence_target ON graph_evidence(target_type,target_id,evidence_id);
CREATE INDEX IF NOT EXISTS graph_evidence_artifact ON graph_evidence(source_path);
CREATE TABLE IF NOT EXISTS graph_issues(issue_id TEXT PRIMARY KEY,issue_type TEXT NOT NULL,severity TEXT NOT NULL,source_node_id TEXT,raw_reference TEXT,database_key TEXT,source_path TEXT,start_line INTEGER,message TEXT NOT NULL,properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_issues_source ON graph_issues(source_node_id,issue_type);
CREATE TABLE IF NOT EXISTS graph_paths(path_id TEXT PRIMARY KEY,actor_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),table_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),operation TEXT NOT NULL CHECK(operation IN('R','W')),edge_path_json TEXT NOT NULL CHECK(json_valid(edge_path_json)),evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_paths_actor ON graph_paths(actor_node_id,operation,table_node_id);
CREATE TABLE IF NOT EXISTS graph_localization(target_type TEXT NOT NULL,target_id TEXT NOT NULL,field_name TEXT NOT NULL,locale TEXT NOT NULL,value TEXT NOT NULL,source_kind TEXT NOT NULL,review_status TEXT NOT NULL,author_name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,source_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(target_id,field_name,locale));
CREATE INDEX IF NOT EXISTS graph_localization_lookup ON graph_localization(target_type,target_id,locale);
CREATE TABLE IF NOT EXISTS graph_executable_mappings(job_system TEXT NOT NULL,executable_name TEXT NOT NULL,executable_scope TEXT NOT NULL,canonical_executable_name TEXT NOT NULL,alias TEXT NOT NULL,PRIMARY KEY(job_system,executable_name,executable_scope));
CREATE TABLE IF NOT EXISTS graph_business_semantics(semantic_id TEXT NOT NULL,locale TEXT NOT NULL,domain TEXT NOT NULL,label TEXT NOT NULL,definition TEXT NOT NULL,aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json)),status TEXT NOT NULL CHECK(status IN('draft','approved','deprecated')),source_kind TEXT NOT NULL DEFAULT 'IMPORTED',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(semantic_id,locale));
CREATE INDEX IF NOT EXISTS graph_business_semantics_lookup ON graph_business_semantics(locale,status,label);
CREATE TABLE IF NOT EXISTS graph_business_semantic_mappings(target_id TEXT PRIMARY KEY,semantic_id TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN('approved','rejected')),source_kind TEXT NOT NULL DEFAULT 'USER',author_name TEXT NOT NULL DEFAULT '',confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS graph_business_semantic_mappings_semantic ON graph_business_semantic_mappings(semantic_id,status);
CREATE TABLE IF NOT EXISTS graph_knowledge(knowledge_id INTEGER PRIMARY KEY,target_id TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN('draft','pending','approved','rejected')),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS table_details(node_id TEXT PRIMARY KEY REFERENCES graph_nodes(node_id) ON DELETE CASCADE,database_id TEXT NOT NULL,table_code TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS column_details(node_id TEXT PRIMARY KEY REFERENCES graph_nodes(node_id) ON DELETE CASCADE,table_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,column_code TEXT NOT NULL,ordinal_position INTEGER,data_type TEXT,nullable INTEGER CHECK(nullable IN(0,1)),length_value INTEGER,precision_value INTEGER,scale_value INTEGER,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS column_details_table ON column_details(table_node_id,ordinal_position);
CREATE TABLE IF NOT EXISTS resolution_issues(issue_id TEXT PRIMARY KEY,issue_type TEXT NOT NULL,severity TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',source_node_id TEXT,raw_reference TEXT,database_key TEXT,source_path TEXT,start_line INTEGER,message TEXT NOT NULL,properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS resolution_issues_status_type ON resolution_issues(status,issue_type);
"""
COMPAT_SCHEMA = """
CREATE VIEW IF NOT EXISTS localized_texts AS
SELECT target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at FROM graph_localization;
CREATE VIEW IF NOT EXISTS knowledge_entries AS
SELECT knowledge_id,target_id,body,status,created_at,updated_at FROM graph_knowledge;
CREATE VIEW IF NOT EXISTS node_evidence AS
SELECT evidence_id,target_id AS node_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json,source_id FROM graph_evidence WHERE target_type='NODE';
CREATE VIEW IF NOT EXISTS edge_evidence AS
SELECT evidence_id,target_id AS edge_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json,source_id FROM graph_evidence WHERE target_type='EDGE';
CREATE VIEW IF NOT EXISTS edge_paths AS
SELECT path_id,actor_node_id,table_node_id,operation,edge_path_json,evidence_ids_json,source_id FROM graph_paths;
"""
AUTH_SOURCE = "authoritative:input-data"
RESOLVER_SOURCE = "resolver:cross-source"
RUN_TERMINAL_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "INTERRUPTED"}
READ_TERMINALS = {"READS", "REMOTE_READS"}
WRITE_TERMINALS = {"INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES"}
CRUD_TERMINALS = READ_TERMINALS | WRITE_TERMINALS
MATERIALIZER_INTERMEDIATE_EDGES = {"CALLS_API", "STARTS", "CALLS", "HANDLED_BY", "RESOLVES_TO", "TRIGGERS", "ENTRY_IN"}
MATERIALIZER_ACTOR_TYPES = {"SCREEN", "UI_ACTION", "API_OPERATION", "EXECUTABLE", "PROCEDURE", "FUNCTION", "TRIGGER", "SQL_FILE", "JOB"}
ROUTINE_NODE_TYPES = {"PROCEDURE", "FUNCTION"}
API_EXPLICIT_MAPPING_KEYS = {
    "apiOperationId",
    "api_operation_id",
    "targetApiOperationId",
    "target_api_operation_id",
    "target_node_id",
    "resolvedOperationId",
    "resolved_operation_id",
    "operationId",
    "operation_id",
}

def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    _migrate_schema(db)
    _sync_resolution_issues(db)
    db.executescript(COMPAT_SCHEMA)

def _migrate_schema(db: sqlite3.Connection) -> None:
    if not _has_column(db,"graph_localization","created_at"):
        db.execute("ALTER TABLE graph_localization ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE graph_localization SET created_at=datetime('now') WHERE created_at=''")
    if not _has_column(db,"graph_localization","updated_at"):
        db.execute("ALTER TABLE graph_localization ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE graph_localization SET updated_at=created_at WHERE updated_at=''")
    if not _has_column(db,"graph_localization","source_id"):
        db.execute("ALTER TABLE graph_localization ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE graph_localization SET source_id=? WHERE source_id=''",(AUTH_SOURCE,))
    if not _has_column(db,"graph_paths","evidence_ids_json"):
        db.execute("ALTER TABLE graph_paths ADD COLUMN evidence_ids_json TEXT NOT NULL DEFAULT '[]'")
    if not _has_column(db,"graph_knowledge","updated_at"):
        db.execute("ALTER TABLE graph_knowledge ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE graph_knowledge SET updated_at=created_at WHERE updated_at=''")

def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))

def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))

def _table_catalog(root: Path) -> list[tuple[dict[str, str], list[dict[str, str]]]]:
    result = []
    for table in _read(root / "tables.csv"):
        path = root / "tables" / f"{table['table_code']}.csv"
        result.append((table, _read(path) if path.is_file() else []))
    return result

def _node_values(node_id: str, node_type: str, name: str, qname: str, database: str = "", props: dict | None = None):
    return (node_id,node_type,name,qname,name,"",database,"","MAIN",1.0,json.dumps(props or {},sort_keys=True),AUTH_SOURCE)

def _qualified_table_name(database: str, table_code: str) -> str:
    return f"{database}.{table_code}"

def _qualified_column_name(database: str, table_code: str, column_code: str) -> str:
    return f"{database}.{table_code}.{column_code}"

def _optional_int(row: dict[str,str], key: str) -> int | None:
    value=str(row.get(key,"")).strip()
    return int(value) if value else None

def _bool01(value: str) -> int:
    return 1 if str(value).strip().lower() in {"1","true","t","yes","y"} else 0

def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

def _import_names(db: sqlite3.Connection, target_type: str, target_id: str, ja: str, en: str) -> None:
    now = _now()
    for locale, value in (("ja", ja), ("en", en)):
        if value.strip():
            db.execute(
                "INSERT OR REPLACE INTO graph_localization(target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at,source_id) VALUES(?,?,?,?,?,'IMPORTED','approved','tables',?,?,?)",
                (target_type,target_id,"name",locale,value.strip(),now,now,AUTH_SOURCE),
            )

def import_authoritative(db: sqlite3.Connection, root: Path) -> None:
    tables = _table_catalog(root); jobs = _read(root / "jobnet.csv")
    mappings = _read(root / "executable-mappings.csv"); locales = _read(root / "localized-metadata.csv")
    semantics_path = root / "semantic-dictionary.csv"
    semantics = _read(semantics_path) if semantics_path.is_file() else []
    db.execute("INSERT OR REPLACE INTO graph_sources VALUES(?,?,datetime('now'))",(AUTH_SOURCE,"authoritative",))
    db.execute("DELETE FROM graph_localization WHERE source_kind='IMPORTED'")
    db.execute("DELETE FROM graph_business_semantics WHERE source_kind='IMPORTED'")
    now = _now()
    for row in semantics:
        aliases = [value.strip() for value in str(row.get("aliases") or "").split("|") if value.strip()]
        semantic_id = str(row.get("semantic_id") or "").strip()
        locale = str(row.get("locale") or "en").strip().lower()
        status = str(row.get("status") or "approved").strip().lower()
        if not re.fullmatch(r"semantic:[a-z0-9][a-z0-9:_-]*", semantic_id) or status not in {"draft", "approved", "deprecated"}:
            raise ValueError(f"invalid semantic dictionary row: {semantic_id or '<missing>'}")
        db.execute(
            """
            INSERT INTO graph_business_semantics(semantic_id,locale,domain,label,definition,aliases_json,status,source_kind,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'IMPORTED',?,?)
            ON CONFLICT(semantic_id,locale) DO UPDATE SET domain=excluded.domain,label=excluded.label,
              definition=excluded.definition,aliases_json=excluded.aliases_json,status=excluded.status,updated_at=excluded.updated_at
            """,
            (semantic_id,locale,str(row.get("domain") or "").strip(),str(row.get("label") or "").strip(),str(row.get("definition") or "").strip(),json.dumps(aliases,ensure_ascii=False),status,now,now),
        )
    for table, columns in tables:
        database=normalize_oracle_identifier(table['database']); table_code=normalize_oracle_identifier(table['table_code'])
        tid=table_id(database,table_code)
        db.execute("INSERT OR REPLACE INTO databases(database_id,system_id,technical_name,qualified_name,updated_at) VALUES(?,?,?,?,datetime('now'))",(database,"",database,database))
        db.execute("INSERT OR REPLACE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",_node_values(tid,"TABLE",table_code,_qualified_table_name(database,table_code),database))
        db.execute("INSERT OR REPLACE INTO table_details(node_id,database_id,table_code,updated_at) VALUES(?,?,?,datetime('now'))",(tid,database,table_code))
        _import_names(db,"NODE",tid,table.get("table_name_ja",""),table.get("table_name_en",""))
        for row in columns:
            column_code=normalize_oracle_identifier(row['column_code']); cid=column_id(database,table_code,column_code)
            column_props={**row,"database":database,"table_code":table_code,"column_code":column_code}
            db.execute("INSERT OR REPLACE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",_node_values(cid,"COLUMN",column_code,_qualified_column_name(database,table_code,column_code),database,column_props))
            _import_names(db,"NODE",cid,row.get("column_name_ja",""),row.get("column_name_en",""))
            db.execute(
                """
                INSERT OR REPLACE INTO column_details(node_id,table_node_id,column_code,ordinal_position,data_type,nullable,length_value,precision_value,scale_value,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))
                """,
                (cid,tid,column_code,_optional_int(row,"ordinal_position"),str(row.get("data_type","")).strip(),_bool01(row.get("nullable","")),_optional_int(row,"length_value"),_optional_int(row,"precision_value"),_optional_int(row,"scale_value")),
            )
            edge_id=canonical_edge_id(tid,"CONTAINS",cid,"","STRUCTURAL")
            db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(edge_id,tid,cid,"CONTAINS","STRUCTURAL","",1.0,"{}",AUTH_SOURCE))
    job_ids: dict[tuple[str,str], str] = {}
    for row in jobs:
        network=f"job-network:batch-system:{row['jobnet_id']}"; job=f"job:batch-system:{row['jobnet_id']}:{row['job_id']}"
        job_ids[(row['jobnet_id'],row['job_id'])]=job
        db.execute("INSERT OR IGNORE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",_node_values(network,"JOB_NETWORK",row['jobnet_name'],network))
        job_props={**row,"job_system":"batch-system","executable_scope":"batch-system"}
        db.execute("INSERT OR REPLACE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",_node_values(job,"JOB",row['job_name'],job,props=job_props))
        edge_id=canonical_edge_id(network,"CONTAINS",job,"","STRUCTURAL")
        db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(edge_id,network,job,"CONTAINS","STRUCTURAL","",1.0,"{}",AUTH_SOURCE))
    for row in jobs:
        predecessor=str(row.get("predecessor_job_id","")).strip()
        if not predecessor:
            continue
        job=job_ids[(row['jobnet_id'],row['job_id'])]; predecessor_job=job_ids.get((row['jobnet_id'],predecessor))
        if predecessor_job:
            edge_id=canonical_edge_id(job,"DEPENDS_ON",predecessor_job,"","STRUCTURAL")
            db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(edge_id,job,predecessor_job,"DEPENDS_ON","STRUCTURAL","",1.0,"{}",AUTH_SOURCE))
    db.execute("DELETE FROM graph_executable_mappings")
    db.executemany("INSERT OR REPLACE INTO graph_executable_mappings VALUES(:job_system,:executable_name,:executable_scope,:canonical_executable_name,:alias)",mappings)
    for row in locales:
        created=row.get("created_at") or _now(); updated=row.get("updated_at") or created
        db.execute(
            """
            INSERT OR REPLACE INTO graph_localization(target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at,source_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (row["target_type"],row["target_id"],row["field_name"],row["locale"],row["value"],row["source_kind"],row["review_status"],row["author_name"],created,updated,AUTH_SOURCE),
        )

def publish(db: sqlite3.Connection, package: dict[str, object]) -> None:
    manifest=package["manifest"]; source=manifest["sourceId"]
    db.execute("DELETE FROM graph_sources WHERE source_id=?",(RESOLVER_SOURCE,))
    db.execute("DELETE FROM source_artifacts WHERE source_id=?",(source,))
    db.execute("DELETE FROM graph_sources WHERE source_id=?",(source,))
    db.execute("INSERT INTO graph_sources VALUES(?,?,?)",(source,manifest["packageId"],manifest["createdAt"]))
    for row in package["nodes"]: db.execute("INSERT OR IGNORE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(*row.values(),source))
    for row in package["edges"]: db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(*row.values(),source))
    for row in package["evidence"]:
        values=list(row.values()); values[4:8]=[int(x) if x else None for x in values[4:8]]
        db.execute("INSERT INTO graph_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(*values,source))
        _upsert_source_artifact(db,source,values[3])
    for row in package["issues"]:
        values=list(row.values()); values[7]=int(values[7]) if values[7] else None
        _insert_issue(db,*values,source)
    db.execute("DELETE FROM graph_localization WHERE source_id=?",(source,))
    for row in package.get("localized_texts",[]):
        created=row.get("created_at") or _now(); updated=row.get("updated_at") or created
        db.execute(
            """
            INSERT OR REPLACE INTO graph_localization(target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at,source_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (row["target_type"],row["target_id"],row["field_name"],row["locale"],row["value"],row["source_kind"],row["review_status"],row["author_name"],created,updated,source),
        )

def _upsert_source_artifact(db: sqlite3.Connection, source_id: str, source_path: str) -> None:
    artifact_id="artifact:"+hashlib.sha256(f"{source_id}|{source_path}".encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO source_artifacts(artifact_id,source_id,source_path) VALUES(?,?,?)",(artifact_id,source_id,source_path))

def _insert_issue(db: sqlite3.Connection, issue_id: str, issue_type: str, severity: str, source_node_id: str | None, raw_reference: str | None, database_key: str | None, source_path: str | None, start_line: int | None, message: str, properties_json: str, source_id: str) -> None:
    db.execute("INSERT OR IGNORE INTO graph_issues VALUES(?,?,?,?,?,?,?,?,?,?,?)",(issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json,source_id))
    db.execute(
        """
        INSERT OR IGNORE INTO resolution_issues(issue_id,issue_type,severity,status,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json,source_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (issue_id,issue_type,severity,"OPEN",source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json,source_id),
    )

def _sync_resolution_issues(db: sqlite3.Connection) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO resolution_issues(issue_id,issue_type,severity,status,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json,source_id)
        SELECT issue_id,issue_type,severity,'OPEN',source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json,source_id
        FROM graph_issues
        """
    )

def resolve(db: sqlite3.Connection) -> None:
    """Resolve cross-source references after every package is imported.

    Source extractors emit local references only. The resolver owns links that need
    knowledge of more than one source package, such as Job -> Executable and
    Angular API call reference -> .NET API operation.
    """
    db.execute("DELETE FROM graph_sources WHERE source_id=?",(RESOLVER_SOURCE,))
    db.execute("INSERT OR REPLACE INTO graph_sources VALUES(?,?,datetime('now'))",(RESOLVER_SOURCE,"cross-source-resolver"))
    _resolve_executables(db)
    _resolve_api_calls(db)
    _resolve_routine_references(db)

def _resolve_executables(db: sqlite3.Connection) -> None:
    jobs=db.execute("SELECT node_id,properties_json FROM graph_nodes WHERE node_type='JOB'").fetchall()
    executables=db.execute("SELECT node_id,technical_name,properties_json FROM graph_nodes WHERE node_type='EXECUTABLE'").fetchall()
    by_filename: dict[str,list[str]]={}
    by_assembly: dict[str,list[str]]={}
    by_alias: dict[str,list[str]]={}
    for node_id, name, raw_props in executables:
        technical_name=str(name or "")
        _append_index(by_filename,_normalize_executable_filename(technical_name),node_id)
        _append_index(by_assembly,_normalize_executable_assembly(technical_name),node_id)
        props=_json_props(raw_props)
        for value in _property_strings(props,"names","fileNames","filenames","targetNames"):
            _append_index(by_filename,_normalize_executable_filename(value),node_id)
        for value in _property_strings(props,"assemblyNames","assemblyName","assemblies"):
            _append_index(by_assembly,_normalize_executable_assembly(value),node_id)
        for value in _property_strings(props,"aliases","alias"):
            _append_index(by_alias,_normalize_executable_alias(value),node_id)
    mappings=db.execute("SELECT job_system,executable_name,executable_scope,canonical_executable_name,alias FROM graph_executable_mappings").fetchall()
    for job_id, raw in jobs:
        props=json.loads(raw); name=props.get("executable_name","")
        key=(props.get("job_system",""),props.get("executable_scope",""),name)
        candidates, ambiguous, reason = _executable_candidates_for_job(key, mappings, by_filename, by_assembly, by_alias)
        if len(candidates)==1:
            target=candidates[0]; eid=canonical_edge_id(job_id,"STARTS",target,"","TECHNICAL")
            db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(eid,job_id,target,"STARTS","TECHNICAL","",1.0,json.dumps({"match":reason},ensure_ascii=False),RESOLVER_SOURCE))
        elif name:
            issue_type="AMBIGUOUS_SYMBOL" if ambiguous else "EXECUTABLE_NOT_MAPPED"
            iid="issue:"+hashlib.sha256(f"{job_id}|{key}|{issue_type}".encode()).hexdigest()
            message=f"Ambiguous executable mapping: {key}" if ambiguous else f"Executable not mapped: {key}"
            _insert_issue(db,iid,issue_type,"WARNING",job_id,name,"","",None,message,"{}",RESOLVER_SOURCE)

def _executable_candidates_for_job(key: tuple[str,str,str], mappings: list[sqlite3.Row], by_filename: dict[str,list[str]], by_assembly: dict[str,list[str]], by_alias: dict[str,list[str]]) -> tuple[list[str],bool,str]:
    job_system, executable_scope, raw_name = key
    explicit=[row for row in mappings if row[0]==job_system and row[2]==executable_scope and row[1]==raw_name]
    if len(explicit)>1:
        return [],True,"explicit-mapping"
    if len(explicit)==1:
        candidates=_lookup_executable(by_filename,_normalize_executable_filename(explicit[0][3]))
        if candidates:
            return candidates,len(candidates)>1,"explicit-mapping"
    filename_candidates=_lookup_executable(by_filename,_normalize_executable_filename(raw_name))
    if filename_candidates:
        return filename_candidates,len(filename_candidates)>1,"filename"
    assembly_candidates=_lookup_executable(by_assembly,_normalize_executable_assembly(raw_name))
    if assembly_candidates:
        return assembly_candidates,len(assembly_candidates)>1,"assembly"
    alias_candidates=_lookup_executable(by_alias,_normalize_executable_alias(raw_name))
    if alias_candidates:
        return alias_candidates,len(alias_candidates)>1,"alias"
    alias_mappings=[row for row in mappings if row[0]==job_system and row[2]==executable_scope and _normalize_executable_alias(row[4])==_normalize_executable_alias(raw_name)]
    if len(alias_mappings)>1:
        return [],True,"alias-mapping"
    if len(alias_mappings)==1:
        candidates=_lookup_executable(by_filename,_normalize_executable_filename(alias_mappings[0][3]))
        if candidates:
            return candidates,len(candidates)>1,"alias-mapping"
    return [],False,"none"

def _append_index(index: dict[str,list[str]], key: str, node_id: str) -> None:
    if not key:
        return
    values=index.setdefault(key,[])
    if node_id not in values:
        values.append(node_id)

def _lookup_executable(index: dict[str,list[str]], key: str) -> list[str]:
    return index.get(key,[]) if key else []

def _normalize_executable_filename(value: str) -> str:
    text=Path(str(value or "").strip().strip('"').strip("'")).name.casefold()
    if not text:
        return ""
    if text.endswith(".exe"):
        return text
    if text.endswith(".dll"):
        return f"{Path(text).stem}.exe"
    return f"{text}.exe"

def _normalize_executable_assembly(value: str) -> str:
    text=Path(str(value or "").strip().strip('"').strip("'")).name
    lowered=text.casefold()
    if lowered.endswith(".exe") or lowered.endswith(".dll"):
        return Path(text).stem.casefold()
    return lowered

def _normalize_executable_alias(value: str) -> str:
    return str(value or "").strip().casefold()

def _property_strings(props: dict, *keys: str) -> list[str]:
    values: list[str]=[]
    for key in keys:
        item=props.get(key)
        if isinstance(item,str):
            values.append(item)
        elif isinstance(item,(list,tuple)):
            values.extend(str(value) for value in item if isinstance(value,(str,int,float)))
    return values

def _resolve_api_calls(db: sqlite3.Connection) -> None:
    operations=[]
    for node_id, raw_props in db.execute("SELECT node_id,properties_json FROM graph_nodes WHERE node_type='API_OPERATION'"):
        parsed=_parse_api_operation(node_id, raw_props)
        if parsed: operations.append(parsed)
    if not operations:
        return
    for call_id, raw_props in db.execute("SELECT node_id,properties_json FROM graph_nodes WHERE node_type='API_CALL_REFERENCE'"):
        call=_parse_api_call(call_id, raw_props)
        if not call:
            continue
        matches, reason, confidence = _match_api_operation(call, operations)
        raw_reference=f"{call['method']} {call['route']}"
        if len(matches)==1:
            target=matches[0]["node_id"]
            _insert_resolver_edge(db, call_id, target, "RESOLVES_TO", confidence, {"match": reason})
            for caller_id in _api_call_ui_callers(db, call_id):
                _insert_resolver_edge(db, caller_id, target, "CALLS_API", confidence, {"match": reason, "via": call_id})
        elif matches:
            candidates=[item["node_id"] for item in matches]
            _insert_resolver_issue(db, "API_ROUTE_AMBIGUOUS", "ERROR", call_id, raw_reference, "Multiple API operations matched", {"candidates": candidates, "match": reason})
        else:
            _insert_resolver_issue(db, "API_ROUTE_NOT_MATCHED", "WARNING", call_id, raw_reference, "No API operation matched", {})

def _api_call_ui_callers(db: sqlite3.Connection, call_id: str) -> list[str]:
    """Return SCREEN/UI_ACTION callers that should receive resolver CALLS_API edges."""
    callers: set[str]=set()

    def add_screen_for_action(action_id: str) -> None:
        for (screen_id,) in db.execute(
            """
            SELECT e.source_node_id
            FROM graph_edges e
            JOIN graph_nodes n ON n.node_id=e.source_node_id
            WHERE e.target_node_id=? AND e.edge_type='CONTAINS' AND n.node_type='SCREEN'
            """,
            (action_id,),
        ):
            callers.add(screen_id)

    direct=db.execute(
        """
        SELECT e.source_node_id,n.node_type
        FROM graph_edges e
        JOIN graph_nodes n ON n.node_id=e.source_node_id
        WHERE e.target_node_id=? AND e.edge_type='CALLS'
        """,
        (call_id,),
    ).fetchall()
    service_callers=[]
    for source_id, node_type in direct:
        if node_type in {"SCREEN","UI_ACTION"}:
            callers.add(source_id)
            if node_type=="UI_ACTION":
                add_screen_for_action(source_id)
        elif node_type in {"ANGULAR_SERVICE","SERVICE"}:
            service_callers.append(source_id)

    for service_id in service_callers:
        for source_id, node_type in db.execute(
            """
            SELECT e.source_node_id,n.node_type
            FROM graph_edges e
            JOIN graph_nodes n ON n.node_id=e.source_node_id
            WHERE e.target_node_id=? AND e.edge_type='CALLS'
            """,
            (service_id,),
        ):
            if node_type not in {"SCREEN","UI_ACTION"}:
                continue
            callers.add(source_id)
            if node_type=="UI_ACTION":
                add_screen_for_action(source_id)

    return sorted(callers)

def _insert_resolver_edge(db: sqlite3.Connection, source_id: str, target_id: str, edge_type: str, confidence: float = 1.0, properties: dict | None = None) -> None:
    edge_id=canonical_edge_id(source_id,edge_type,target_id,"","TECHNICAL")
    props=json.dumps(properties or {},sort_keys=True,separators=(",",":"))
    db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(edge_id,source_id,target_id,edge_type,"TECHNICAL","",confidence,props,RESOLVER_SOURCE))

def _insert_resolver_issue(db: sqlite3.Connection, issue_type: str, severity: str, source_node_id: str, raw_reference: str, message: str, properties: dict | None = None) -> None:
    identity=f"resolver|{issue_type}|{source_node_id}|{raw_reference}|{message}"
    issue_id="issue:"+hashlib.sha256(identity.encode()).hexdigest()
    props=json.dumps(properties or {},sort_keys=True,separators=(",",":"))
    _insert_issue(db,issue_id,issue_type,severity,source_node_id,raw_reference,"","",None,message,props,RESOLVER_SOURCE)

def _resolve_routine_references(db: sqlite3.Connection) -> None:
    """Resolve unresolved procedure/function references to canonical routine nodes.

    Extractors may emit unresolved reference nodes when a routine target cannot be
    resolved inside a single source package. This pass only links references with
    exact database + package/standalone + routine (+ signature when present)
    matches. Missing signature is allowed only when the routine is not overloaded.
    """
    routines=[parsed for parsed in (_parse_routine_node(*row) for row in db.execute("SELECT node_id,node_type,technical_name,qualified_name,properties_json FROM graph_nodes WHERE node_type IN('PROCEDURE','FUNCTION')")) if parsed]
    if not routines:
        return
    by_scope: dict[tuple[str,str,str],list[dict[str,str]]]={}
    by_database_name: dict[tuple[str,str],list[dict[str,str]]]={}
    for routine in routines:
        by_scope.setdefault((routine["database"],routine["package"],routine["name"]),[]).append(routine)
        by_database_name.setdefault((routine["database"],routine["name"]),[]).append(routine)

    unresolved_rows=db.execute(
        """
        SELECT node_id,technical_name,qualified_name,database_key,properties_json
        FROM graph_nodes WHERE node_type='UNRESOLVED_REFERENCE'
        """
    ).fetchall()
    for node_id, technical_name, qualified_name, database_key, raw_props in unresolved_rows:
        context_edges=db.execute("SELECT source_node_id,raw_operation,properties_json FROM graph_edges WHERE target_node_id=? AND edge_type='CALLS'",(node_id,)).fetchall()
        references=_routine_reference_candidates(node_id, technical_name, qualified_name, database_key, raw_props, context_edges)
        for reference in references:
            matches, reason = _match_routine_reference(reference, by_scope, by_database_name)
            raw_reference=reference.get("raw_reference") or _format_routine_reference(reference)
            if len(matches)==1:
                _insert_resolver_edge(db, node_id, matches[0]["node_id"], "RESOLVES_TO", 1.0, {"match": reason})
                break
            if matches:
                _insert_resolver_issue(db,"AMBIGUOUS_SYMBOL","WARNING",node_id,raw_reference,"Multiple procedure/function candidates matched",{"candidates":[item["node_id"] for item in matches],"match":reason})
                break
        else:
            if _looks_like_routine_reference(node_id, technical_name, qualified_name, raw_props):
                reference=references[0] if references else {"raw_reference": technical_name or qualified_name or node_id}
                _insert_resolver_issue(db,"PROCEDURE_NOT_FOUND","WARNING",node_id,reference.get("raw_reference") or str(technical_name or node_id),"Procedure/function reference was not resolved",{})

def _parse_routine_node(node_id: str, node_type: str, technical_name: str, qualified_name: str, raw_props: str) -> dict[str,str] | None:
    parts=node_id.split(":")
    if len(parts)!=5 or node_type not in ROUTINE_NODE_TYPES or parts[0] not in {"procedure","function"}:
        return None
    try:
        database=normalize_oracle_identifier(parts[1])
        package=normalize_oracle_identifier(parts[2])
        name=normalize_oracle_identifier(parts[3] or technical_name)
    except ValueError:
        return None
    signature=parts[4] or _routine_signature_from_props(_json_props(raw_props)) or "void"
    return {"node_id":node_id,"node_type":node_type,"database":database,"package":package,"name":name,"signature":signature,"signature_key":_signature_key(signature),"qualified_name":qualified_name}

def _routine_reference_candidates(node_id: str, technical_name: str, qualified_name: str, database_key: str, raw_props: str, context_edges: list[sqlite3.Row]) -> list[dict[str,str]]:
    props=_json_props(raw_props)
    default_database=_default_reference_database(node_id, database_key, props)
    context_routines=[parsed for parsed in (_parse_routine_id(row[0]) for row in context_edges) if parsed]
    raw_values=[]
    raw_values.extend(_routine_raw_values(props))
    raw_values.extend([technical_name, qualified_name, node_id])
    for row in context_edges:
        raw_values.append(str(row[1] or ""))
        raw_values.extend(_routine_raw_values(_json_props(row[2])))
    structured=_structured_routine_references(props, default_database)
    candidates: list[dict[str,str]]=[]
    for item in structured:
        candidates.extend(_expand_reference_context(item, context_routines))
    for raw in raw_values:
        parsed=_parse_routine_reference(str(raw or ""), default_database, props)
        if parsed:
            candidates.extend(_expand_reference_context(parsed, context_routines))
    unique: dict[tuple[str,str,str,str],dict[str,str]]={}
    for candidate in candidates:
        key=(candidate.get("database",""),candidate.get("package",""),candidate.get("name",""),candidate.get("signature",""))
        if candidate.get("database") and candidate.get("name") and key not in unique:
            unique[key]=candidate
    return list(unique.values())

def _default_reference_database(node_id: str, database_key: str, props: dict) -> str:
    for value in (props.get("database"),props.get("database_key"),props.get("databaseKey"),database_key):
        if isinstance(value,str) and value.strip():
            try:
                return normalize_oracle_identifier(value)
            except ValueError:
                pass
    parts=node_id.split(":",2)
    if len(parts)>=2 and parts[0]=="unresolved-reference":
        try:
            return normalize_oracle_identifier(parts[1])
        except ValueError:
            pass
    return ""

def _routine_raw_values(value: object) -> list[str]:
    values: list[str]=[]
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {"raw_reference","rawReference","reference","routine_reference","routineReference","expression","target","object_name","objectName","procedure","function","routine","name"}:
                values.extend(_string_values(item))
            elif isinstance(item,(dict,list,tuple)) and key in {"call","target","routine","procedure","function","reference"}:
                values.extend(_routine_raw_values(item))
    elif isinstance(value,(list,tuple)):
        for item in value:
            values.extend(_routine_raw_values(item))
    return values

def _structured_routine_references(props: dict, default_database: str) -> list[dict[str,str]]:
    names=_first_string(props,"routine","routineName","procedure","procedureName","function","functionName","name","object_name","objectName")
    if not names:
        return []
    package=_first_string(props,"package","packageName","owner","ownerName","standalone","objectOwner")
    database=_normalize_identifier_or_empty(_first_string(props,"database","database_key","databaseKey") or default_database)
    signature=_routine_signature_from_props(props)
    refs=[]
    for name in _split_reference_names(names):
        try:
            refs.append({"database":database,"package":normalize_oracle_identifier(package) if package else "","name":normalize_oracle_identifier(name),"signature":signature,"raw_reference":name})
        except ValueError:
            continue
    return refs

def _parse_routine_reference(raw: str, default_database: str, props: dict) -> dict[str,str] | None:
    text=str(raw or "").strip().strip('"\'')
    if not text:
        return None
    parsed_id=_parse_routine_id(text)
    if parsed_id:
        parsed_id["raw_reference"]=text
        return parsed_id
    if text.startswith("unresolved-reference:"):
        text=text.split(":",2)[-1]
    text=re.sub(r"^(?:EXEC(?:UTE)?|CALL)\s+", "", text, flags=re.IGNORECASE).strip()
    if "@" in text:
        return None
    paren_signature=""
    match=re.match(r"^([\w\".$#]+)\s*\((.*)\)\s*$", text)
    if match:
        text=match.group(1)
        paren_signature=_signature_from_parameter_text(match.group(2))
    if not re.fullmatch(r"[A-Za-z0-9_.$#\"]+(?:\.[A-Za-z0-9_.$#\"]+)*", text):
        return None
    parts=[part for part in text.split(".") if part]
    if not parts:
        return None
    signature=_routine_signature_from_props(props) or paren_signature
    try:
        if len(parts)>=3:
            maybe_database=_normalize_identifier_or_empty(parts[-3])
            database=maybe_database if maybe_database==default_database or not default_database else default_database
            package=normalize_oracle_identifier(parts[-2])
            name=normalize_oracle_identifier(parts[-1])
        elif len(parts)==2:
            database=default_database
            package=normalize_oracle_identifier(parts[0])
            name=normalize_oracle_identifier(parts[1])
        else:
            database=default_database
            package=""
            name=normalize_oracle_identifier(parts[0])
    except ValueError:
        return None
    return {"database":database,"package":package,"name":name,"signature":signature,"raw_reference":raw}

def _parse_routine_id(value: str) -> dict[str,str] | None:
    parts=str(value or "").split(":")
    if len(parts)!=5 or parts[0] not in {"procedure","function"}:
        return None
    try:
        return {"database":normalize_oracle_identifier(parts[1]),"package":normalize_oracle_identifier(parts[2]),"name":normalize_oracle_identifier(parts[3]),"signature":parts[4]}
    except ValueError:
        return None

def _expand_reference_context(reference: dict[str,str], context_routines: list[dict[str,str]]) -> list[dict[str,str]]:
    if reference.get("package"):
        return [reference]
    expanded=[]
    for context in context_routines:
        if context.get("database")==reference.get("database"):
            expanded.append({**reference,"package":context.get("package","")})
    expanded.append(reference)
    return expanded

def _match_routine_reference(reference: dict[str,str], by_scope: dict[tuple[str,str,str],list[dict[str,str]]], by_database_name: dict[tuple[str,str],list[dict[str,str]]]) -> tuple[list[dict[str,str]], str]:
    database=reference.get("database","")
    package=reference.get("package","")
    name=reference.get("name","")
    if not database or not name:
        return [],"none"
    candidates=by_scope.get((database,package,name),[]) if package else by_database_name.get((database,name),[])
    if reference.get("signature"):
        signature_key=_signature_key(reference["signature"])
        candidates=[item for item in candidates if item.get("signature_key")==signature_key]
        return candidates,"routine-signature"
    return candidates,"routine-unique" if len(candidates)==1 else "routine-overload"

def _format_routine_reference(reference: dict[str,str]) -> str:
    parts=[reference.get("database",""),reference.get("package",""),reference.get("name","")]
    text=".".join(part for part in parts if part)
    if reference.get("signature"):
        text+=f"({reference['signature']})"
    return text

def _looks_like_routine_reference(node_id: str, technical_name: str, qualified_name: str, raw_props: str) -> bool:
    props=_json_props(raw_props)
    if _structured_routine_references(props, _default_reference_database(node_id,"",props)):
        return True
    text=" ".join(str(value or "") for value in (technical_name, qualified_name, node_id))
    return "." in text and not any(word in text.upper() for word in ("DYNAMIC", "EXECUTE IMMEDIATE"))

def _first_string(mapping: dict, *keys: str) -> str:
    for key in keys:
        value=mapping.get(key)
        if isinstance(value,str) and value.strip():
            return value.strip()
    return ""

def _split_reference_names(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]

def _normalize_identifier_or_empty(value: str) -> str:
    try:
        return normalize_oracle_identifier(value) if value else ""
    except ValueError:
        return ""

def _routine_signature_from_props(props: dict) -> str:
    value=None
    for key in ("signature","parameter_signature","parameterSignature","parameters_signature","parametersSignature"):
        if isinstance(props.get(key),str) and props[key].strip():
            value=props[key]
            break
    if value is None:
        for key in ("parameterTypes","parameter_types","parameters","args","arguments"):
            item=props.get(key)
            if isinstance(item,(list,tuple)) and item:
                value="_".join(str(part.get("type") if isinstance(part,dict) else part) for part in item)
                break
            if isinstance(item,str) and item.strip():
                value=item
                break
    return str(value).strip() if value is not None else ""

def _signature_from_parameter_text(text: str) -> str:
    parts=[part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return "void"
    type_tokens=[]
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_.$#\"]+", part):
            return ""
        type_tokens.append(part)
    return "_".join(type_tokens)

def _signature_key(signature: str) -> str:
    text=str(signature or "void").strip()
    if not text:
        text="void"
    tokens=[token for token in re.split(r"[,_\s]+", text.upper()) if token]
    return "_".join(tokens) if tokens else "VOID"

def _parse_api_operation(node_id: str, raw_props: str) -> dict[str,str] | None:
    parts=node_id.split(":",3)
    if len(parts)!=4 or parts[0]!="api-operation":
        return None
    props=_json_props(raw_props)
    application=parts[1]
    method=str(props.get("method") or parts[2])
    route=str(props.get("route") or parts[3])
    try:
        method, route = normalize_http_route(method, route)
    except ValueError:
        return None
    return {"node_id":node_id,"application":application,"method":method,"route":route,"properties":props}

def _parse_api_call(node_id: str, raw_props: str) -> dict[str,str] | None:
    parts=node_id.split(":",3)
    if len(parts)!=4 or parts[0]!="api-call":
        return None
    props=_json_props(raw_props)
    source=parts[1]
    method=str(props.get("method") or parts[2])
    route=str(props.get("route") or parts[3])
    try:
        method, route = normalize_http_route(method, route)
    except ValueError:
        return None
    return {"node_id":node_id,"source":source,"method":method,"route":route,"properties":props}

def _json_props(raw: str) -> dict:
    try:
        parsed=json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed,dict) else {}

def _match_api_operation(call: dict[str,str], operations: list[dict[str,str]]) -> tuple[list[dict[str,str]], str, float]:
    explicit=_explicit_api_operation_matches(call, operations)
    if explicit:
        return explicit,"explicit-mapping",1.0
    method_matches=[item for item in operations if item["method"]==call["method"]]
    full=[item for item in method_matches if item["route"]==call["route"]]
    if full:
        return full,"method-route",1.0
    prefixed_app, stripped_route = _strip_api_prefix(call["method"], call["route"])
    if stripped_route:
        app_matches=[item for item in method_matches if item["application"]==prefixed_app and item["route"]==stripped_route]
        if app_matches:
            return app_matches,"api-prefix-application",0.95
        stripped_matches=[item for item in method_matches if item["route"]==stripped_route]
        if stripped_matches:
            return stripped_matches,"api-prefix-route",0.9
    suffix=[item for item in method_matches if _suffix_route_match(call["route"], item["route"])]
    if suffix:
        return suffix,"unique-suffix",0.8
    return [],"none",0.0

def _explicit_api_operation_matches(call: dict, operations: list[dict]) -> list[dict]:
    props=call.get("properties") if isinstance(call.get("properties"),dict) else {}
    by_id={item["node_id"]:item for item in operations}
    matches: list[dict]=[]
    for node_id in _collect_explicit_api_operation_ids(props):
        target=by_id.get(node_id)
        if target:
            matches.append(target)
    target_specs=[]
    for key in {"mapping","explicitMapping","apiMapping","api","target","targetApi","operation"}:
        if key in props:
            target_specs.extend(_collect_explicit_api_operation_targets(props[key]))
    for target in target_specs:
        try:
            method, route = normalize_http_route(str(target.get("method") or call.get("method") or ""), str(target.get("route") or target.get("path") or ""))
        except ValueError:
            continue
        application=str(target.get("application") or target.get("source") or target.get("service") or "").strip()
        for item in operations:
            if item["method"]==method and item["route"]==route and (not application or item["application"]==application):
                matches.append(item)
    return _unique_matches(matches)

def _collect_explicit_api_operation_ids(value: object) -> list[str]:
    ids: list[str]=[]
    if isinstance(value,dict):
        for key, item in value.items():
            if key in API_EXPLICIT_MAPPING_KEYS:
                ids.extend(_string_values(item))
            elif isinstance(item,(dict,list,tuple)) and key in {"mapping","explicitMapping","apiMapping","api","target","targetApi","operation"}:
                ids.extend(_collect_explicit_api_operation_ids(item))
    elif isinstance(value,(list,tuple)):
        for item in value:
            ids.extend(_collect_explicit_api_operation_ids(item))
    return [item for item in ids if item.startswith("api-operation:")]

def _collect_explicit_api_operation_targets(value: object) -> list[dict]:
    targets: list[dict]=[]
    if isinstance(value,dict):
        if any(key in value for key in ("route","path")) and any(key in value for key in ("method","httpMethod")):
            target=dict(value)
            if "httpMethod" in target and "method" not in target:
                target["method"]=target["httpMethod"]
            targets.append(target)
        for key, item in value.items():
            if isinstance(item,(dict,list,tuple)) and key in {"mapping","explicitMapping","apiMapping","api","target","targetApi","operation"}:
                targets.extend(_collect_explicit_api_operation_targets(item))
    elif isinstance(value,(list,tuple)):
        for item in value:
            targets.extend(_collect_explicit_api_operation_targets(item))
    return targets

def _string_values(value: object) -> list[str]:
    if isinstance(value,str):
        return [value.strip()]
    if isinstance(value,(list,tuple,set)):
        items: list[str]=[]
        for item in value:
            items.extend(_string_values(item))
        return items
    if isinstance(value,dict):
        return [str(item).strip() for key in API_EXPLICIT_MAPPING_KEYS for item in _string_values(value.get(key,"")) if item]
    return []

def _unique_matches(matches: list[dict]) -> list[dict]:
    unique: dict[str,dict]={}
    for item in matches:
        unique.setdefault(item["node_id"],item)
    return list(unique.values())

def _strip_api_prefix(method: str, route: str) -> tuple[str,str]:
    parts=[part for part in route.strip("/").split("/") if part]
    if len(parts)<2 or not parts[0].endswith("-api"):
        return "",""
    try:
        _, stripped = normalize_http_route(method, "/" + "/".join(parts[1:]))
    except ValueError:
        return "",""
    return parts[0], stripped

def _suffix_route_match(call_route: str, operation_route: str) -> bool:
    call_parts=[part for part in call_route.strip("/").split("/") if part]
    operation_parts=[part for part in operation_route.strip("/").split("/") if part]
    return len(operation_parts)>=2 and len(call_parts)>len(operation_parts) and call_parts[-len(operation_parts):]==operation_parts

def materialize(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM graph_paths")
    db.execute("DELETE FROM graph_edges WHERE graph_layer='DATA_FLOW'")
    rows=db.execute("""
        SELECT edge_id,source_node_id,target_node_id,edge_type,source_id,raw_operation,properties_json
        FROM graph_edges WHERE graph_layer!='DATA_FLOW'
        ORDER BY edge_id
    """).fetchall(); outgoing={}
    for row in rows: outgoing.setdefault(row[1],[]).append(row)
    actor_types=",".join("?"*len(MATERIALIZER_ACTOR_TYPES))
    actors=[r[0] for r in db.execute(f"SELECT node_id FROM graph_nodes WHERE node_type IN({actor_types})",sorted(MATERIALIZER_ACTOR_TYPES))]
    tables={r[0] for r in db.execute("SELECT node_id FROM graph_nodes WHERE node_type='TABLE'")}
    for actor in actors:
        queue=[(actor,[],frozenset({actor}))]
        while queue:
            node,path,visited=queue.pop(0)
            for edge in outgoing.get(node,[]):
                target=edge[2]; next_path=path+[edge[0]]
                operation=_terminal_mode(edge[3])
                if target in tables and operation:
                    evidence=[r[0] for r in db.execute(f"SELECT evidence_id FROM graph_evidence WHERE target_id IN ({','.join('?'*len(next_path))}) ORDER BY evidence_id",next_path)]
                    identity=f"{actor}|{target}|{operation}|{'|'.join(next_path)}"; pid="path:"+hashlib.sha256(identity.encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO graph_paths VALUES(?,?,?,?,?,?,?)",(pid,actor,target,operation,json.dumps(next_path),json.dumps(evidence),edge[4]))
                    data_edge_type="READS" if operation=="R" else "WRITES"
                    terminal_operation=edge[5] or edge[3]
                    data_edge_id=canonical_edge_id(actor,data_edge_type,target,terminal_operation,"DATA_FLOW")
                    terminal_props=json.loads(edge[6] or "{}")
                    data_properties={"path_id":pid,"edge_path":next_path,"terminal_edge_id":edge[0],"terminal_edge_type":edge[3],"terminal_raw_operation":edge[5],"operation":operation}
                    if "semantic" in terminal_props:
                        data_properties["semantic"]=terminal_props["semantic"]
                    data_props=json.dumps(data_properties,sort_keys=True)
                    db.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(data_edge_id,actor,target,data_edge_type,"DATA_FLOW",terminal_operation,1.0,data_props,edge[4]))
                elif edge[3] in MATERIALIZER_INTERMEDIATE_EDGES and target not in visited and len(next_path)<16:
                    queue.append((target,next_path,visited|{target}))

def _terminal_mode(edge_type: str) -> str:
    if edge_type in READ_TERMINALS:
        return "R"
    if edge_type in WRITE_TERMINALS:
        return "W"
    return ""

def authoritative_ids(input_root: Path) -> set[str]:
    external=set()
    for table, columns in _table_catalog(input_root):
        database=normalize_oracle_identifier(table['database']); table_code=normalize_oracle_identifier(table['table_code'])
        external.add(table_id(database,table_code))
        external.update(column_id(database,table_code,row['column_code']) for row in columns)
    for row in _read(input_root/"jobnet.csv"):
        external.add(f"job-network:batch-system:{row['jobnet_id']}")
        external.add(f"job:batch-system:{row['jobnet_id']}:{row['job_id']}")
    return external

def validation_ids(roots: Iterable[Path], input_root: Path) -> set[str]:
    external=authoritative_ids(input_root)
    for root in roots:
        external.update(row["node_id"] for row in _read(root/"nodes.csv"))
    return external

def import_roots(roots: Iterable[Path], db_path: Path, input_root: Path, evidence_root: Path) -> dict[str,int]:
    packages=list(roots)
    # Validation is complete before opening/publishing, preserving the live DB on corrupt input.
    allowed_ids=validation_ids(packages,input_root)
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_nodes'").fetchone():
                allowed_ids.update(row[0] for row in db.execute("SELECT node_id FROM graph_nodes"))
    validated=[validate_package(root,allowed_ids,workspace_root=evidence_root) for root in packages]
    db_path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db:
        initialize(db)
        with db:
            db.execute("PRAGMA defer_foreign_keys=ON")
            run_id=_create_processing_run(db,validated)
            import_authoritative(db,input_root)
            for package in validated:
                _record_import_package(db,run_id,package,"RUNNING")
                publish(db,package)
                _record_import_package(db,run_id,package,_package_status(package))
            resolve(db); materialize(db)
            problems=db.execute("PRAGMA foreign_key_check").fetchall()
            if problems: raise RuntimeError(f"foreign key check failed: {problems}")
            _complete_processing_run(db,run_id)
        return {name:db.execute(f"SELECT COUNT(*) FROM graph_{name}").fetchone()[0] for name in ("nodes","edges","evidence","issues","paths")}

def _create_processing_run(db: sqlite3.Connection, packages: list[dict[str,object]]) -> str:
    started=_now()
    identity=json.dumps([package["manifest"] for package in packages],sort_keys=True,default=str)+started
    run_id="run:"+hashlib.sha256(identity.encode()).hexdigest()
    db.execute(
        """
        INSERT INTO processing_runs(run_id,run_status,started_at,package_count,properties_json)
        VALUES(?,?,?,?,?)
        """,
        (run_id,"RUNNING",started,len(packages),json.dumps({"source":"csv-importer"},sort_keys=True)),
    )
    return run_id

def _record_import_package(db: sqlite3.Connection, run_id: str, package: dict[str,object], status: str) -> None:
    manifest=package["manifest"]
    issues=package["issues"]
    warnings=sum(1 for row in issues if row.get("severity")=="WARNING")
    errors=sum(1 for row in issues if row.get("severity")=="ERROR")
    db.execute(
        """
        INSERT OR REPLACE INTO import_packages(run_id,source_id,package_id,contract_version,source_created_at,import_status,nodes_count,edges_count,evidence_count,issues_count,warnings_count,errors_count,manifest_json,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        (
            run_id,manifest["sourceId"],manifest["packageId"],manifest["contractVersion"],manifest["createdAt"],status,
            len(package["nodes"]),len(package["edges"]),len(package["evidence"]),len(issues),warnings,errors,
            json.dumps(manifest,sort_keys=True,separators=(",",":")),
        ),
    )

def _package_status(package: dict[str,object]) -> str:
    return "COMPLETED_WITH_ERRORS" if any(row.get("severity")=="ERROR" for row in package["issues"]) else "COMPLETED"

def _complete_processing_run(db: sqlite3.Connection, run_id: str) -> None:
    issue_counts=db.execute("SELECT severity,count(*) FROM graph_issues GROUP BY severity").fetchall()
    by_severity={row[0]:row[1] for row in issue_counts}
    status="COMPLETED_WITH_ERRORS" if by_severity.get("ERROR",0) else "COMPLETED"
    counts={name:db.execute(f"SELECT count(*) FROM graph_{name}").fetchone()[0] for name in ("nodes","edges","evidence","issues")}
    db.execute(
        """
        UPDATE processing_runs
        SET run_status=?,completed_at=?,node_count=?,edge_count=?,evidence_count=?,issue_count=?,warning_count=?,error_count=?,updated_at=datetime('now')
        WHERE run_id=?
        """ if _has_column(db,"processing_runs","updated_at") else """
        UPDATE processing_runs
        SET run_status=?,completed_at=?,node_count=?,edge_count=?,evidence_count=?,issue_count=?,warning_count=?,error_count=?
        WHERE run_id=?
        """,
        (status,_now(),counts["nodes"],counts["edges"],counts["evidence"],counts["issues"],by_severity.get("WARNING",0),by_severity.get("ERROR",0),run_id),
    )
