"""Graph RAG retrieval over ``codemap-rag.sqlite`` (Python port of ``vscode-extension/src/core/retriever.ts``).

Find seed nodes for a question (business dictionary + name index), walk the
graph a few hops and render the facts as numbered lines ([C1] concepts, [N1]
nodes, [E1] relations, [F1] flows, [I1] issues) that an answer must cite.

The two implementations must give the same text for the same database;
``vscode-extension/test/retriever.test.ts`` compares them.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from code_tree_exporter.rag_export import compact, tokens

EDGE_PRIORITY: dict[str, int] = {
    "WRITES_TO": 10, "INSERTS": 10, "UPDATES": 10, "DELETES": 10, "MERGES": 10, "WRITES": 10, "LOADS_INTO": 10,
    "READS_FROM": 9, "READS": 9, "REMOTE_READS": 9,
    "HANDLES_API": 8, "HANDLED_BY": 8, "CALLS_API": 8, "STARTS": 8, "INVOKES": 8, "TRIGGERS": 8,
    "DEPENDS_ON": 7, "CALLS": 7, "USES_SEQUENCE": 6, "RESOLVES_TO": 6, "MAPS_TO": 5, "ENTRY_IN": 5, "IMPLEMENTS": 5,
    "BELONGS_TO": 3, "USES": 2, "CONTAINS": 2, "PROJECT_REFERENCE": 1,
}
TYPE_PRIOR: dict[str, float] = {
    "TABLE": 1.4, "API_OPERATION": 1.4, "PROCEDURE": 1.3, "FUNCTION": 1.2, "PLSQL_PACKAGE": 1.2, "SCREEN": 1.3,
    "UI_ACTION": 1.2, "JOB": 1.3, "JOB_NETWORK": 1.2, "COMMAND_MODE": 1.2, "TRIGGER": 1.2, "VIEW": 1.3,
    "SQL_FILE": 1.1, "METHOD": 1.0, "COLUMN": 0.7, "CSHARP_TYPE": 0.8, "FILE": 0.4, "INLINE_SQL": 0.4,
    "UNRESOLVED_REFERENCE": 0.3, "SYSTEM": 0.3, "REPOSITORY": 0.3, "DATABASE": 0.3, "DATABASE_SCHEMA": 0.3,
    "DOTNET_PROJECT": 0.3, "DOTNET_SOLUTION": 0.2,
}
WRITE_EDGES = frozenset({"WRITES_TO", "INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES", "LOADS_INTO"})
READ_EDGES = frozenset({"READS_FROM", "READS", "REMOTE_READS"})
MAX_SEEDS = 10
EDGE_SCAN_LIMIT = 500
_WORDS = re.compile(r"[\s,;:!?()\"'`\[\]{}]+")
_PLURAL = re.compile(r"^[a-z]{3,}s$")


@dataclass
class GraphNode:
    id: int
    stable_id: str
    type: str
    name: str
    qualified_name: str
    file: str | None
    line: int | None
    names: str
    doc: str


@dataclass
class GraphEdge:
    id: int
    src: int
    dst: int
    type: str
    operation: str
    file: str | None
    line: int | None
    snippet: str | None


@dataclass
class Concept:
    semantic_id: str
    labels: list[str]
    definitions: list[str]
    aliases: list[str]
    linked: list[int]


@dataclass
class GraphContext:
    text: str
    seeds: list[GraphNode]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    concepts: list[Concept]
    labels: dict[int, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


class Retriever:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.meta = {str(key): str(value) for key, value in db.execute("SELECT key, value FROM meta")}
        self.node_count = int(db.execute("SELECT count(*) FROM nodes").fetchone()[0] or 0)
        self.label_totals = {
            int(concept_id): int(count)
            for concept_id, count in db.execute(
                "SELECT concept_id, count(*) FROM concept_terms WHERE label_terms=1 GROUP BY concept_id"
            )
        }

    def stats(self) -> dict[str, int]:
        return {
            table: int(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("nodes", "edges", "concepts", "concept_links", "flows", "issues")
        }

    def retrieve(
        self,
        question: str,
        *,
        mode: str = "ask",
        max_nodes: int = 40,
        hops: int | None = None,
        max_context_chars: int = 16000,
        file: str | None = None,
        lines: tuple[int, int] | None = None,
        seed_ids: list[int] | None = None,
    ) -> GraphContext:
        hops = hops if hops is not None else (4 if mode == "flow" else 3 if mode == "impact" else 2)
        if mode == "gaps":
            return self.gaps(max_context_chars)
        concepts = [] if mode == "file" or seed_ids else self.match_concepts(question)
        if seed_ids:
            scores = {node_id: float(len(seed_ids) - position) for position, node_id in enumerate(seed_ids)}
        elif mode == "file" and file:
            scores = self.file_seeds(file, lines)
        else:
            scores = self.term_seeds(question)
        # Code linked to a matched business term ranks with the best direct name match.
        bonus = max(1.0, 0.8 * max([0.0, *scores.values()]))
        for concept in concepts:
            for node_id in concept.linked:
                scores[node_id] = scores.get(node_id, 0.0) + bonus
        ranked = sorted(scores.items(), key=lambda item: -item[1])
        cutoff = (ranked[0][1] if ranked else 0.0) * 0.3
        seeds_ranked = [node_id for node_id, score in ranked if score >= cutoff][:MAX_SEEDS]

        direction = "in" if mode == "impact" else "out" if mode == "flow" else "both"
        node_ids, edges = self.expand(seeds_ranked, hops, max_nodes, direction)
        nodes = self.nodes(list(node_ids))
        by_id = {node.id: node for node in nodes}
        seeds = [by_id[node_id] for node_id in seeds_ranked if node_id in by_id]
        all_concepts = merge_concepts(concepts, self.concepts_for_nodes(seeds_ranked))
        return self.render(seeds, nodes, edges, all_concepts, max_context_chars)

    # ------------------------------------------------------------------ seeds

    def match_concepts(self, question: str) -> list[Concept]:
        """Business dictionary hits: label phrases ("hủy đơn hàng") and code aliases ("cancelOrder")."""
        terms = query_terms(question)
        if not terms:
            return []
        rows = self.db.execute(
            f"""SELECT ct.concept_id, ct.term, ct.label_terms, c.semantic_id FROM concept_terms ct JOIN concepts c ON c.id=ct.concept_id
                WHERE ct.term IN ({_marks(terms)}) AND c.status <> 'deprecated'""",
            terms,
        ).fetchall()
        hits: dict[int, dict[str, Any]] = {}
        for concept_id, term, label_terms, semantic_id in rows:
            hit = hits.setdefault(int(concept_id), {"semantic_id": str(semantic_id), "label": [], "alias": []})
            bucket = hit["label"] if int(label_terms) else hit["alias"]
            if term not in bucket:
                bucket.append(str(term))
        # "đơn hàng" (Order) is dropped when "hủy đơn hàng" (Cancel order) matched the same words and more.
        for concept_id in list(hits):
            hit = hits[concept_id]
            dominated = any(
                other != concept_id
                and len(candidate["label"]) > len(hit["label"])
                and all(term in candidate["label"] for term in hit["label"])
                and all(_spelled_by(term, set(hit["label"])) for term in hit["alias"])
                for other, candidate in hits.items()
            )
            if dominated:
                del hits[concept_id]
        by_semantic: dict[str, float] = {}
        for concept_id, hit in hits.items():
            total = self.label_totals.get(concept_id, 0)
            label_size = len(hit["label"])
            score = 0.0
            if total and label_size == total:
                score = 2.0 * total
            elif total and label_size / total >= 0.66:
                score = float(label_size)
            # Alias terms are whole aliases ("cancelorder"), a strong signal.
            score += 3 * len([term for term in hit["alias"] if term not in hit["label"]])
            if score > by_semantic.get(hit["semantic_id"], 0.0):
                by_semantic[hit["semantic_id"]] = score
        best = max([0.0, *by_semantic.values()])
        chosen = sorted(
            ((semantic_id, score) for semantic_id, score in by_semantic.items() if score > 0 and score >= best * 0.75),
            key=lambda item: -item[1],
        )[:3]
        return [self.concept(semantic_id) for semantic_id, _ in chosen]

    def concept(self, semantic_id: str) -> Concept:
        rows = self.db.execute(
            "SELECT label, definition, aliases FROM concepts WHERE semantic_id=? ORDER BY locale", (semantic_id,)
        ).fetchall()
        aliases: dict[str, None] = {}
        for _, _, raw in rows:
            for alias in json.loads(str(raw or "[]")):
                aliases.setdefault(alias, None)
        return Concept(
            semantic_id=semantic_id,
            labels=[str(row[0]) for row in rows],
            definitions=[str(row[1]) for row in rows],
            aliases=list(aliases),
            linked=[int(row[0]) for row in self.db.execute("SELECT node_id FROM concept_links WHERE semantic_id=?", (semantic_id,))],
        )

    def concepts_for_nodes(self, ids: list[int]) -> list[Concept]:
        if not ids:
            return []
        semantic_ids: dict[str, None] = {}
        for (semantic_id,) in self.db.execute(
            f"SELECT DISTINCT semantic_id FROM concept_links WHERE node_id IN ({_marks(ids)})", ids
        ):
            semantic_ids.setdefault(str(semantic_id), None)
        return [self.concept(semantic_id) for semantic_id in semantic_ids]

    def term_seeds(self, question: str) -> dict[int, float]:
        """Name index hits weighted by rarity (idf) and node type."""
        terms = query_terms(question)
        result: dict[int, float] = {}
        if not terms:
            return result
        frequency: dict[str, int] = {}
        for term, count in self.db.execute(
            f"SELECT term, count(*) FROM terms WHERE term IN ({_marks(terms)}) GROUP BY term", terms
        ):
            frequency[str(term)] = int(count)
        common = max(50, self.node_count * 0.15)
        kept = [term for term in frequency if frequency.get(term, 0) <= common]
        if not kept:
            kept = sorted(frequency, key=lambda term: frequency.get(term, 0))[:1]
        if not kept:
            return result
        matched: dict[int, int] = {}
        for node_id, term, weight, node_type in self.db.execute(
            f"""SELECT t.node_id, t.term, t.weight, n.type FROM terms t JOIN nodes n ON n.id=t.node_id
                WHERE t.term IN ({_marks(kept)})""",
            kept,
        ):
            node_id = int(node_id)
            idf = math.log(1 + self.node_count / (frequency.get(str(term)) or 1))
            prior = TYPE_PRIOR.get(str(node_type), 1.0)
            result[node_id] = result.get(node_id, 0.0) + float(weight) * idf * prior
            matched[node_id] = matched.get(node_id, 0) + 1
        for node_id, count in matched.items():
            result[node_id] = result.get(node_id, 0.0) * (1 + 0.5 * (count - 1))
        return result

    def file_seeds(self, file: str, lines: tuple[int, int] | None = None) -> dict[int, float]:
        normalized = re.sub(r"^\.?/", "", file.replace("\\", "/"))
        rows = self.db.execute("SELECT id, type, line FROM nodes WHERE file=?", (normalized,)).fetchall()
        if not rows:
            tail = "/".join(normalized.split("/")[-3:])
            rows = self.db.execute("SELECT id, type, line FROM nodes WHERE file LIKE ?", (f"%{tail}",)).fetchall()
        result: dict[int, float] = {}
        for node_id, node_type, line in rows:
            inside = not lines or (line is not None and lines[0] <= int(line) <= lines[1])
            result[int(node_id)] = TYPE_PRIOR.get(str(node_type), 1.0) * (10 if inside else 1)
        return result

    def find_nodes(self, query: str, limit: int = 20) -> list[GraphNode]:
        """Seed nodes for a free-text query (business terms and names), best first."""
        scores = self.term_seeds(query)
        bonus = max(1.0, 0.8 * max([0.0, *scores.values()]))
        for concept in self.match_concepts(query):
            for node_id in concept.linked:
                scores[node_id] = scores.get(node_id, 0.0) + bonus
        ids = [node_id for node_id, _ in sorted(scores.items(), key=lambda item: -item[1])[:limit]]
        by_id = {node.id: node for node in self.nodes(ids)}
        return [by_id[node_id] for node_id in ids if node_id in by_id]

    def nodes_by_stable_id(self, stable_ids: Iterable[str]) -> list[GraphNode]:
        values = list(dict.fromkeys(stable_ids))
        if not values:
            return []
        found = {
            node.stable_id: node
            for node in (
                _node(row)
                for row in self.db.execute(
                    f"SELECT id, stable_id, type, name, qualified_name, file, line, names, doc FROM nodes WHERE stable_id IN ({_marks(values)})",
                    values,
                )
            )
        }
        return [found[value] for value in values if value in found]

    # ------------------------------------------------------------------ walk

    def expand(
        self,
        seeds: list[int],
        hops: int,
        max_nodes: int,
        direction: str,
        keep: Callable[[GraphEdge], bool] | None = None,
    ) -> tuple[dict[int, None], list[GraphEdge]]:
        """Bounded walk; ``keep`` (Python only, not in the TS twin) drops edges before they are counted."""
        node_ids: dict[int, None] = dict.fromkeys(seeds)
        edges: dict[int, GraphEdge] = {}
        frontier = list(seeds)
        hop = 1
        while hop <= hops and frontier and len(node_ids) < max_nodes:
            per_node = 12 if hop == 1 else 6
            following: list[int] = []
            for node_id in frontier:
                if len(node_ids) >= max_nodes:
                    break
                candidates: list[GraphEdge] = []
                if direction != "in":
                    candidates.extend(self.edge_rows("src", node_id))
                if direction != "out":
                    candidates.extend(self.edge_rows("dst", node_id))
                if keep is not None:
                    candidates = [edge for edge in candidates if keep(edge)]
                candidates.sort(key=lambda edge: -EDGE_PRIORITY.get(edge.type, 3))
                taken = 0
                for edge in candidates:
                    if taken >= per_node or len(node_ids) >= max_nodes:
                        break
                    if EDGE_PRIORITY.get(edge.type, 3) <= 2 and hop > 1:
                        continue
                    other = edge.dst if edge.src == node_id else edge.src
                    edges[edge.id] = edge
                    taken += 1
                    if other not in node_ids:
                        node_ids[other] = None
                        following.append(other)
            frontier = following
            hop += 1
        return node_ids, list(edges.values())

    def edge_rows(self, column: str, node_id: int) -> list[GraphEdge]:
        return [
            _edge(row)
            for row in self.db.execute(
                f"SELECT id, src, dst, type, operation, file, line, snippet FROM edges WHERE {column}=? LIMIT {EDGE_SCAN_LIMIT}",
                (node_id,),
            )
        ]

    def nodes(self, ids: list[int]) -> list[GraphNode]:
        if not ids:
            return []
        return [
            _node(row)
            for row in self.db.execute(
                f"SELECT id, stable_id, type, name, qualified_name, file, line, names, doc FROM nodes WHERE id IN ({_marks(ids)})",
                ids,
            )
        ]

    def flows(self, ids: list[int]) -> list[tuple]:
        if not ids:
            return []
        marks = _marks(ids)
        return self.db.execute(
            f"""SELECT id, type, start_id, target_id, operation, path, complete FROM flows
                WHERE start_id IN ({marks}) OR target_id IN ({marks}) ORDER BY complete DESC, id LIMIT 12""",
            [*ids, *ids],
        ).fetchall()

    def issues(self, ids: list[int]) -> list[tuple]:
        if not ids:
            return []
        return self.db.execute(
            f"SELECT node_id, type, severity, message, file, line FROM issues WHERE node_id IN ({_marks(ids)}) LIMIT 20",
            ids,
        ).fetchall()

    # ------------------------------------------------------------------ render

    def render(
        self, seeds: list[GraphNode], nodes: list[GraphNode], edges: list[GraphEdge], concepts: list[Concept], budget: int
    ) -> GraphContext:
        out = Budget(budget)
        seed_ids = {seed.id for seed in seeds}
        ordered = [*seeds, *(node for node in nodes if node.id not in seed_ids)]
        label = {node.id: f"N{index + 1}" for index, node in enumerate(ordered)}

        if concepts:
            out.add("## Business concepts (from the business dictionary)")
            for index, concept in enumerate(concepts):
                linked = ", ".join(label[node_id] for node_id in concept.linked if node_id in label)
                out.add(
                    f"[C{index + 1}] {' / '.join(concept.labels)} — {' / '.join(concept.definitions)}"
                    + (f" | code aliases: {', '.join(concept.aliases)}" if concept.aliases else "")
                    + (f" | implemented by: {linked}" if linked else " | implemented by: (no linked code found)")
                )
        out.add("## Code and data nodes" + (" (first entries matched the question)" if seeds else ""))
        for node in ordered:
            where = f" @ {node.file}{f':{node.line}' if node.line else ''}" if node.file else ""
            names = f" | business names: {node.names}" if node.names else ""
            doc = f" | comments: {node.doc[:300]}" if node.doc else ""
            out.add(f"[{label[node.id]}] {node.type} {display_name(node)}{names}{where}{doc}")
        if edges:
            out.add("## Relations")
            for index, edge in enumerate(sorted(edges, key=lambda item: -EDGE_PRIORITY.get(item.type, 3))):
                operation = f" ({edge.operation})" if edge.operation and edge.operation != edge.type else ""
                where = f" @ {edge.file}{f':{edge.line}' if edge.line else ''}" if edge.file else ""
                snippet = f" `{_one_line(edge.snippet)[:160]}`" if edge.snippet else ""
                out.add(f"[E{index + 1}] {label.get(edge.src)} -{edge.type}{operation}-> {label.get(edge.dst)}{where}{snippet}")
        ids = [node.id for node in ordered]
        flows = self.flows([seed.id for seed in seeds] + ids[:20])
        if flows:
            path_ids: dict[int, None] = {}
            for flow in flows:
                for node_id in json.loads(str(flow[5])):
                    path_ids.setdefault(int(node_id), None)
            names = {node.id: node for node in self.nodes(list(path_ids))}
            out.add("## End-to-end data flows (precomputed by the extractor)")
            for index, (_, flow_type, _, _, operation, path, complete) in enumerate(flows):
                chain = " → ".join(
                    (f"{label[node_id] + ' ' if node_id in label else ''}{names[node_id].type} {names[node_id].name}")
                    if node_id in names
                    else f"#{node_id}"
                    for node_id in (int(value) for value in json.loads(str(path)))
                )
                out.add(
                    f"[F{index + 1}] {flow_type}{f' {operation}' if operation else ''}{'' if int(complete or 0) else ' (incomplete)'}: {chain}"
                )
        issue_rows = self.issues(ids)
        issues = []
        if issue_rows:
            out.add("## Known gaps / issues reported by the extractor")
            for index, (node_id, issue_type, severity, message, file, line) in enumerate(issue_rows):
                where = f" @ {file}{f':{line}' if line else ''}" if file else ""
                out.add(f"[I{index + 1}] {severity} {issue_type} on {label.get(int(node_id), '?') if node_id is not None else '?'}: {message}{where}")
                issues.append({"node_id": node_id, "type": issue_type, "severity": severity, "message": message, "file": file, "line": line})
        return GraphContext(out.text(), seeds, ordered, edges, concepts, label, issues, out.truncated)

    def gaps(self, budget: int) -> GraphContext:
        """Business terms without code, jobs without executables, and other extractor issues."""
        out = Budget(budget)
        unlinked = self.db.execute(
            """SELECT c.semantic_id, group_concat(c.label, ' / '), max(c.aliases) FROM concepts c
               WHERE c.status <> 'deprecated' AND NOT EXISTS (SELECT 1 FROM concept_links l WHERE l.semantic_id=c.semantic_id)
               GROUP BY c.semantic_id"""
        ).fetchall()
        out.add("## Business concepts with no linked code")
        if not unlinked:
            out.add("(none)")
        for index, (semantic_id, labels, aliases) in enumerate(unlinked):
            out.add(f"[C{index + 1}] {labels} ({semantic_id}) aliases: {aliases}")
        out.add("## Extractor issues by type")
        for issue_type, severity, count in self.db.execute(
            "SELECT type, severity, count(*) AS n FROM issues GROUP BY type, severity ORDER BY n DESC"
        ):
            out.add(f"- {severity} {issue_type}: {count}")
        out.add("## Issue samples")
        for index, (issue_type, severity, message, file, line, node_type, name) in enumerate(
            self.db.execute(
                """SELECT i.type, i.severity, i.message, i.file, i.line, n.type AS node_type, n.name FROM issues i
                   LEFT JOIN nodes n ON n.id=i.node_id ORDER BY CASE i.severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, i.type LIMIT 60"""
            )
        ):
            where = f" @ {file}{f':{line}' if line else ''}" if file else ""
            out.add(f"[I{index + 1}] {severity} {issue_type}{f' on {node_type} {name}' if name else ''}: {message}{where}")
        return GraphContext(out.text(), [], [], [], [], truncated=out.truncated)


def query_terms(question: str) -> list[str]:
    """Query tokens (plus English singulars) and compact forms of 1-3 word windows, so
    "ORDER_HEADER", "cancel order" and "hủy đơn" match whole names and aliases."""
    terms: dict[str, None] = {}
    for token in tokens(question):
        terms.setdefault(token, None)
        if _PLURAL.match(token):
            terms.setdefault(token[:-1], None)
    words = [word for word in _WORDS.split(question) if word]
    for size in range(1, 4):
        for start in range(0, len(words) - size + 1):
            value = compact("".join(words[start:start + size]))
            if len(value) >= 3:
                terms.setdefault(value, None)
    return list(terms)[:80]


def edge_category(edge_type: str) -> str:
    if edge_type in WRITE_EDGES:
        return "write"
    if edge_type in READ_EDGES:
        return "read"
    if edge_type in {"CALLS", "CALLS_API", "HANDLES_API", "HANDLED_BY", "TRIGGERS", "RESOLVES_TO", "ENTRY_IN"}:
        return "call"
    if edge_type in {"STARTS", "INVOKES", "DEPENDS_ON"}:
        return "business"
    if edge_type in {"CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE"}:
        return "structure"
    return "other"


def display_name(node: GraphNode) -> str:
    if node.type in {"METHOD", "PROCEDURE", "FUNCTION", "LOCAL_ROUTINE"}:
        parts = [
            part
            for part in re.split(r"[./\\:]", re.sub(r"\(.*$", "", node.qualified_name))
            if part and not part.isdigit() and not re.fullmatch(r"(?i)cs|ts|js|sql|pkb|pks|pls", part)
        ]
        owner = parts[-2] if len(parts) >= 2 else ""
        if owner and owner != node.name:
            return f"{owner}.{node.name}"
    return node.name


def merge_concepts(first: list[Concept], second: list[Concept]) -> list[Concept]:
    seen = {concept.semantic_id for concept in first}
    return [*first, *(concept for concept in second if concept.semantic_id not in seen)][:8]


def _spelled_by(term: str, words: set[str]) -> bool:
    """True when an alias hit is just the label words glued together ("donhang" from "don", "hang")."""
    rest = term
    for word in sorted(words, key=len, reverse=True):
        rest = rest.replace(word, "")
    return rest == ""


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _marks(values: list) -> str:
    return ",".join("?" * len(values))


def _node(row: tuple) -> GraphNode:
    return GraphNode(
        id=int(row[0]), stable_id=str(row[1]), type=str(row[2]), name=str(row[3]), qualified_name=str(row[4] or ""),
        file=None if row[5] is None else str(row[5]), line=None if row[6] is None else int(row[6]),
        names=str(row[7] or ""), doc=str(row[8] or ""),
    )


def _edge(row: tuple) -> GraphEdge:
    return GraphEdge(
        id=int(row[0]), src=int(row[1]), dst=int(row[2]), type=str(row[3]), operation=str(row[4] or ""),
        file=None if row[5] is None else str(row[5]), line=None if row[6] is None else int(row[6]),
        snippet=None if row[7] is None else str(row[7]),
    )


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.lines: list[str] = []
        self.used = 0
        self.truncated = False

    def add(self, line: str) -> bool:
        if self.used + len(line) + 1 > self.limit:
            self.truncated = True
            return False
        self.lines.append(line)
        self.used += len(line) + 1
        return True

    def text(self) -> str:
        return "\n".join(self.lines) + ("\n(… more facts omitted: context budget reached)" if self.truncated else "")
