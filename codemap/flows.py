"""The one flow walk: from an entry (screen, API, job, method) to the data it reads or writes.

The web code-flow view (``/flow?mode=W|R``), the business documents
(``business.GraphIndex``) and the MCP ``codemap_trace_flow`` tool all walk the
same edges (``contract.vocabulary.follows``), so they name the same tables
for the same entry. This module walks ``graph_edges`` of ``codemap.sqlite`` and
works on any connection to it (row factory or not).
"""
from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from code_tree_exporter.contract.vocabulary import READ_EDGES, WRITE_EDGES, follows

# A route longer than this is not a flow.
MAX_PATH_EDGES = 64
# States explored per node of the answer: a walk that finds little data still stops.
EXPLORE_FACTOR = 20
MIN_EXPLORE = 5000
# Edges per node of the answer: past that a graph view is a hairball and the browser stalls.
EDGES_PER_NODE = 5
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
    """Breadth-first over (node, past a read/write) states, each expanded once.

    A shared helper called from many places is walked once, not once per route
    through it (enumerating routes is exponential in the depth of the call graph).
    Every edge on some route to the data is kept; ``paths`` holds the shortest
    route per read/write edge (``out``) or per reader/writer and caller (``in``).
    """
    reverse = direction == "in"
    types: dict[str, str] = {}

    def node_type(value: str) -> str:
        if value not in types:
            row = db.execute("SELECT node_type FROM graph_nodes WHERE node_id=?", (value,)).fetchone()
            types[value] = str(row[0]) if row else ""
        return types[value]

    start = (node_id, False)
    # state -> (previous state, edge) of the shortest route to it.
    parent: dict[tuple[str, bool], tuple[tuple[str, bool], dict] | None] = {start: None}
    depth = {start: 0}
    moves: list[tuple[tuple[str, bool], dict, tuple[str, bool]]] = []
    queue = deque([start])
    column = "target_node_id" if reverse else "source_node_id"
    select = f"SELECT {', '.join(EDGE_COLUMNS)} FROM graph_edges WHERE {column}=? ORDER BY edge_id"
    explore = max(limit * EXPLORE_FACTOR, MIN_EXPLORE)
    truncated = False
    while queue:
        if len(parent) > explore:
            truncated = True
            break
        state = queue.popleft()
        if depth[state] >= MAX_PATH_EDGES:
            continue
        current, seen_terminal = state
        for row in db.execute(select, (current,)).fetchall():
            edge = dict(zip(EDGE_COLUMNS, row))
            is_terminal = edge["edge_type"] in terminals
            # A step is a hop towards the data, never another read/write (CONTAINS only into the parts that run).
            is_step = edge["edge_type"] not in DATA_EDGES and follows(edge["edge_type"], node_type(edge["target_node_id"]))
            if not (is_terminal or is_step) or not visible(edge):
                continue
            following = edge["source_node_id"] if reverse else edge["target_node_id"]
            if following == node_id:
                continue
            following_state = (following, seen_terminal or is_terminal)
            moves.append((state, edge, following_state))
            # Out of an entry the walk ends at the data; into a table it goes on to whoever reached the writer.
            if is_terminal and not reverse:
                continue
            if following_state not in parent:
                parent[following_state] = (state, edge)
                depth[following_state] = depth[state] + 1
                queue.append(following_state)
    truncated = truncated or bool(queue)

    # A move counts when it is a read/write, comes after one, or leads to a state that has such a move.
    useful: set[tuple[str, bool]] = set()
    into: dict[tuple[str, bool], list[tuple[str, bool]]] = {}
    for state, edge, following_state in moves:
        into.setdefault(following_state, []).append(state)
    pending = [state for state, edge, following_state in moves if following_state[1]]
    while pending:
        state = pending.pop()
        if state in useful:
            continue
        useful.add(state)
        pending.extend(into.get(state, ()))

    def route(state: tuple[str, bool]) -> list[dict]:
        edges: list[dict] = []
        while parent[state] is not None:
            state, edge = parent[state]
            edges.append(edge)
        return edges[::-1]

    if reverse:
        candidates = [route(state) for state in parent if state[1]]
    else:
        candidates = [[*route(state), edge] for state, edge, following_state in moves if edge["edge_type"] in terminals]
    candidates.sort(key=len)
    result = DataPaths(node_ids={node_id}, truncated=truncated)
    for path in candidates:
        endpoints = {end for edge in path for end in (edge["source_node_id"], edge["target_node_id"])}
        if len(result.node_ids | endpoints) > limit:
            result.truncated = True
            continue
        result.node_ids |= endpoints
        result.paths.append([edge["edge_id"] for edge in (reversed(path) if reverse else path)])
    # Then the other routes (a method reached through two services), nearest first, while they fit.
    for state, edge, following_state in moves:
        if not (following_state[1] or following_state in useful):
            continue
        endpoints = {edge["source_node_id"], edge["target_node_id"]}
        if len(result.node_ids | endpoints) > limit or len(result.edges) >= limit * EDGES_PER_NODE:
            result.truncated = True
            continue
        result.node_ids |= endpoints
        result.edges[edge["edge_id"]] = edge
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
