"""CSV package validation and low-level publication helpers."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from contract.graph_contract import (
    EDGE_TYPES,
    GRAPH_LAYERS,
    GRAPH_ROLES,
    ISSUE_TYPES,
    NODE_TYPES,
    TARGET_TYPES,
    canonical_edge_id,
    canonical_evidence_key,
    normalize_repository_path,
    validate_confidence,
    validate_edge_fields,
    validate_enum,
    validate_evidence_location,
    validate_node_fields,
    validate_properties_json,
    validate_stable_id,
)

CSV_HEADERS = {
    "nodes": "node_id,node_type,technical_name,qualified_name,default_display_name,system_key,database_key,repository_key,graph_role,confidence,properties_json".split(","),
    "edges": "edge_id,source_node_id,target_node_id,edge_type,graph_layer,raw_operation,confidence,properties_json".split(","),
    "evidence": "evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json".split(","),
    "issues": "issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json".split(","),
}
OPTIONAL_CSV_HEADERS = {
    "localized_texts": "target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at".split(","),
}
PACKAGE_CSV_HEADERS = {**CSV_HEADERS, **OPTIONAL_CSV_HEADERS}
SUPPORTED_CONTRACT_VERSIONS = frozenset({"1.0"})
FORBIDDEN_SOURCE_NODE_TYPES = frozenset({"TABLE", "JOB", "JOB_NETWORK"})
SEVERITIES = frozenset({"INFO", "WARNING", "ERROR"})

GRAPH_TABLES = {
    "sources": "graph_sources",
    "nodes": "graph_nodes",
    "edges": "graph_edges",
    "evidence": "graph_evidence",
    "issues": "graph_issues",
    "paths": "graph_paths",
    "knowledge": "graph_knowledge",
}

SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS graph_sources(source_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_nodes(node_id TEXT PRIMARY KEY,node_type TEXT NOT NULL,technical_name TEXT NOT NULL,qualified_name TEXT NOT NULL,default_display_name TEXT NOT NULL,system_key TEXT,database_key TEXT,repository_key TEXT,graph_role TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_nodes_name ON graph_nodes(default_display_name,technical_name);
CREATE TABLE IF NOT EXISTS graph_edges(edge_id TEXT PRIMARY KEY,source_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),target_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),edge_type TEXT NOT NULL,graph_layer TEXT NOT NULL,raw_operation TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_edges_source ON graph_edges(source_node_id,edge_type);
CREATE INDEX IF NOT EXISTS graph_edges_target ON graph_edges(target_node_id,edge_type);
CREATE TABLE IF NOT EXISTS graph_evidence(evidence_id TEXT PRIMARY KEY,target_type TEXT NOT NULL CHECK(target_type IN('NODE','EDGE')),target_id TEXT NOT NULL,source_path TEXT NOT NULL,start_line INTEGER,end_line INTEGER,start_column INTEGER,end_column INTEGER,evidence_kind TEXT NOT NULL,extractor_name TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),snippet TEXT NOT NULL,properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS graph_evidence_target ON graph_evidence(target_type,target_id,evidence_id);
CREATE TABLE IF NOT EXISTS graph_issues(issue_id TEXT PRIMARY KEY,issue_type TEXT NOT NULL,severity TEXT NOT NULL,source_node_id TEXT,raw_reference TEXT,database_key TEXT,source_path TEXT,start_line INTEGER,message TEXT NOT NULL,properties_json TEXT NOT NULL CHECK(json_valid(properties_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS graph_localization(target_type TEXT NOT NULL,target_id TEXT NOT NULL,field_name TEXT NOT NULL,locale TEXT NOT NULL,value TEXT NOT NULL,source_kind TEXT NOT NULL,review_status TEXT NOT NULL,author_name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,source_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(target_id,field_name,locale));
CREATE TABLE IF NOT EXISTS graph_paths(path_id TEXT PRIMARY KEY,actor_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),table_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),operation TEXT NOT NULL,edge_path_json TEXT NOT NULL CHECK(json_valid(edge_path_json)),source_id TEXT NOT NULL REFERENCES graph_sources(source_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS graph_knowledge(knowledge_id INTEGER PRIMARY KEY, target_id TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN('draft','pending','approved','rejected')),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

def _rows(root: Path, name: str) -> list[dict[str, str]]:
    with (root / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != PACKAGE_CSV_HEADERS[name]:
            raise ValueError(f"invalid {name}.csv header")
        return list(reader)

def _normalize_manifest(manifest: dict[str, object]) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    version = manifest.get("contractVersion")
    if version not in SUPPORTED_CONTRACT_VERSIONS:
        raise ValueError("unsupported contract version")
    return _normalize_requirement_manifest(manifest)

def _normalize_requirement_manifest(manifest: dict[str, object]) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    required = {"contractVersion", "extractor", "source", "generatedAt", "files", "statistics"}
    if set(manifest) - (required | {"checksums", "metadata"}) or not required <= set(manifest):
        raise ValueError("invalid manifest fields")
    extractor = manifest["extractor"]
    source = manifest["source"]
    files = manifest["files"]
    statistics = manifest["statistics"]
    checksums = manifest.get("checksums") or {}
    if not all(isinstance(value, dict) for value in (extractor, source, files, statistics, checksums)):
        raise ValueError("invalid manifest fields")
    allowed_file_keys = set(CSV_HEADERS) | set(OPTIONAL_CSV_HEADERS)
    declared_file_keys = set(files)
    if not set(CSV_HEADERS) <= declared_file_keys or declared_file_keys - allowed_file_keys:
        raise ValueError("invalid manifest file mapping")
    allowed_statistics = {"filesScanned", *declared_file_keys}
    if set(statistics) - allowed_statistics:
        raise ValueError("invalid manifest statistics")
    allowed_checksum_keys = {f"{name}.csv" for name in declared_file_keys}
    if set(checksums) - allowed_checksum_keys:
        raise ValueError("invalid manifest checksum")
    extractor_name = str(extractor.get("name") or "").strip()
    extractor_version = str(extractor.get("version") or "").strip()
    source_key = str(source.get("sourceKey") or "").strip()
    repository_key = str(source.get("repositoryKey") or source_key).strip()
    generated_at = str(manifest.get("generatedAt") or "").strip()
    if not extractor_name or not extractor_version or not source_key or not repository_key or not generated_at:
        raise ValueError("invalid manifest fields")
    file_specs: dict[str, dict[str, object]] = {}
    for name in CSV_HEADERS:
        csv_name = f"{name}.csv"
        if files.get(name) != csv_name:
            raise ValueError("invalid manifest file mapping")
        checksum_entry = checksums.get(csv_name) if isinstance(checksums, dict) else None
        sha = None
        bytes_value = None
        if checksum_entry is not None:
            if not isinstance(checksum_entry, dict):
                raise ValueError("invalid manifest checksum")
            sha = checksum_entry.get("sha256")
            bytes_value = checksum_entry.get("bytes")
            if not isinstance(sha, str) or not _is_sha256(sha):
                raise ValueError("invalid manifest checksum")
            if bytes_value is not None and (not isinstance(bytes_value, int) or bytes_value < 0):
                raise ValueError("invalid manifest file size")
        count = statistics.get(name)
        if not isinstance(count, int) or count < 0:
            raise ValueError("invalid manifest statistics")
        file_specs[name] = {"path": csv_name, "sha256": sha, "bytes": bytes_value}
    for name in OPTIONAL_CSV_HEADERS:
        csv_name = f"{name}.csv"
        if name not in files:
            if name in statistics or csv_name in checksums:
                raise ValueError("invalid manifest file mapping")
            continue
        if files.get(name) != csv_name:
            raise ValueError("invalid manifest file mapping")
        checksum_entry = checksums.get(csv_name) if isinstance(checksums, dict) else None
        sha = None
        bytes_value = None
        if checksum_entry is not None:
            if not isinstance(checksum_entry, dict):
                raise ValueError("invalid manifest checksum")
            sha = checksum_entry.get("sha256")
            bytes_value = checksum_entry.get("bytes")
            if not isinstance(sha, str) or not _is_sha256(sha):
                raise ValueError("invalid manifest checksum")
            if bytes_value is not None and (not isinstance(bytes_value, int) or bytes_value < 0):
                raise ValueError("invalid manifest file size")
        count = statistics.get(name)
        if not isinstance(count, int) or count < 0:
            raise ValueError("invalid manifest statistics")
        file_specs[name] = {"path": csv_name, "sha256": sha, "bytes": bytes_value}
    files_scanned = statistics.get("filesScanned", 0)
    if not isinstance(files_scanned, int) or files_scanned < 0:
        raise ValueError("invalid manifest statistics")
    package_id = f"{extractor_name}:{source_key}"
    normalized = {
        "contractVersion": "1.0",
        "packageId": package_id,
        "sourceId": source_key,
        "createdAt": generated_at,
        "filesScanned": files_scanned,
        "files": {},
        "counts": {name: statistics[name] for name in file_specs},
        "metadata": {
            **dict(manifest.get("metadata") or {}),
            "manifestFormat": "requirements-1.0",
            "extractor": {"name": extractor_name, "version": extractor_version},
            "source": {**source, "repositoryKey": repository_key},
        },
    }
    return normalized, file_specs

def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

def _require_unique(rows: list[dict[str, str]], key: str, label: str) -> None:
    values = [row[key] for row in rows]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")

def _require_unique_localization(rows: list[dict[str, str]]) -> None:
    values = [(row["target_id"], row["field_name"], row["locale"]) for row in rows]
    if len(values) != len(set(values)):
        raise ValueError("duplicate localized text")

def _validate_evidence_source_anchor(root: Path, row: dict[str, str], workspace_root: Path | None) -> None:
    source_file = _resolve_source_path(root, row["source_path"], workspace_root)
    if source_file is None:
        raise ValueError("dangling evidence path")
    _validate_line_bounds(source_file, row["start_line"], row["end_line"], "evidence")

def _validate_issue_source_anchor(root: Path, row: dict[str, str], workspace_root: Path | None) -> None:
    source_file = _resolve_source_path(root, row["source_path"], workspace_root)
    if source_file is None:
        raise ValueError("dangling issue path")
    if row["start_line"]:
        _validate_line_bounds(source_file, row["start_line"], row["start_line"], "issue")

def _resolve_source_path(root: Path, source_path: str, workspace_root: Path | None) -> Path | None:
    source = Path(source_path)
    if source.is_absolute():
        return source if source.is_file() else None
    if workspace_root is None:
        raise ValueError("workspace_root is required to resolve relative evidence paths")
    base = workspace_root.resolve()
    path = (base / source).resolve()
    return path if path.is_relative_to(base) and path.is_file() else None

def _validate_line_bounds(source_file: Path, start_line: str, end_line: str, label: str) -> None:
    if not start_line:
        return
    start = int(start_line)
    end = int(end_line or start_line)
    line_count = len(source_file.read_text(encoding="utf-8").splitlines())
    if start > line_count or end > line_count:
        raise ValueError(f"{label} line range exceeds source file")

def validate_package(root: Path, external_node_ids: Iterable[str] = (), workspace_root: Path | None = None) -> dict[str, object]:
    raw_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest, file_specs = _normalize_manifest(raw_manifest)
    repository_key = manifest["metadata"]["source"]["repositoryKey"]
    repository_root = workspace_root / repository_key if workspace_root else None
    loaded = {name: _rows(root, name) for name in file_specs}
    checksum_validated = True
    normalized_files: dict[str, dict[str, object]] = {}
    for name, rows in loaded.items():
        path = root / f"{name}.csv"
        spec = file_specs[name]
        if spec["path"] != path.name:
            raise ValueError(f"invalid manifest file mapping for {name}.csv")
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        actual_bytes = path.stat().st_size
        expected_sha = spec.get("sha256")
        expected_bytes = spec.get("bytes")
        expected_count = manifest["counts"][name]
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(f"{path.name} checksum/count mismatch")
        if expected_bytes is not None and actual_bytes != expected_bytes:
            raise ValueError(f"{path.name} checksum/count mismatch")
        if len(rows) != expected_count:
            raise ValueError(f"{path.name} checksum/count mismatch")
        checksum_validated = checksum_validated and bool(expected_sha)
        normalized_files[path.name] = {"sha256": actual_sha, "bytes": actual_bytes}
    manifest["files"] = normalized_files
    manifest.setdefault("metadata", {})["checksumsValidated"] = checksum_validated

    _require_unique(loaded["nodes"], "node_id", "node identity")
    _require_unique(loaded["edges"], "edge_id", "edge identity")
    _require_unique(loaded["evidence"], "evidence_id", "evidence identity")
    _require_unique(loaded["issues"], "issue_id", "issue identity")
    if "localized_texts" in loaded:
        _require_unique_localization(loaded["localized_texts"])

    node_ids = {row["node_id"] for row in loaded["nodes"]}
    allowed_node_ids = node_ids | set(external_node_ids)
    edge_ids = {row["edge_id"] for row in loaded["edges"]}
    for row in loaded["nodes"]:
        validate_node_fields(row["node_type"], row["graph_role"], row["confidence"], row["properties_json"])
        validate_stable_id(row["node_id"], row["node_type"])
        if row["node_type"] in FORBIDDEN_SOURCE_NODE_TYPES:
            raise ValueError("source package declares authoritative node")
    for row in loaded["edges"]:
        validate_edge_fields(row["edge_type"], row["graph_layer"], row["confidence"], row["properties_json"])
        if row["graph_layer"] == "DATA_FLOW":
            raise ValueError("invalid source edge")
        if row["source_node_id"] not in allowed_node_ids or row["target_node_id"] not in allowed_node_ids:
            raise ValueError("dangling package edge")
        if row["edge_id"] != canonical_edge_id(row["source_node_id"], row["edge_type"], row["target_node_id"], row["raw_operation"], row["graph_layer"]):
            raise ValueError("non-canonical edge_id")
    evidence_keys: set[tuple[str, ...]] = set()
    for row in loaded["evidence"]:
        validate_enum("target_type", row["target_type"], TARGET_TYPES)
        row["source_path"] = validate_evidence_location(row["source_path"], row["start_line"], row["end_line"], row["start_column"], row["end_column"])
        _validate_evidence_source_anchor(root, row, repository_root)
        validate_confidence(row["confidence"])
        validate_properties_json(row["properties_json"])
        target_ids = node_ids if row["target_type"] == "NODE" else edge_ids
        if row["target_id"] not in target_ids:
            raise ValueError("dangling evidence")
        key = canonical_evidence_key(row)
        if key in evidence_keys:
            raise ValueError("duplicate evidence canonical key")
        evidence_keys.add(key)
    for row in loaded["issues"]:
        validate_enum("issue_type", row["issue_type"], ISSUE_TYPES)
        validate_enum("severity", row["severity"], SEVERITIES)
        if row["source_node_id"] and row["source_node_id"] not in allowed_node_ids:
            raise ValueError("dangling issue source")
        if row["source_path"]:
            row["source_path"] = normalize_repository_path(row["source_path"])
            _validate_issue_source_anchor(root, row, repository_root)
        elif row["start_line"]:
            raise ValueError("issue start_line requires source_path")
        if not row["message"].strip():
            raise ValueError("issue message is required")
        validate_properties_json(row["properties_json"])
    for row in loaded.get("localized_texts", []):
        _validate_localized_text(row, allowed_node_ids, edge_ids)
    return {"manifest": manifest, **loaded}

def _validate_localized_text(row: dict[str, str], allowed_node_ids: set[str], edge_ids: set[str]) -> None:
    target_type = str(row["target_type"] or "").strip()
    target_id = str(row["target_id"] or "").strip()
    if target_type == "EDGE":
        if target_id not in edge_ids:
            raise ValueError("dangling localized text")
    else:
        validate_enum("target_type", target_type, NODE_TYPES)
        validate_stable_id(target_id, target_type)
        if target_id not in allowed_node_ids:
            raise ValueError("dangling localized text")
    for key in ("field_name", "locale", "value", "source_kind", "review_status", "author_name"):
        if not str(row.get(key) or "").strip():
            raise ValueError("localized text fields are required")

def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)

def publish(db: sqlite3.Connection, package: dict[str, object]) -> None:
    manifest = package["manifest"]
    source = manifest["sourceId"]
    with db:
        initialize(db)
        db.execute("DELETE FROM graph_sources WHERE source_id=?", (source,))
        db.execute("INSERT INTO graph_sources VALUES(?,?,?)", (source, manifest["packageId"], manifest["createdAt"]))
        for row in package["nodes"]:
            db.execute("INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (*row.values(), source))
        for row in package["edges"]:
            db.execute("INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)", (*row.values(), source))
        for row in package["evidence"]:
            values = list(row.values()); values[4:8] = [int(value) if value else None for value in values[4:8]]
            db.execute("INSERT INTO graph_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, source))
        for row in package["issues"]:
            values = list(row.values()); values[7] = int(values[7]) if values[7] else None
            db.execute("INSERT INTO graph_issues VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, source))
        db.execute("DELETE FROM graph_localization WHERE source_id=?", (source,))
        for row in package.get("localized_texts", []):
            created = row.get("created_at") or db.execute("SELECT datetime('now')").fetchone()[0]
            updated = row.get("updated_at") or created
            db.execute(
                """
                INSERT OR IGNORE INTO graph_localization(target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at,source_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (row["target_type"], row["target_id"], row["field_name"], row["locale"], row["value"], row["source_kind"], row["review_status"], row["author_name"], created, updated, source),
            )
        materialize(db, source)

def materialize(db: sqlite3.Connection, source_id: str) -> None:
    db.execute("DELETE FROM graph_paths WHERE source_id=?", (source_id,))
    rows = db.execute("SELECT edge_id,source_node_id,target_node_id,edge_type FROM graph_edges WHERE source_id=?", (source_id,)).fetchall()
    outgoing: dict[str, list[tuple]] = {}
    for row in rows: outgoing.setdefault(row[1], []).append(row)
    actors = {row[0] for row in db.execute("SELECT node_id FROM graph_nodes WHERE node_type IN('SCREEN','API_OPERATION','EXECUTABLE','PROCEDURE','FUNCTION','SQL_FILE')")}
    tables = {row[0] for row in db.execute("SELECT node_id FROM graph_nodes WHERE node_type='TABLE'")}
    for actor in actors:
        pending = [(actor, [])]; seen = {actor}
        while pending:
            node, path = pending.pop(0)
            for edge in outgoing.get(node, []):
                target, next_path = edge[2], path + [edge[0]]
                if target in tables and edge[3] in {"READS","REMOTE_READS","INSERTS","UPDATES","DELETES","MERGES","WRITES"}:
                    operation = "R" if edge[3] in {"READS","REMOTE_READS"} else "W"
                    key = hashlib.sha256(f"{actor}|{target}|{operation}".encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO graph_paths VALUES(?,?,?,?,?,?)", (f"path:{key}", actor, target, operation, json.dumps(next_path), source_id))
                elif target not in seen and len(next_path) < 12:
                    seen.add(target); pending.append((target, next_path))

def import_roots(roots: Iterable[Path], db_path: Path) -> dict[str, int]:
    packages = [validate_package(root) for root in roots]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        initialize(db)
        for package in packages: publish(db, package)
        problems = db.execute("PRAGMA foreign_key_check").fetchall()
        if problems: raise RuntimeError(f"foreign key check failed: {problems}")
        return {name: db.execute(f"SELECT COUNT(*) FROM graph_{name}").fetchone()[0] for name in ("nodes","edges","evidence","issues","paths")}
