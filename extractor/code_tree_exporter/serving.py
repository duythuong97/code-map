"""Write ``codemap.sqlite``: the one file the web app and the MCP server read.

It holds the serving tables (``graph_*``, see ``serving_tables.py``: stable
string ids, serving edge names, evidence, localized names, business data) and
the search index over them (``search_*``, see ``search_index.py``). Both are
built from ``graph.sqlite`` in one pass, so the two readers see one graph.

Business data comes from the ``inputData`` folder when present:

* ``semantic-dictionary.csv``  business terms, definitions and code aliases
* ``tables.csv`` / ``tables/<TABLE>.csv``  Japanese/English table and column names
* ``localized-metadata.csv``  extra localized names
* ``jobnet.csv`` / ``executable-mappings.csv``  batch job networks
* ``owners.csv`` / ``ops-facts.csv``  who owns a system, batch windows, volumes, SLAs

``semantic_tree`` (megabytes per node) stays in ``graph.sqlite``; the web app
reads it from there when a node's detail is opened.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .decoding import read_text_auto
from .fingerprints import node_fingerprints
from .search_index import compact, finish_search_tables, write_search_tables
from .serving_tables import add_business_edge, add_business_node, finish_serving_tables, write_serving_tables

# meta.schema_version; storage.py refuses a file with another version.
SCHEMA_VERSION = "3"
DATABASE_NAME = "codemap.sqlite"
_MODE_ARGUMENT = re.compile(r"--mode[=\s]+(\S+)")


def write_serving_database(
    graph_path: Path,
    output_path: Path,
    *,
    input_root: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, int]:
    """Build ``output_path`` (``codemap.sqlite``) from the engine ``graph.sqlite``."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(f"{graph_path.resolve().as_uri()}?mode=ro", uri=True)) as graph, closing(
        sqlite3.connect(temporary)
    ) as db:
        graph.row_factory = sqlite3.Row
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        with db:
            business = _BusinessData(input_root)
            node_ids = write_serving_tables(graph, db, business.root)
            business_nodes = _add_jobs(db, business)
            write_search_tables(graph, db, node_ids, business_nodes)
            _knowledge_inputs(graph, db, business)
            engine = _graph_metadata(graph)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "graph_generated_at": engine.get("generated_at", ""),
                "source_name": engine.get("source_name", ""),
                "source_root": str(source_root or engine.get("source_root") or ""),
                "input_data": str(input_root or ""),
                # {source: {vcs, revision, branch, dirty, ...}} as JSON: how old the graph is.
                "source_revisions": json.dumps(engine.get("source_revisions") or {}, ensure_ascii=False, sort_keys=True),
            }
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.executemany("INSERT INTO meta VALUES(?,?)", [(k, str(v)) for k, v in metadata.items()])
        finish_serving_tables(db)
        finish_search_tables(db)
        db.execute("ANALYZE")
        counts = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("graph_nodes", "graph_edges", "graph_paths", "search_terms", "search_concepts",
                          "search_concept_links", "search_flows", "graph_issues")
        }
    temporary.replace(output_path)
    return counts


def _graph_metadata(graph: sqlite3.Connection) -> dict[str, object]:
    return {key: json.loads(value) for key, value in graph.execute("SELECT key, value_json FROM metadata")}


class _BusinessData:
    def __init__(self, root: Path | None) -> None:
        self.root = root if root is not None and root.is_dir() else None

    def rows(self, relative: str) -> list[dict[str, str]]:
        if self.root is None or not (self.root / relative).is_file():
            return []
        text = read_text_auto(self.root / relative)
        return [{key: (value or "").strip() for key, value in row.items() if key} for row in csv.DictReader(io.StringIO(text, newline=""))]


def _add_jobs(db: sqlite3.Connection, business: _BusinessData) -> dict[str, tuple[str, str]]:
    """JOB_NETWORK/JOB nodes from jobnet.csv, linked to the code they start.

    Returns {stable id: (file, doc)} of the added nodes for the search index.
    """
    added: dict[str, tuple[str, str]] = {}
    jobs = business.rows("jobnet.csv")
    if not jobs:
        return added
    aliases: dict[str, set[str]] = {}
    for row in business.rows("executable-mappings.csv"):
        names = {compact(Path(row.get(key, "")).stem) for key in ("executable_name", "canonical_executable_name", "alias")}
        for name in names:
            aliases.setdefault(name, set()).update(names - {""})
    executables: dict[str, list[str]] = {}
    for stable_id, name in db.execute("SELECT node_id, technical_name FROM graph_nodes WHERE node_type='EXECUTABLE'"):
        for key in {compact(Path(name).stem), compact(stable_id.rsplit(":", 1)[-1].rsplit(".", 1)[0])}:
            executables.setdefault(key, []).append(stable_id)
    modes: dict[tuple[str, str], str] = {}
    for stable_id, name in db.execute("SELECT node_id, technical_name FROM graph_nodes WHERE node_type='COMMAND_MODE'"):
        parts = stable_id.split(":")
        if len(parts) >= 3:
            modes[(compact(parts[1]), compact(name))] = stable_id

    def node(stable_id: str, node_type: str, name: str, doc: str = "", properties: dict | None = None) -> str:
        if stable_id not in added:
            added[stable_id] = ("jobnet.csv", doc)
            add_business_node(db, stable_id, node_type, name, properties or {})
        return stable_id

    def issue(issue_id: str, issue_type: str, node_id: str | None, reference: str | None, message: str) -> None:
        db.execute(
            "INSERT OR IGNORE INTO graph_issues VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (issue_id, issue_type, "WARNING", node_id, reference, None, "jobnet.csv", None, message, "{}",
             "code-tree:business"),
        )

    job_ids: dict[tuple[str, str], str] = {}
    for index, row in enumerate(jobs):
        if not (row.get("jobnet_id") or "").strip() or not (row.get("job_id") or "").strip():
            # A row without ids (or a file with other headers) is reported, not allowed to abort the extract.
            issue(f"issue:jobnet-row:{index}", "INVALID_INPUT_ROW", None, None,
                  "jobnet.csv row without jobnet_id/job_id was skipped")
            continue
        network = node(f"job-network:{row['jobnet_id']}", "JOB_NETWORK", row.get("jobnet_name") or row["jobnet_id"])
        doc = f"{row.get('executable_name', '')} {row.get('arguments', '')}".strip()
        job = node(f"job:{row['jobnet_id']}:{row['job_id']}", "JOB", row.get("job_name") or row["job_id"], doc, row)
        job_ids[(row["jobnet_id"], row["job_id"])] = job
        add_business_edge(db, network, job, "CONTAINS")
        executable = compact(Path(row.get("executable_name", "")).stem)
        candidates = sorted({target for key in aliases.get(executable, {executable}) for target in executables.get(key, [])})
        mode_match = _MODE_ARGUMENT.search(row.get("arguments", ""))
        mode = next(
            (
                modes[(key, compact(mode_match.group(1)))]
                for key in sorted(aliases.get(executable, {executable}))
                if mode_match and (key, compact(mode_match.group(1))) in modes
            ),
            None,
        )
        for target in candidates:
            add_business_edge(db, job, target, "STARTS", row.get("arguments", ""))
        if mode is not None:
            # A job running a command mode is a call (contract/vocabulary.py has no INVOKES).
            add_business_edge(db, job, mode, "CALLS", row.get("arguments", ""))
        if not candidates:
            issue(f"issue:job-executable:{row['jobnet_id']}:{row['job_id']}", "JOB_EXECUTABLE_NOT_FOUND", job,
                  row.get("executable_name"),
                  f"Job {row['job_id']} runs {row.get('executable_name')} but no scanned executable matches it")
    for row in jobs:
        current = job_ids.get((row.get("jobnet_id"), row.get("job_id")))
        predecessor = job_ids.get((row.get("jobnet_id"), row.get("predecessor_job_id", "")))
        if current is not None and predecessor is not None:
            add_business_edge(db, current, predecessor, "DEPENDS_ON")
    return added


def _knowledge_inputs(graph: sqlite3.Connection, db: sqlite3.Connection, business: _BusinessData) -> None:
    """Node fingerprints (to send approved rules back to review when code changes), system owners
    (``owners.csv``) and operational facts (``ops-facts.csv``: batch windows, volumes, SLAs)."""
    db.executescript(
        """
        CREATE TABLE graph_fingerprints(node_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        CREATE TABLE graph_owners(system TEXT PRIMARY KEY, team TEXT NOT NULL, owner TEXT NOT NULL,
          contact TEXT NOT NULL, notes TEXT NOT NULL);
        CREATE TABLE graph_ops_facts(fact_id INTEGER PRIMARY KEY, target TEXT NOT NULL, node_id TEXT, kind TEXT NOT NULL,
          value TEXT NOT NULL, note TEXT NOT NULL);
        CREATE INDEX graph_ops_facts_node ON graph_ops_facts(node_id);
        """
    )
    db.executemany("INSERT INTO graph_fingerprints VALUES(?,?)", sorted(node_fingerprints(graph).items()))
    owners = {}
    for row in business.rows("owners.csv"):
        system = row.get("system", "")
        if system:
            owners[system] = (system, row.get("team", ""), row.get("owner", ""), row.get("contact", ""), row.get("notes", ""))
    db.executemany("INSERT INTO graph_owners VALUES(?,?,?,?,?)", sorted(owners.values()))
    by_stable = {node_id for (node_id,) in db.execute("SELECT node_id FROM graph_nodes")}
    by_name: dict[str, list[str]] = {}
    for node_id, name in db.execute("SELECT node_id, technical_name FROM graph_nodes WHERE node_type <> 'COLUMN'"):
        by_name.setdefault(compact(name), []).append(node_id)
    facts = []
    for row in business.rows("ops-facts.csv"):
        target = row.get("target", "")
        if not target or not row.get("kind"):
            continue
        matches = [target] if target in by_stable else by_name.get(compact(target), [])
        facts.append((target, matches[0] if len(matches) == 1 else None, row["kind"], row.get("value", ""), row.get("note", "")))
    db.executemany("INSERT INTO graph_ops_facts(target,node_id,kind,value,note) VALUES(?,?,?,?,?)", facts)


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
