"""Import a code-tree ``graph.sqlite`` into the serving database.

The extraction engine (``code_tree_exporter``) writes one ``graph.sqlite``
per run. The API and UI read the ``graph_*`` tables of the serving database,
so this module copies the engine graph into them and turns the engine's
materialised flows (actor -> table) into the read/write paths and DATA_FLOW
edges the lineage views use.

Engine and UI use slightly different edge vocabularies; ``ui_edge_type``
maps the engine's generic edges (``READS_FROM``/``WRITES_TO``/
``HANDLES_API``) to the operation-specific ones the UI understands. Edge ids
are recomputed with ``canonical_edge_id`` for the mapped type.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from application.backend.importer.pipeline import READ_TERMINALS, WRITE_TERMINALS, initialize
from code_tree_exporter.contract.graph_contract import canonical_edge_id

SOURCE_PREFIX = "code-tree:"
_SUPPORTED_CONTRACTS = {"3.0"}
_WRITE_OPERATIONS = {
    "INSERT": "INSERTS",
    "UPDATE": "UPDATES",
    "DELETE": "DELETES",
    "MERGE": "MERGES",
}
_REMOTE_TARGET_TYPES = {"EXTERNAL_DATABASE_OBJECT", "DATABASE_LINK"}


def resolve_graph_path(path: Path) -> Path:
    """Accept either ``graph.sqlite`` or the output directory containing it."""
    path = path.expanduser().resolve()
    candidate = path / "graph.sqlite" if path.is_dir() else path
    if not candidate.is_file():
        raise ValueError(f"graph.sqlite not found: {candidate}")
    return candidate


def inspect_graph(path: Path) -> dict[str, object]:
    """Validate an engine graph without importing it."""
    graph_path = resolve_graph_path(path)
    with closing(_open_read_only(graph_path)) as graph:
        metadata = _metadata(graph)
        contract = str(metadata.get("contract_version") or "")
        if contract not in _SUPPORTED_CONTRACTS:
            raise ValueError(f"unsupported graph contract version: {contract or 'missing'}")
        counts = {
            table: graph.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("nodes", "edges", "evidence", "issues")
        }
        packages = [row[0] for row in graph.execute("SELECT DISTINCT package_key FROM nodes ORDER BY 1")]
    return {
        "graph": str(graph_path),
        "name": metadata.get("source_name"),
        "contract_version": contract,
        "generated_at": metadata.get("generated_at"),
        "packages": packages,
        "counts": counts,
    }


def ui_edge_type(edge_type: str, raw_operation: str, target_type: str) -> str:
    if edge_type == "READS_FROM":
        return "REMOTE_READS" if target_type in _REMOTE_TARGET_TYPES else "READS"
    if edge_type == "WRITES_TO":
        return _WRITE_OPERATIONS.get(raw_operation.upper(), "WRITES")
    if edge_type == "HANDLES_API":
        return "HANDLED_BY"
    return edge_type


def import_graph(path: Path, db_path: Path, input_root: Path | None = None) -> dict[str, int]:
    """Replace previously imported engine data with the graph at ``path``.

    ``input_root`` (``input-data``) optionally supplies Japanese/English table
    and column names (``tables.csv`` and ``tables/<TABLE>.csv``)."""
    summary = inspect_graph(path)
    graph_path = Path(str(summary["graph"]))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_open_read_only(graph_path)) as graph, closing(sqlite3.connect(db_path)) as db:
        graph.row_factory = sqlite3.Row
        initialize(db)
        with db:
            db.execute("PRAGMA defer_foreign_keys=ON")
            run_id = _start_run(db, summary)
            _clear_previous(db)
            created = str(summary.get("generated_at") or _now())
            _import_sources(db, graph, created)
            node_ids, node_types = _import_nodes(db, graph, created)
            edge_ids = _import_edges(db, graph, node_ids, node_types)
            _import_evidence(db, graph, node_ids, edge_ids)
            _import_issues(db, graph, node_ids)
            _import_flows(db, graph, node_ids, edge_ids)
            _import_table_names(db, input_root)
            problems = db.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key check failed: {problems[:5]}")
            counts = {
                name: db.execute(f"SELECT count(*) FROM graph_{name}").fetchone()[0]
                for name in ("nodes", "edges", "evidence", "issues", "paths")
            }
            _finish_run(db, run_id, counts)
        return counts


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _metadata(graph: sqlite3.Connection) -> dict[str, object]:
    try:
        rows = graph.execute("SELECT key, value_json FROM metadata").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"not a code-tree graph: {exc}") from exc
    return {key: json.loads(value) for key, value in rows}


def _source_id(package_key: str | None) -> str:
    return SOURCE_PREFIX + (package_key or "global")


def _clear_previous(db: sqlite3.Connection) -> None:
    pattern = SOURCE_PREFIX + "%"
    # graph_paths reference nodes from every source, so they are rebuilt.
    db.execute("DELETE FROM graph_paths")
    db.execute("DELETE FROM graph_edges WHERE graph_layer='DATA_FLOW'")
    for table in ("graph_evidence", "graph_issues", "resolution_issues", "source_artifacts", "graph_edges", "graph_nodes", "graph_sources"):
        db.execute(f"DELETE FROM {table} WHERE source_id LIKE ?", (pattern,))


def _import_sources(db: sqlite3.Connection, graph: sqlite3.Connection, created: str) -> None:
    keys = {
        row[0]
        for table in ("nodes", "edges", "evidence", "issues")
        for row in graph.execute(f"SELECT DISTINCT package_key FROM {table}")
    }
    db.executemany(
        "INSERT OR REPLACE INTO graph_sources(source_id,package_id,created_at) VALUES(?,?,?)",
        [(_source_id(key), _source_id(key), created) for key in sorted(keys, key=str)],
    )


def _import_nodes(db, graph, created: str) -> tuple[dict[int, str], dict[str, str]]:
    node_ids: dict[int, str] = {}
    node_types: dict[str, str] = {}
    for row in graph.execute("SELECT * FROM nodes ORDER BY stable_id"):
        source_id = _source_id(row["package_key"])
        node_ids[row["node_id"]] = row["stable_id"]
        node_types[row["stable_id"]] = row["node_type"]
        db.execute(
            "INSERT OR IGNORE INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["stable_id"], row["node_type"], row["technical_name"], row["qualified_name"],
                row["default_display_name"], row["system_key"], row["database_key"], row["repository_key"],
                row["graph_role"], row["confidence"], row["properties_json"] or "{}", source_id,
            ),
        )
    return node_ids, node_types


def _import_edges(db, graph, node_ids: dict[int, str], node_types: dict[str, str]) -> dict[int, str]:
    edge_ids: dict[int, str] = {}
    for row in graph.execute("SELECT * FROM edges ORDER BY stable_id"):
        source = node_ids.get(row["source_node_id"])
        target = node_ids.get(row["target_node_id"])
        if source is None or target is None:
            continue
        raw_operation = row["raw_operation"] or ""
        edge_type = ui_edge_type(row["edge_type"], raw_operation, node_types.get(target, ""))
        # The UI materialises its own DATA_FLOW layer; engine data-flow edges
        # are kept as technical links so the materialiser can follow them.
        layer = "TECHNICAL" if row["graph_layer"] == "DATA_FLOW" else row["graph_layer"]
        edge_id = canonical_edge_id(source, edge_type, target, raw_operation, layer)
        properties = json.loads(row["properties_json"] or "{}")
        if edge_type != row["edge_type"] or layer != row["graph_layer"]:
            properties.setdefault("engine_edge_type", row["edge_type"])
            properties.setdefault("engine_graph_layer", row["graph_layer"])
        cursor = db.execute(
            "INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",
            (
                edge_id, source, target, edge_type, layer, raw_operation, row["confidence"],
                json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                _source_id(row["package_key"]),
            ),
        )
        if cursor.rowcount == 0:
            # Same edge already imported (e.g. from the TECHNICAL and DATA_FLOW
            # layer): point evidence at the stored row.
            existing = db.execute(
                "SELECT edge_id FROM graph_edges WHERE source_node_id=? AND target_node_id=? AND edge_type=? AND graph_layer=? AND raw_operation=?",
                (source, target, edge_type, layer, raw_operation),
            ).fetchone()
            edge_id = existing[0] if existing else edge_id
        edge_ids[row["edge_id"]] = edge_id
    return edge_ids


def _import_evidence(db, graph, node_ids: dict[int, str], edge_ids: dict[int, str]) -> None:
    artifacts: set[tuple[str, str]] = set()
    for row in graph.execute("SELECT * FROM evidence ORDER BY stable_id"):
        target = (node_ids if row["target_type"] == "NODE" else edge_ids).get(row["target_id"])
        if target is None:
            continue
        source_id = _source_id(row["package_key"])
        start_line = row["start_line"] if row["start_line"] and row["start_line"] > 0 else None
        db.execute(
            "INSERT OR IGNORE INTO graph_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["stable_id"], row["target_type"], target, row["source_path"], start_line, row["end_line"],
                row["start_column"], row["end_column"], row["evidence_kind"], row["extractor_name"],
                row["confidence"], row["snippet"] or "", row["properties_json"] or "{}", source_id,
            ),
        )
        artifacts.add((source_id, row["source_path"]))
    db.executemany(
        "INSERT OR IGNORE INTO source_artifacts(artifact_id,source_id,source_path) VALUES(?,?,?)",
        [
            ("artifact:" + hashlib.sha256(f"{source_id}|{path}".encode()).hexdigest(), source_id, path)
            for source_id, path in sorted(artifacts)
        ],
    )


def _import_issues(db, graph, node_ids: dict[int, str]) -> None:
    for row in graph.execute("SELECT * FROM issues ORDER BY stable_id"):
        source_node = node_ids.get(row["source_node_id"]) if row["source_node_id"] is not None else None
        values = (
            row["stable_id"], row["issue_type"], row["severity"], source_node, row["raw_reference"],
            row["database_key"], row["source_path"], row["start_line"], row["message"],
            row["properties_json"] or "{}", _source_id(row["package_key"]),
        )
        db.execute("INSERT OR IGNORE INTO graph_issues VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
        db.execute(
            """
            INSERT OR IGNORE INTO resolution_issues(issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json,source_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )


def _import_flows(db, graph, node_ids: dict[int, str], edge_ids: dict[int, str]) -> None:
    """graph_paths + DATA_FLOW edges from the engine's complete flows, in the
    same shape ``pipeline.materialize`` produces for the UI."""
    steps: dict[int, list[int | None]] = {}
    for row in graph.execute("SELECT flow_id, incoming_edge_id FROM flow_steps ORDER BY flow_id, step_index"):
        steps.setdefault(row[0], []).append(row[1])
    edges = {
        row[0]: row
        for row in db.execute(
            "SELECT edge_id,source_node_id,target_node_id,edge_type,source_id,raw_operation,properties_json "
            "FROM graph_edges WHERE source_id LIKE ?",
            (SOURCE_PREFIX + "%",),
        )
    }
    flows = graph.execute(
        "SELECT flow_id,start_node_id,target_node_id FROM flows WHERE complete=1 ORDER BY stable_id"
    ).fetchall()
    for flow_id, start, target in flows:
        actor, table = node_ids.get(start), node_ids.get(target)
        path = [edge_ids.get(edge) for edge in steps.get(flow_id, []) if edge is not None]
        if actor is None or table is None or not path or None in path or path[-1] not in edges:
            continue
        terminal = edges[path[-1]]
        operation = "R" if terminal[3] in READ_TERMINALS else "W" if terminal[3] in WRITE_TERMINALS else ""
        if not operation:
            continue
        placeholders = ",".join("?" * len(path))
        evidence = [
            row[0]
            for row in db.execute(
                f"SELECT evidence_id FROM graph_evidence WHERE target_id IN ({placeholders}) ORDER BY evidence_id",
                path,
            )
        ]
        identity = f"{actor}|{table}|{operation}|{'|'.join(path)}"
        path_id = "path:" + hashlib.sha256(identity.encode()).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO graph_paths VALUES(?,?,?,?,?,?,?)",
            (path_id, actor, table, operation, json.dumps(path), json.dumps(evidence), terminal[4]),
        )
        data_edge_type = "READS" if operation == "R" else "WRITES"
        terminal_operation = terminal[5] or terminal[3]
        properties = {
            "path_id": path_id,
            "edge_path": path,
            "terminal_edge_id": terminal[0],
            "terminal_edge_type": terminal[3],
            "terminal_raw_operation": terminal[5],
            "operation": operation,
        }
        terminal_properties = json.loads(terminal[6] or "{}")
        if "semantic" in terminal_properties:
            properties["semantic"] = terminal_properties["semantic"]
        db.execute(
            "INSERT OR IGNORE INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",
            (
                canonical_edge_id(actor, data_edge_type, table, terminal_operation, "DATA_FLOW"),
                actor, table, data_edge_type, "DATA_FLOW", terminal_operation, 1.0,
                json.dumps(properties, sort_keys=True), terminal[4],
            ),
        )


def _import_table_names(db: sqlite3.Connection, input_root: Path | None) -> None:
    source_id = SOURCE_PREFIX + "localization"
    db.execute("DELETE FROM graph_localization WHERE source_id=?", (source_id,))
    if input_root is None or not (input_root / "tables.csv").is_file():
        return
    tables: dict[str, list[str]] = {}
    columns: dict[tuple[str, str], list[str]] = {}
    for node_id, node_type, name, properties in db.execute(
        "SELECT node_id,node_type,technical_name,properties_json FROM graph_nodes WHERE node_type IN ('TABLE','COLUMN') AND source_id LIKE ?",
        (SOURCE_PREFIX + "%",),
    ):
        if node_type == "TABLE":
            tables.setdefault(name.upper(), []).append(node_id)
        else:
            table = str(json.loads(properties or "{}").get("table") or "").upper()
            columns.setdefault((table, name.upper()), []).append(node_id)
    now = _now()

    def names(target_ids: list[str], ja: str, en: str) -> None:
        for target_id in target_ids:
            for locale, value in (("ja", ja), ("en", en)):
                if value.strip():
                    db.execute(
                        "INSERT OR REPLACE INTO graph_localization(target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at,source_id) "
                        "VALUES('NODE',?,'name',?,?,'IMPORTED','approved','tables',?,?,?)",
                        (target_id, locale, value.strip(), now, now, source_id),
                    )

    with (input_root / "tables.csv").open(encoding="utf-8-sig", newline="") as handle:
        table_rows = list(csv.DictReader(handle))
    for table in table_rows:
        code = (table.get("table_code") or "").strip().upper()
        names(tables.get(code, []), table.get("table_name_ja") or "", table.get("table_name_en") or "")
        column_file = input_root / "tables" / f"{table.get('table_code', '').strip()}.csv"
        if not column_file.is_file():
            continue
        with column_file.open(encoding="utf-8-sig", newline="") as handle:
            for column in csv.DictReader(handle):
                key = (code, (column.get("column_code") or "").strip().upper())
                names(columns.get(key, []), column.get("column_name_ja") or "", column.get("column_name_en") or "")


def _start_run(db: sqlite3.Connection, summary: dict[str, object]) -> str:
    started = _now()
    run_id = "run:" + hashlib.sha256(f"{summary['graph']}|{started}".encode()).hexdigest()
    db.execute(
        "INSERT INTO processing_runs(run_id,run_status,started_at,package_count,properties_json) VALUES(?,?,?,?,?)",
        (
            run_id, "RUNNING", started, len(summary["packages"]),  # type: ignore[arg-type]
            json.dumps({"source": "code-tree", "graph": summary["graph"], "name": summary.get("name")}, sort_keys=True),
        ),
    )
    return run_id


def _finish_run(db: sqlite3.Connection, run_id: str, counts: dict[str, int]) -> None:
    severity = dict(db.execute("SELECT severity,count(*) FROM graph_issues GROUP BY severity").fetchall())
    db.execute(
        """
        UPDATE processing_runs
        SET run_status=?,completed_at=?,node_count=?,edge_count=?,evidence_count=?,issue_count=?,warning_count=?,error_count=?
        WHERE run_id=?
        """,
        (
            "COMPLETED_WITH_ERRORS" if severity.get("ERROR") else "COMPLETED", _now(),
            counts["nodes"], counts["edges"], counts["evidence"], counts["issues"],
            severity.get("WARNING", 0), severity.get("ERROR", 0), run_id,
        ),
    )


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
