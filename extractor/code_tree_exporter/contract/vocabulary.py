"""Which edge types read, write, call or contain: the one place readers look this up.

Two vocabularies exist. ``graph.sqlite`` keeps the engine names the extractors
emit (``READS_FROM``, ``WRITES_TO``, ``HANDLES_API``). ``codemap.sqlite``, the
only file the web app and the MCP server read, speaks the serving names
(``READS``/``REMOTE_READS``, ``INSERTS``/``UPDATES``/..., ``HANDLED_BY``);
``serving_edge_type`` is the only translation between them.

Code that reads ``codemap.sqlite`` uses the serving sets (``READ_EDGES``,
``WRITE_EDGES``, ...). Code that reads ``graph.sqlite`` directly (diff, the
publisher) uses the ``ANY_*`` sets, which accept both.
"""
from __future__ import annotations

# Serving vocabulary (codemap.sqlite).
READ_EDGES = frozenset({"READS", "REMOTE_READS"})
# SQL*Loader (LOADS_INTO) writes its table like an INSERT does.
WRITE_EDGES = frozenset({"INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES", "LOADS_INTO"})
COLUMN_EDGES = frozenset({"READS_COLUMN", "WRITES_COLUMN"})
# Edges a request or a job travels along on its way to the data. A message goes
# producer -PUBLISHES-> topic -CONSUMED_BY-> consumer (messages.py).
CALL_EDGES = frozenset({
    "CALLS", "CALLS_API", "HANDLED_BY", "TRIGGERS", "TRIGGERS_JOB", "RESOLVES_TO", "ENTRY_IN", "ROUTES_TO",
    "PUBLISHES", "CONSUMED_BY",
})
BUSINESS_EDGES = frozenset({"STARTS", "DEPENDS_ON"})
STRUCTURE_EDGES = frozenset({"CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE"})
# The hops a flow takes from its entry (screen, API, job) to the edge that reads or writes data.
# Every flow walk uses these through follows(): the engine's materialized flows
# (v3/publisher.py), the web code-flow view and the MCP trace (codemap/flows.py)
# and the business documents (codemap/business.py). Engine and serving names both
# appear so the one rule works on graph.sqlite and on codemap.sqlite.
FLOW_STEP_EDGES = CALL_EDGES | frozenset({
    "HANDLES_API", "STARTS", "INVOKES", "BELONGS_TO", "EXECUTES_SQL", "DEFINES_STATEMENT", "INCLUDES_FRAGMENT",
})
# CONTAINS is mostly structure (project -> screen, table -> column, file -> class); a flow
# only goes into the parts that run: a screen's UI actions, a job network's jobs, a
# method's SQL, a package's routines.
FLOW_PART_TYPES = frozenset({
    "UI_ACTION", "JOB", "INLINE_SQL", "SQL_STATEMENT", "PROCEDURE", "FUNCTION", "LOCAL_ROUTINE",
})
FOLLOW_EDGES = FLOW_STEP_EDGES | READ_EDGES | WRITE_EDGES

# Engine names (graph.sqlite) and the sets that accept both vocabularies.
ENGINE_READ_EDGES = frozenset({"READS_FROM"})
ENGINE_WRITE_EDGES = frozenset({"WRITES_TO"})
ANY_READ_EDGES = READ_EDGES | ENGINE_READ_EDGES | frozenset({"READS_COLUMN"})
ANY_WRITE_EDGES = WRITE_EDGES | ENGINE_WRITE_EDGES | frozenset({"WRITES_COLUMN", "POPULATES"})
FLOW_DATA_EDGES = ANY_READ_EDGES | ANY_WRITE_EDGES | frozenset({"USES_SEQUENCE"})

_WRITE_OPERATIONS = {"INSERT": "INSERTS", "UPDATE": "UPDATES", "DELETE": "DELETES", "MERGE": "MERGES"}
_REMOTE_TARGET_TYPES = frozenset({"EXTERNAL_DATABASE_OBJECT", "DATABASE_LINK"})


def serving_edge_type(edge_type: str, raw_operation: str = "", target_type: str = "") -> str:
    """The serving name of an engine edge (``WRITES_TO`` + ``UPDATE`` -> ``UPDATES``)."""
    if edge_type == "READS_FROM":
        return "REMOTE_READS" if target_type in _REMOTE_TARGET_TYPES else "READS"
    if edge_type == "WRITES_TO":
        return _WRITE_OPERATIONS.get(raw_operation.upper(), "WRITES")
    if edge_type == "HANDLES_API":
        return "HANDLED_BY"
    # The serving vocabulary has no INVOKES: a job running a command mode is a call.
    if edge_type == "INVOKES":
        return "CALLS"
    return edge_type


def follows(edge_type: str, target_type: str) -> bool:
    """True when ``edge_type`` (into a node of ``target_type``) is a hop of a flow, not a read or write."""
    if edge_type == "CONTAINS":
        return target_type in FLOW_PART_TYPES
    return edge_type in FLOW_STEP_EDGES


def flow_edge(edge_type: str, target_type: str) -> bool:
    """A hop or a data edge (read, write, sequence use): what a flow is made of."""
    return edge_type in FLOW_DATA_EDGES or follows(edge_type, target_type)


