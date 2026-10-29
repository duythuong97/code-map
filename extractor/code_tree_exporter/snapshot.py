"""A graph as sorted text lines keyed by stable ids, for golden tests and diffs.

Numeric ids, timestamps and absolute paths are left out, so two extracts of
the same source give the same snapshot on any machine.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def graph_snapshot(graph_path: Path) -> dict[str, list[str]]:
    with closing(sqlite3.connect(f"{graph_path.resolve().as_uri()}?mode=ro", uri=True)) as graph:
        stable = {node_id: stable_id for node_id, stable_id in graph.execute("SELECT node_id, stable_id FROM nodes")}
        nodes = sorted(
            f"{node_type} {stable_id}"
            for stable_id, node_type in graph.execute("SELECT stable_id, node_type FROM nodes")
        )
        edges = sorted(
            {
                f"{stable.get(source, source)} -{edge_type}{f'({operation})' if operation else ''}-> {stable.get(target, target)}"
                for source, edge_type, operation, target in graph.execute(
                    "SELECT source_node_id, edge_type, raw_operation, target_node_id FROM edges"
                )
            }
        )
        issues = sorted(
            {
                f"{severity} {issue_type} {path or ''}:{line or ''}"
                for issue_type, severity, path, line in graph.execute(
                    "SELECT issue_type, severity, source_path, start_line FROM issues"
                )
            }
        )
        files = sorted(
            f"{source} {path} {encoding}"
            for source, path, encoding in graph.execute(
                "SELECT source_key, relative_path, actual_encoding FROM sources"
            )
        )
    return {"files": files, "nodes": nodes, "edges": edges, "issues": issues}


def write_snapshot(snapshot: dict[str, list[str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def snapshot_difference(expected: dict[str, list[str]], actual: dict[str, list[str]], limit: int = 40) -> str:
    """Human-readable +/- lines per section; empty when equal."""
    lines: list[str] = []
    for section in sorted(set(expected) | set(actual)):
        before, after = set(expected.get(section, [])), set(actual.get(section, []))
        removed, added = sorted(before - after), sorted(after - before)
        if not removed and not added:
            continue
        lines.append(f"[{section}] -{len(removed)} +{len(added)}")
        lines.extend(f"  - {item}" for item in removed[:limit])
        lines.extend(f"  + {item}" for item in added[:limit])
    return "\n".join(lines)
