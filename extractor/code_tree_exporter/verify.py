"""Measure the graph against ground truth people already have.

``verify_oracle``: Oracle computes PL/SQL dependencies when it compiles
(``ALL_DEPENDENCIES``); export them to CSV and compare with the graph to get
the precision and recall of the PL/SQL extraction on the real system:

    SELECT OWNER, NAME, TYPE, REFERENCED_OWNER, REFERENCED_NAME, REFERENCED_TYPE, REFERENCED_LINK_NAME
      FROM ALL_DEPENDENCIES
     WHERE OWNER IN ('APP') AND TYPE IN ('PACKAGE BODY','PROCEDURE','FUNCTION','TRIGGER','VIEW');

Oracle records dependencies per object (a package body, not each procedure),
so graph edges are rolled up to their package. Dependencies created by dynamic
SQL are invisible to Oracle too; graph-only pairs are listed for review, not
counted as errors blindly.

``sample_edges`` / ``score_sample``: a stratified sample of graph edges for a
person to mark correct/wrong, giving precision per edge type.
"""
from __future__ import annotations

import csv
import io
import math
import random
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from .decoding import read_text_auto

CODE_TYPES = {"PROCEDURE", "FUNCTION", "LOCAL_ROUTINE", "TRIGGER", "VIEW", "MATERIALIZED_VIEW", "PLSQL_PACKAGE"}
TARGET_TYPES = {
    "TABLE": "TABLE", "VIEW": "VIEW", "MATERIALIZED_VIEW": "VIEW", "SEQUENCE": "SEQUENCE", "SYNONYM": "SYNONYM",
    "PROCEDURE": "CODE", "FUNCTION": "CODE", "LOCAL_ROUTINE": "CODE", "PLSQL_PACKAGE": "CODE",
    "EXTERNAL_DATABASE_OBJECT": "TABLE", "UNRESOLVED_REFERENCE": "TABLE",
}
ORACLE_TARGETS = {
    "TABLE": "TABLE", "VIEW": "VIEW", "MATERIALIZED VIEW": "VIEW", "SEQUENCE": "SEQUENCE", "SYNONYM": "SYNONYM",
    "PACKAGE": "CODE", "PACKAGE BODY": "CODE", "PROCEDURE": "CODE", "FUNCTION": "CODE",
}
SYSTEM_OWNERS = {"SYS", "SYSTEM", "PUBLIC", "MDSYS", "CTXSYS", "XDB", "ORDSYS", "WMSYS"}
SYSTEM_PREFIXES = ("DBMS_", "UTL_", "OWA_", "HTP", "HTF", "APEX_")
SYSTEM_NAMES = {"STANDARD", "DUAL", "PLITBLM", "SYS_STUB_FOR_PURITY_ANALYSIS"}
DEPENDENCY_EDGES = {"READS_FROM", "WRITES_TO", "CALLS", "USES_SEQUENCE", "USES", "READS", "WRITES", "INSERTS",
                    "UPDATES", "DELETES", "MERGES", "REMOTE_READS"}
STRUCTURAL_EDGES = {"CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE"}


def _leaf(name: str) -> str:
    return str(name or "").split("@", 1)[0].split(".")[-1].strip().strip('"').upper()


def oracle_pairs(path: Path, owners: set[str] | None = None) -> tuple[set[tuple[str, str, str]], set[str]]:
    """(object, referenced name, kind) pairs and the objects that have any dependency."""
    rows = csv.DictReader(io.StringIO(read_text_auto(path), newline=""))
    pairs: set[tuple[str, str, str]] = set()
    objects: set[str] = set()
    for raw in rows:
        row = {str(key).strip().upper(): (value or "").strip() for key, value in raw.items() if key}
        owner, name = row.get("OWNER", "").upper(), _leaf(row.get("NAME", ""))
        if owners and owner not in owners:
            continue
        referenced_owner = row.get("REFERENCED_OWNER", "").upper()
        referenced = _leaf(row.get("REFERENCED_NAME", ""))
        kind = ORACLE_TARGETS.get(row.get("REFERENCED_TYPE", "").upper())
        if not name or not referenced or not kind:
            continue
        if referenced_owner in SYSTEM_OWNERS or referenced in SYSTEM_NAMES or referenced.startswith(SYSTEM_PREFIXES):
            continue
        if referenced == name:  # package body -> its own spec
            continue
        objects.add(name)
        pairs.add((name, referenced, kind))
    return pairs, objects


def graph_pairs(graph_path: Path) -> tuple[set[tuple[str, str, str]], set[str], dict[tuple[str, str, str], str]]:
    """The graph's dependencies rolled up to Oracle objects (package, standalone routine, trigger, view)."""
    with closing(sqlite3.connect(f"{graph_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        nodes = {
            node_id: (node_type, _leaf(name))
            for node_id, node_type, name in db.execute("SELECT node_id, node_type, technical_name FROM nodes")
        }
        package_of: dict[int, str] = {}
        for source, target, edge_type in db.execute("SELECT source_node_id, target_node_id, edge_type FROM edges"):
            if edge_type == "CONTAINS" and nodes.get(source, ("",))[0] == "PLSQL_PACKAGE":
                package_of[target] = nodes[source][1]
        evidence = {
            edge_id: f"{path}:{line}"
            for edge_id, path, line in db.execute(
                "SELECT target_id, source_path, min(start_line) FROM evidence WHERE target_type='EDGE' GROUP BY target_id"
            )
        }

        def object_of(node_id: int) -> str:
            return package_of.get(node_id) or nodes[node_id][1]

        pairs: set[tuple[str, str, str]] = set()
        where: dict[tuple[str, str, str], str] = {}
        objects: set[str] = set()
        for node_id, (node_type, _) in nodes.items():
            if node_type in CODE_TYPES:
                objects.add(object_of(node_id))
        for edge_id, source, target, edge_type in db.execute(
            "SELECT edge_id, source_node_id, target_node_id, edge_type FROM edges"
        ):
            if edge_type in STRUCTURAL_EDGES or edge_type not in DEPENDENCY_EDGES:
                continue
            if source not in nodes or target not in nodes or nodes[source][0] not in CODE_TYPES:
                continue
            kind = TARGET_TYPES.get(nodes[target][0])
            if not kind:
                continue
            name, referenced = object_of(source), object_of(target) if kind == "CODE" else nodes[target][1]
            if name == referenced or referenced == "DYNAMIC_SQL":  # the placeholder for EXECUTE IMMEDIATE
                continue
            pair = (name, referenced, kind)
            pairs.add(pair)
            where.setdefault(pair, evidence.get(edge_id, ""))
    return pairs, objects, where


def verify_oracle(graph_path: Path, dependencies_csv: Path, *, owners: set[str] | None = None) -> dict[str, Any]:
    truth, truth_objects = oracle_pairs(dependencies_csv, owners)
    found, graph_objects, where = graph_pairs(graph_path)
    scanned = truth_objects & graph_objects
    # Recall only over objects the extract scanned; objects it never saw are a coverage gap, reported apart.
    truth_scanned = {pair for pair in truth if pair[0] in scanned}
    found_scanned = {pair for pair in found if pair[0] in scanned}
    hits = truth_scanned & found_scanned
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in sorted({pair[2] for pair in truth_scanned | found_scanned}):
        expected = {pair for pair in truth_scanned if pair[2] == kind}
        got = {pair for pair in found_scanned if pair[2] == kind}
        by_kind[kind] = {
            "oracle": len(expected), "graph": len(got), "both": len(expected & got),
            "recall": _ratio(len(expected & got), len(expected)), "precision": _ratio(len(expected & got), len(got)),
        }
    missed: dict[str, list[str]] = defaultdict(list)
    for name, referenced, kind in sorted(truth_scanned - found_scanned):
        missed[name].append(f"{referenced} ({kind})")
    extra = [
        {"object": name, "references": referenced, "kind": kind, "at": where.get((name, referenced, kind), "")}
        for name, referenced, kind in sorted(found_scanned - truth_scanned)
    ]
    return {
        "objects": {"oracle": len(truth_objects), "graph": len(graph_objects), "both": len(scanned),
                    "not_scanned": sorted(truth_objects - graph_objects)[:200]},
        "pairs": {"oracle": len(truth_scanned), "graph": len(found_scanned), "both": len(hits)},
        "recall": _ratio(len(hits), len(truth_scanned)),
        "precision": _ratio(len(hits), len(found_scanned)),
        "by_kind": by_kind,
        "missed_by_object": {name: values for name, values in sorted(missed.items(), key=lambda item: -len(item[1]))[:100]},
        "graph_only": extra[:200],
        "note": "graph_only pairs can be dynamic SQL Oracle does not see, or extraction errors: review a sample.",
    }


def format_oracle_report(report: dict[str, Any]) -> str:
    objects, pairs = report["objects"], report["pairs"]
    lines = [
        f"Objects: {objects['both']} compared ({objects['oracle']} in ALL_DEPENDENCIES, {objects['graph']} in the graph; "
        f"{len(objects['not_scanned'])} not scanned by the extract)",
        f"Dependencies: recall {report['recall']}, precision {report['precision']} "
        f"({pairs['both']} matched, {pairs['oracle']} in Oracle, {pairs['graph']} in the graph)",
        "",
        "| kind | Oracle | graph | both | recall | precision |",
        "|---|---|---|---|---|---|",
    ]
    for kind, value in report["by_kind"].items():
        lines.append(f"| {kind} | {value['oracle']} | {value['graph']} | {value['both']} | {value['recall']} | {value['precision']} |")
    if report["missed_by_object"]:
        lines += ["", "Missed (in Oracle, not in the graph), by object:"]
        lines += [f"- {name}: {', '.join(values[:12])}" for name, values in list(report["missed_by_object"].items())[:30]]
    if report["graph_only"]:
        lines += ["", "Only in the graph (review: dynamic SQL or a wrong edge):"]
        lines += [f"- {item['object']} → {item['references']} ({item['kind']}) {item['at']}" for item in report["graph_only"][:30]]
    if objects["not_scanned"]:
        lines += ["", "Not scanned (add the source to the config): " + ", ".join(objects["not_scanned"][:40])]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- edge sampling

SAMPLE_COLUMNS = ["sample_id", "edge_type", "operation", "source_type", "source", "target_type", "target", "file", "line",
                  "snippet", "verdict", "note"]


def sample_edges(graph_path: Path, n: int = 50, seed: int = 1) -> list[dict[str, Any]]:
    """Up to ``n`` edges, spread over edge types, for a person to mark ``correct``, ``wrong`` or ``unsure``."""
    with closing(sqlite3.connect(f"{graph_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        nodes = {node_id: (node_type, name) for node_id, node_type, name in
                 db.execute("SELECT node_id, node_type, technical_name FROM nodes")}
        evidence = {edge_id: (path, line, snippet) for edge_id, path, line, snippet in db.execute(
            "SELECT target_id, source_path, min(start_line), snippet FROM evidence WHERE target_type='EDGE' GROUP BY target_id")}
        by_type: dict[str, list[tuple]] = defaultdict(list)
        for edge_id, source, target, edge_type, operation in db.execute(
            "SELECT edge_id, source_node_id, target_node_id, edge_type, raw_operation FROM edges ORDER BY edge_id"
        ):
            if edge_type not in STRUCTURAL_EDGES and source in nodes and target in nodes:
                by_type[edge_type].append((edge_id, source, target, operation))
    randomizer = random.Random(seed)
    picked: list[tuple[str, tuple]] = []
    pools = {edge_type: randomizer.sample(rows, len(rows)) for edge_type, rows in sorted(by_type.items())}
    while len(picked) < n and any(pools.values()):
        for edge_type in sorted(pools):
            if pools[edge_type] and len(picked) < n:
                picked.append((edge_type, pools[edge_type].pop()))
    rows = []
    for index, (edge_type, (edge_id, source, target, operation)) in enumerate(picked, start=1):
        path, line, snippet = evidence.get(edge_id, ("", "", ""))
        rows.append({
            "sample_id": index, "edge_type": edge_type, "operation": operation or "",
            "source_type": nodes[source][0], "source": nodes[source][1], "target_type": nodes[target][0],
            "target": nodes[target][1], "file": path or "", "line": line or "",
            "snippet": " ".join(str(snippet or "").split())[:200], "verdict": "", "note": "",
        })
    return rows


def write_sample(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def score_sample(path: Path) -> dict[str, Any]:
    """Precision per edge type from a reviewed sample (verdict: correct / wrong / unsure)."""
    rows = list(csv.DictReader(io.StringIO(read_text_auto(path), newline="")))
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "wrong": 0, "unsure": 0, "unreviewed": 0})
    for row in rows:
        verdict = (row.get("verdict") or "").strip().lower()
        key = verdict if verdict in {"correct", "wrong", "unsure"} else "unreviewed"
        by_type[row.get("edge_type", "?")][key] += 1
    result = {}
    total_correct = total_judged = 0
    for edge_type, counts in sorted(by_type.items()):
        judged = counts["correct"] + counts["wrong"]
        total_correct += counts["correct"]
        total_judged += judged
        result[edge_type] = {**counts, "precision": _ratio(counts["correct"], judged), "interval95": _wilson(counts["correct"], judged)}
    return {"by_type": result, "precision": _ratio(total_correct, total_judged),
            "interval95": _wilson(total_correct, total_judged), "judged": total_judged, "rows": len(rows)}


def _wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if not total:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return [round((centre - margin) / denominator, 3), round((centre + margin) / denominator, 3)]


def _ratio(found: int, total: int) -> float | None:
    return round(found / total, 3) if total else None
