"""The one flow walk: from an entry (screen, API, job, method) to the data it reads or writes.

The web code-flow view (``/flow?mode=W|R``), the business documents
(``business.GraphIndex``) and the MCP ``codemap_trace_flow`` tool all walk the
same edges (``contract.vocabulary.follows``), so they name the same tables
for the same entry. This module walks ``graph_edges`` of ``codemap.sqlite`` and
works on any connection to it (row factory or not).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from code_tree_exporter.contract.vocabulary import READ_EDGES, WRITE_EDGES, follows

# A path longer than this is a cycle the per-path check did not catch, not a flow.
MAX_PATH_EDGES = 64
EDGE_COLUMNS = ("edge_id", "source_node_id", "target_node_id", "edge_type", "graph_layer", "raw_operation",
                "confidence", "properties_json", "source_id")
DATA_EDGES = READ_EDGES | WRITE_EDGES
TERMINALS = {"W": WRITE_EDGES, "R": READ_EDGES, "RW": DATA_EDGES}


@dataclass
class DataPaths:
    """Edge-id paths from the entry to a read/write edge (``in``: from the reader/writer to the data)."""

    node_ids: set[str]
    edges: dict[str, dict] = field(default_factory=dict)
    paths: list[list[str]] = field(default_factory=list)
    truncated: bool = False


def data_paths(
    db: sqlite3.Connection,
    node_id: str,
    mode: str = "RW",
    direction: str = "out",
    limit: int = 500,
    visible: Callable[[dict], bool] | None = None,
) -> DataPaths:
    """Paths between ``node_id`` and the data it reads (``R``), writes (``W``) or both (``RW``).

    ``out`` starts at an entry and ends at the data; ``in`` starts at a table and
    finds who reads or writes it; ``both`` does the two. ``visible`` drops edges
    a caller does not show (the web's semantic levels); by default the derived
    actor -> table shortcuts (``DATA_FLOW``) are left out, so every path is made of
    the edges the code has. ``limit`` caps the nodes.
    """
    directions = ["in", "out"] if direction == "both" else [direction]
    result = DataPaths(node_ids={node_id})
    for item in directions:
        walk = _walk(db, node_id, TERMINALS[mode], item, limit, visible or _technical)
        if len(result.node_ids | walk.node_ids) > limit:
            result.truncated = True
            continue
        result.node_ids |= walk.node_ids
        result.edges.update(walk.edges)
        result.paths.extend(walk.paths)
        result.truncated = result.truncated or walk.truncated
    return result


def _technical(edge: dict) -> bool:
    return edge["graph_layer"] != "DATA_FLOW"


def _walk(db, node_id: str, terminals: frozenset[str], direction: str, limit: int, visible) -> DataPaths:
    reverse = direction == "in"
    types: dict[str, str] = {}

    def node_type(value: str) -> str:
        if value not in types:
            row = db.execute("SELECT node_type FROM graph_nodes WHERE node_id=?", (value,)).fetchone()
            types[value] = str(row[0]) if row else ""
        return types[value]

    result = DataPaths(node_ids={node_id})
    pending: list[tuple[str, list[dict], bool, frozenset[str]]] = [(node_id, [], False, frozenset({node_id}))]
    visited: set[tuple] = set()

    def add_path(path: list[dict]) -> None:
        endpoints = {end for edge in path for end in (edge["source_node_id"], edge["target_node_id"])} | {node_id}
        if len(result.node_ids | endpoints) > limit:
            result.truncated = True
            return
        result.node_ids |= endpoints
        for edge in path:
            result.edges[edge["edge_id"]] = edge
        result.paths.append([edge["edge_id"] for edge in (reversed(path) if reverse else path)])

    column = "target_node_id" if reverse else "source_node_id"
    select = f"SELECT {', '.join(EDGE_COLUMNS)} FROM graph_edges WHERE {column}=? ORDER BY edge_id"
    while pending and len(result.node_ids) <= limit:
        current, path, seen_terminal, path_seen = pending.pop(0)
        state = (current, seen_terminal, path_seen)
        if state in visited:
            continue
        visited.add(state)
        for row in db.execute(select, (current,)).fetchall():
            edge = dict(zip(EDGE_COLUMNS, row))
            is_terminal = edge["edge_type"] in terminals
            # A step is a hop towards the data, never another read/write (CONTAINS only into the parts that run).
            is_step = edge["edge_type"] not in DATA_EDGES and follows(edge["edge_type"], node_type(edge["target_node_id"]))
            if not (is_terminal or is_step) or not visible(edge):
                continue
            following = edge["source_node_id"] if reverse else edge["target_node_id"]
            if following in path_seen:
                continue
            next_path = [*path, edge]
            if is_terminal or seen_terminal:
                add_path(next_path)
            # Out of an entry the walk ends at the data; into a table it goes on to whoever reached the writer.
            if (not is_terminal or reverse) and len(next_path) < MAX_PATH_EDGES:
                pending.append((following, next_path, seen_terminal or is_terminal, path_seen | {following}))
    result.truncated = result.truncated or bool(pending)
    return result


def touched_data(db: sqlite3.Connection, node_id: str, limit: int = 500) -> list[tuple[str, str, list[str]]]:
    """[(table node id, terminal edge type, edge-id path)] the entry reads or writes, shortest path per pair."""
    walk = data_paths(db, node_id, "RW", "out", limit)
    best: dict[tuple[str, str], list[str]] = {}
    for path in walk.paths:
        terminal = walk.edges[path[-1]]
        key = (terminal["target_node_id"], terminal["edge_type"])
        if key not in best or len(path) < len(best[key]):
            best[key] = path
    return [(target, edge_type, path) for (target, edge_type), path in sorted(best.items(), key=lambda item: (len(item[1]), item[0]))]
