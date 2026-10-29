"""MCP tools: one per step of the AIDD workflow, not one per table.

Every answer starts with a line saying when the graph was built and whether it
is stale, cites facts as [C1]/[N1]/[E1]/[F1]/[I1]/[D1], and ends with a
"Not certain" section. Facts that are not listed are unknown, not absent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from code_tree_exporter.rag_export import compact

from .business_view import business_lines
from .retriever import EDGE_PRIORITY, GraphContext, Retriever
from .workspace import Workspace

DETAIL_BUDGET = {"brief": 6000, "normal": 16000, "full": 40000}
ENTRY_TYPES = frozenset({"SCREEN", "UI_ACTION", "API_OPERATION", "JOB", "JOB_NETWORK", "EXECUTABLE", "COMMAND_MODE"})
DATA_TYPES = frozenset({
    "TABLE", "VIEW", "MATERIALIZED_VIEW", "COLUMN", "SEQUENCE", "SYNONYM", "EXTERNAL_DATABASE_OBJECT", "DATA_FILE",
})
EXTERNAL_TYPES = frozenset({"EXTERNAL_API_OPERATION", "EXTERNAL_SYSTEM", "API_CALL_REFERENCE"})
# What static extraction cannot see; said once per answer so silence is not read as "no dependency".
BLIND_SPOTS = (
    "Static extraction does not see: SQL built at runtime (reported as DYNAMIC_* issues), column-level "
    "reads/writes from code (only SQL*Loader maps columns), ORM/LINQ queries, reflection or DI chosen at runtime, "
    "and sources not configured in the extract."
)
AMBIGUOUS_TERMS = {
    "vi": ["xử lý", "hoàn tất", "hoàn thành", "cập nhật", "kiểm tra", "thông tin", "phù hợp", "hợp lệ", "bình thường",
           "liên quan", "tương ứng", "nếu cần", "v.v", "tự động", "đồng bộ"],
    "en": ["process", "handle", "complete", "update", "appropriate", "valid", "normal", "related", "as needed", "etc",
           "sync", "automatically"],
    "ja": ["処理", "更新", "適切", "など", "等", "関連", "正常", "必要に応じて", "連携"],
}
_IDENTIFIER = re.compile(
    r"`([^`]{3,120})`"
    r"|\b((?:GET|POST|PUT|PATCH|DELETE)\s+/[\w/{}:.-]+)"
    r"|\b([A-Z][A-Z0-9]*_[A-Z0-9_]{2,})\b"
    r"|\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]+)\b"
    r"|\b([a-z]+[A-Z][A-Za-z0-9]+|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]+)\b"
)


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[Workspace, dict[str, Any]], str]
    write: bool = False
    annotations: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": not self.write,
                "destructiveHint": False,
                "idempotentHint": not self.write,
                "openWorldHint": False,
                **self.annotations,
            },
        }


class ToolError(ValueError):
    """A bad argument; reported to the model as a tool error it can correct."""


# --------------------------------------------------------------------------- shared rendering

def _detail(arguments: dict[str, Any]) -> int:
    detail = str(arguments.get("detail") or "normal")
    if detail not in DETAIL_BUDGET:
        raise ToolError("detail must be brief, normal or full")
    return DETAIL_BUDGET[detail]


def _text(arguments: dict[str, Any], name: str, *, required: bool = True, limit: int = 4000) -> str:
    value = arguments.get(name, "")
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ToolError(f"{name} is required")
    if len(value) > limit:
        raise ToolError(f"{name} is longer than {limit} characters")
    return value


def _answer(workspace: Workspace, body: list[str], uncertain: list[str]) -> str:
    lines = [f"> {workspace.status_line()}", "", *body, "", "## Not certain (verify before relying on it)"]
    lines.extend(uncertain or ["- No extractor issue on these facts."])
    lines.append(f"- {BLIND_SPOTS}")
    lines.append("- Anything not listed above is unknown to the graph, not proof that it does not exist.")
    return "\n".join(lines)


def _uncertain(workspace: Workspace, context: GraphContext) -> list[str]:
    lines: list[str] = []
    changed = workspace.changed_files()
    stale_nodes = [
        f"{context.labels[node.id]} {node.name} ({node.file})"
        for node in context.nodes
        if node.file and node.file in changed
    ]
    if stale_nodes:
        lines.append("- Declared in files changed since the extract (re-check the code): " + "; ".join(stale_nodes[:15]))
    unresolved = [
        f"{context.labels[node.id]} {node.name}"
        for node in context.nodes
        if node.type in {"UNRESOLVED_REFERENCE", "API_CALL_REFERENCE"}
    ]
    if unresolved:
        lines.append(
            "- References the extractor could not resolve to a declared target (the real callee/table may be missing): "
            + "; ".join(unresolved[:15])
        )
    if context.issues:
        kinds = sorted({str(issue["type"]) for issue in context.issues})
        lines.append(f"- Extractor issues on these nodes ({', '.join(kinds)}): see [I…] above.")
    if context.truncated:
        lines.append("- The answer was cut at the context budget; ask with detail=full or a narrower question.")
    if not context.seeds:
        lines.append("- Nothing in the graph matched the question; try exact technical names or business terms.")
    return lines


def _business(workspace: Workspace, context: GraphContext) -> list[str]:
    labels = {node.stable_id: context.labels[node.id] for node in context.nodes}
    with workspace.web() as db:
        lines = business_lines(db, [node.stable_id for node in context.nodes], labels)
    return ["", *lines] if lines else []


def _systems(retriever: Retriever, context: GraphContext) -> list[str]:
    """Impact grouped by the system each node runs in (database for data objects, repository for code)."""
    ids = [node.id for node in context.nodes]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    # codemap-rag.sqlite from an older extract has no database column.
    columns = {row[1] for row in retriever.db.execute("PRAGMA table_info(nodes)")}
    database_column = "database" if "database" in columns else "NULL"
    where = {
        int(node_id): (database if (node_type in DATA_TYPES or not repository) and database else repository) or system or "?"
        for node_id, node_type, system, repository, database in retriever.db.execute(
            f"SELECT id, type, system, repository, {database_column} FROM nodes WHERE id IN ({marks})", ids
        )
    }
    writes = {edge.dst for edge in context.edges if edge.type in {"WRITES_TO", "INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES", "LOADS_INTO"}}
    groups: dict[str, dict[str, list[str]]] = {}
    for node in context.nodes:
        kind = (
            "entry points" if node.type in ENTRY_TYPES
            else "data written" if node.type in DATA_TYPES and node.id in writes
            else "data read/used" if node.type in DATA_TYPES
            else "external" if node.type in EXTERNAL_TYPES
            else None if node.type in {"SYSTEM", "REPOSITORY", "DATABASE", "DATABASE_SCHEMA", "FILE", "COLUMN"}
            else "code"
        )
        if kind:
            groups.setdefault(where.get(node.id, "?"), {}).setdefault(kind, []).append(f"{context.labels[node.id]} {node.name}")
    lines = ["", "## Impact by system (review with each system's owner)"]
    for system in sorted(groups):
        lines.append(f"- {system}")
        for kind in ("entry points", "code", "data written", "data read/used", "external"):
            if groups[system].get(kind):
                lines.append(f"  - {kind}: {', '.join(groups[system][kind][:25])}")
    return lines


# --------------------------------------------------------------------------- tools

def search(workspace: Workspace, arguments: dict[str, Any]) -> str:
    question = _text(arguments, "question")
    with workspace.retriever() as retriever:
        context = retriever.retrieve(question, mode="ask", max_context_chars=_detail(arguments))
    return _answer(workspace, [context.text, *_business(workspace, context)], _uncertain(workspace, context))


def impact(workspace: Workspace, arguments: dict[str, Any]) -> str:
    target = _text(arguments, "target", required=False)
    files = arguments.get("files") or []
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files) or len(files) > 50:
        raise ToolError("files must be a list of up to 50 paths")
    if not target and not files:
        raise ToolError("give target (a name, table, API, procedure) or files (paths being changed)")
    budget = _detail(arguments)
    with workspace.retriever() as retriever:
        seeds: dict[int, float] = {}
        for path in files:
            for node_id, score in retriever.file_seeds(path).items():
                seeds[node_id] = max(seeds.get(node_id, 0.0), score)
        if target:
            for node_id in _target_seeds(retriever, target):
                seeds.setdefault(node_id, 1.0)
        seed_ids = [node_id for node_id, _ in sorted(seeds.items(), key=lambda item: -item[1])][:15]
        types = {node.id: node.type for node in retriever.nodes(seed_ids)}

        def dependency(edge) -> bool:
            # Structure (schema contains table, table contains column) is not a dependency; a screen
            # containing a UI action or a jobnet containing a job is.
            if edge.type not in STRUCTURAL_EDGES:
                return True
            return edge.type == "CONTAINS" and _container_type(retriever, edge.src, types) in {"SCREEN", "JOB_NETWORK"}

        # Who depends on the target (callers, readers, jobs) and what the target itself writes or calls.
        incoming, in_edges = retriever.expand(seed_ids, 3, 60, "in", keep=dependency)
        outgoing, out_edges = retriever.expand(seed_ids, 1, 30, "out", keep=dependency)
        effects = [edge for edge in out_edges if EDGE_PRIORITY.get(edge.type, 3) >= 6]
        node_ids = list(dict.fromkeys([*incoming, *(edge.dst for edge in effects)]))
        edges = list({edge.id: edge for edge in [*in_edges, *effects]}.values())
        nodes = retriever.nodes(node_ids)
        by_id = {node.id: node for node in nodes}
        seed_nodes = [by_id[node_id] for node_id in seed_ids if node_id in by_id]
        context = retriever.render(seed_nodes, nodes, edges, retriever.concepts_for_nodes(seed_ids), budget)
        systems = _systems(retriever, context)
    if not seed_ids:
        return _answer(workspace, ["No graph node matches the target or files."], _uncertain(workspace, context))
    return _answer(
        workspace,
        [context.text, *systems, *_business(workspace, context)],
        _uncertain(workspace, context),
    )


STRUCTURAL_EDGES = frozenset({"CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE", "USES"})


def _target_seeds(retriever: Retriever, target: str) -> list[int]:
    """Exact name matches when there are any (ORDER_HEADER, not its columns), else the best search hits."""
    key = compact(target.split(".")[-1] if " " not in target else target)
    whole = compact(target)
    candidates = retriever.find_nodes(target, 30)
    exact = [
        node.id for node in candidates
        if compact(node.name) in {key, whole} or compact(node.qualified_name).endswith(whole)
    ]
    if exact:
        return exact[:10]
    return [node.id for node in candidates[:5]]


def _container_type(retriever: Retriever, node_id: int, known: dict[int, str]) -> str:
    if node_id not in known:
        row = retriever.db.execute("SELECT type FROM nodes WHERE id=?", (node_id,)).fetchone()
        known[node_id] = str(row[0]) if row else ""
    return known[node_id]


def trace_flow(workspace: Workspace, arguments: dict[str, Any]) -> str:
    entry = _text(arguments, "entry")
    with workspace.retriever() as retriever:
        context = retriever.retrieve(entry, mode="flow", max_nodes=60, max_context_chars=_detail(arguments))
        systems = _systems(retriever, context)
    return _answer(workspace, [context.text, *systems, *_business(workspace, context)], _uncertain(workspace, context))


def file_context(workspace: Workspace, arguments: dict[str, Any]) -> str:
    path = _text(arguments, "path", limit=1000)
    start, end = arguments.get("start_line"), arguments.get("end_line")
    lines = None
    if start is not None or end is not None:
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ToolError("start_line and end_line must be 1-based integers with start_line <= end_line")
        lines = (start, end)
    with workspace.retriever() as retriever:
        context = retriever.retrieve("", mode="file", file=path, lines=lines, max_context_chars=_detail(arguments))
    if not context.seeds:
        return _answer(workspace, [f"The graph declares nothing in {path}."], _uncertain(workspace, context))
    return _answer(workspace, [context.text, *_business(workspace, context)], _uncertain(workspace, context))


def current_behavior(workspace: Workspace, arguments: dict[str, Any]) -> str:
    topic = _text(arguments, "topic")
    with workspace.retriever() as retriever:
        context = retriever.retrieve(topic, mode="ask", max_nodes=50, max_context_chars=_detail(arguments))
    business = _business(workspace, context)
    guide = [
        "",
        "## How to use this",
        "- Approved documentation and answered questions are what people confirmed; AI drafts are not.",
        "- Where the code ([E…]/[F…]) and the documentation ([D…]) disagree, report the difference as a question; "
        "do not decide which one is right.",
    ]
    if not business:
        guide.append("- No reviewed business documentation covers these nodes yet: everything here comes from code.")
    return _answer(workspace, [context.text, *business, *guide], _uncertain(workspace, context))


def check_terms(workspace: Workspace, arguments: dict[str, Any]) -> str:
    text = _text(arguments, "text", limit=20000)
    with workspace.retriever() as retriever:
        concepts: dict[str, Any] = {}
        for sentence in re.split(r"(?<=[.!?。\n])\s*", text):
            if sentence.strip():
                for concept in retriever.match_concepts(sentence):
                    concepts.setdefault(concept.semantic_id, concept)
        names = sorted({next(group for group in match.groups() if group) for match in _IDENTIFIER.finditer(text)})
        known, unknown = [], []
        for name in names:
            key = compact(name)
            hit = retriever.db.execute(
                "SELECT n.type, n.name FROM terms t JOIN nodes n ON n.id=t.node_id WHERE t.term=? AND t.weight>=1.5 LIMIT 1",
                (key,),
            ).fetchone() if len(key) >= 3 else None
            if not hit:
                hit = next(
                    ((node.type, node.name) for node in retriever.find_nodes(name, 1) if compact(node.name) == key),
                    None,
                )
            (known if hit else unknown).append((name, hit))
        statuses = {
            semantic_id: status
            for semantic_id, status in retriever.db.execute("SELECT DISTINCT semantic_id, status FROM concepts")
        }
    folded = text.lower()
    vague = [(locale, term) for locale, terms in AMBIGUOUS_TERMS.items() for term in terms if term.lower() in folded]
    body = ["## Business terms recognised (business dictionary)"]
    if concepts:
        for index, concept in enumerate(concepts.values(), start=1):
            status = statuses.get(concept.semantic_id, "")
            body.append(
                f"[C{index}] {' / '.join(concept.labels)} ({concept.semantic_id}"
                + (f", status {status}" if status and status != "approved" else "")
                + f") — {' / '.join(concept.definitions)}"
            )
    else:
        body.append("(none: the text uses no term from the dictionary; agree on terms before writing rules)")
    body.append("## Technical names")
    body.extend(f"- `{name}` exists in the graph as {hit[0]} {hit[1]}" for name, hit in known)
    body.extend(
        f"- `{name}` NOT found in the graph: new name, typo, or outside the scanned sources — confirm which"
        for name, _ in unknown
    )
    if not names:
        body.append("(no technical names detected)")
    body.append("## Vague wording (ask what exactly is meant)")
    body.extend(f"- \"{term}\" ({locale}): name the state, field, table or rule it means" for locale, term in vague)
    if not vague:
        body.append("(none found)")
    uncertain = []
    if unknown:
        uncertain.append(f"- {len(unknown)} technical name(s) are not in the graph.")
    return _answer(workspace, body, uncertain)


def status(workspace: Workspace, arguments: dict[str, Any]) -> str:
    report = workspace.stale()
    with workspace.retriever() as retriever:
        stats = retriever.stats()
        gaps = retriever.gaps(_detail(arguments) // 2).text
    quality = workspace.quality()
    body = ["## Knowledge base", f"- graph: {json.dumps(stats)}"]
    for revision in report.get("revisions") or []:
        if revision.get("vcs") == "none":
            body.append(f"- source {revision['source']}: not in git")
            continue
        since = revision.get("commits_since")
        body.append(
            f"- source {revision['source']}: extracted at {str(revision.get('commit', ''))[:10]} ({revision.get('branch', '')})"
            + (" with uncommitted changes" if revision.get("dirty_at_extract") else "")
            + (f"; {since} commit(s) since" if since else "; no commit since" if since == 0 else "; history unknown")
        )
    counts = report.get("counts") or {}
    if counts:
        body.append(
            f"- working tree: {counts.get('changed', 0)} changed, {counts.get('deleted', 0)} deleted, "
            f"{counts.get('added', 0)} new file(s) since extract"
        )
        for path in (report.get("changed") or [])[:20]:
            body.append(f"  - changed: {path}")
        for path in (report.get("added") or [])[:20]:
            body.append(f"  - new (not in the graph): {path}")
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else quality
    if isinstance(metrics, dict) and metrics:
        body.append("## Extraction quality")
        body.extend(f"- {key}: {value}" for key, value in sorted(metrics.items()) if isinstance(value, (int, float, str)))
    body.extend(["", gaps])
    return _answer(workspace, body, [])


_DETAIL_PROPERTY = {
    "type": "string",
    "enum": ["brief", "normal", "full"],
    "description": "Answer size: brief (~1.5k tokens), normal (~4k, default), full (~10k).",
}

TOOLS: list[Tool] = [
    Tool(
        "codemap_search",
        "Search the code map",
        "Find the screens, APIs, methods, PL/SQL, SQL files, tables, batch jobs and business terms related to a "
        "question in any language (Vietnamese, Japanese, English, or technical names). Returns numbered facts with "
        "file:line evidence and any reviewed business documentation. Use it before reading source files.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question or the names to look up."},
                "detail": _DETAIL_PROPERTY,
            },
            "required": ["question"],
        },
        search,
    ),
    Tool(
        "codemap_impact",
        "Impact analysis",
        "Who depends on a table, API, procedure, method or batch job, and what it writes or calls: callers, "
        "screens, jobs, readers and writers across systems, grouped by system so each owner can review their part. "
        "Pass the files you are about to change in `files` to analyse a change set. Always lists what is uncertain.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the thing being changed (table, API, procedure, method, job)."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Source paths (relative to the source root) that the change touches.",
                },
                "detail": _DETAIL_PROPERTY,
            },
        },
        impact,
    ),
    Tool(
        "codemap_trace_flow",
        "Trace a flow end to end",
        "Follow a screen, UI action, API or batch job downstream through every system it crosses to the data it "
        "reads and writes, with the precomputed end-to-end flows and the flow's business documentation.",
        {
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "Screen route, UI action, API (e.g. 'POST /api/orders') or job name."},
                "detail": _DETAIL_PROPERTY,
            },
            "required": ["entry"],
        },
        trace_flow,
    ),
    Tool(
        "codemap_file_context",
        "What the graph knows about a file",
        "The APIs, methods, procedures and screens declared in one source file (optionally a line range), the "
        "tables and APIs they reach, the jobs that start them and the business terms and documents linked to them.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the source root, e.g. order-db/order_flow.sql."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "detail": _DETAIL_PROPERTY,
            },
            "required": ["path"],
        },
        file_context,
    ),
    Tool(
        "codemap_current_behavior",
        "Current behaviour (as-is)",
        "How the system behaves today for a business topic: the code facts plus the business rules, flow "
        "documents and answered questions people have reviewed, each with its review status. Use it before "
        "writing or reviewing a requirement, and report every difference between requirement, documentation and "
        "code as a question.",
        {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Business topic or term, e.g. 'hủy đơn hàng' or 'CANCEL_ORDER'."},
                "detail": _DETAIL_PROPERTY,
            },
            "required": ["topic"],
        },
        current_behavior,
    ),
    Tool(
        "codemap_check_terms",
        "Check requirement wording",
        "Check a requirement or design text against the business dictionary and the graph: which business terms it "
        "uses, which technical names (tables, APIs, Class.method) do not exist in the graph, and which vague words "
        "need to be made precise.",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The requirement, user story or design text."}},
            "required": ["text"],
        },
        check_terms,
    ),
    Tool(
        "codemap_status",
        "Knowledge base status",
        "When the graph was extracted and from which commits, which files changed since (so which facts may be "
        "stale), extraction quality metrics, business terms with no linked code and extractor issues by type.",
        {"type": "object", "properties": {"detail": _DETAIL_PROPERTY}},
        status,
    ),
]
