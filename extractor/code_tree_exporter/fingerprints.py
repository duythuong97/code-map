"""A behaviour fingerprint per node: changes when what the node does changes, not when lines move.

The hash covers the node's semantic tree (calls, data effects, conditions)
without source positions, and its outgoing edges (type, operation, target).
Reformatting, comments and code added above a method leave it unchanged; a
new condition, a new table write or a changed callee change it.

Rules and approved documentation store the fingerprints of the nodes they
describe; when a later extract gives a node a different fingerprint, what was
approved about it goes back to review (``web/backend/knowledge.py``).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable

_POSITION_KEYS = frozenset(
    {"line", "start_line", "end_line", "start_column", "end_column", "column", "location", "source", "offset", "span"}
)


def node_fingerprints(graph: sqlite3.Connection) -> dict[str, str]:
    """{stable_id: fingerprint} for every node of an engine graph.sqlite."""
    stable: dict[int, str] = {}
    trees: dict[int, Any] = {}
    for node_id, stable_id, properties in graph.execute("SELECT node_id, stable_id, properties_json FROM nodes"):
        stable[node_id] = stable_id
        try:
            tree = json.loads(properties or "{}").get("semantic_tree")
        except (ValueError, AttributeError):
            tree = None
        if tree:
            trees[node_id] = _without_positions(tree)
    outgoing: dict[int, list[str]] = {}
    for source, target, edge_type, operation in graph.execute(
        "SELECT source_node_id, target_node_id, edge_type, raw_operation FROM edges"
    ):
        outgoing.setdefault(source, []).append(f"{edge_type}|{operation or ''}|{stable.get(target, target)}")
    result: dict[str, str] = {}
    for node_id, stable_id in stable.items():
        payload = json.dumps(
            {"tree": trees.get(node_id), "edges": sorted(outgoing.get(node_id, []))},
            sort_keys=True,
            ensure_ascii=False,
        )
        result[stable_id] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return result


def combined(fingerprints: dict[str, str], node_ids: Iterable[str]) -> str:
    """One fingerprint for a set of nodes (a rule, an approved document part); MISSING marks a deleted node."""
    parts = sorted(f"{node_id}={fingerprints.get(node_id, 'MISSING')}" for node_id in set(node_ids))
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]


def _without_positions(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_positions(item) for key, item in value.items() if key not in _POSITION_KEYS}
    if isinstance(value, list):
        return [_without_positions(item) for item in value]
    return value
