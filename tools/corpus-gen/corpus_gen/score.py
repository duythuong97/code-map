"""Score an extract against the corpus truth, and an impact analysis against a scenario.

``score_extract``  (problem A, extractor quality): for every truth edge, is it in
the graph? Recall per pattern and per expectation. A missed ``detectable`` edge
is a regression; ``undetectable`` ones measure blind spots honestly.

``score_impact``   (problem B, AIDD workflow): an impact.yaml written by an agent
(or ``graph_impact``: what the graph alone reaches, no LLM) against the scenario's
true impact. Each miss is classified: the graph has the dependency (the agent or
retrieval missed it) or the graph lacks it (the extractor missed it).

Matching works on the ``search_*`` tables of ``codemap.sqlite`` (graph + jobnet jobs) by technical names.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

WRITE = {"WRITES_TO", "INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES", "LOADS_INTO", "WRITES_COLUMN"}
READ = {"READS_FROM", "READS", "REMOTE_READS", "READS_COLUMN", "USES_SEQUENCE"}
STRUCTURE = {"CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE"}
# Nodes a dependency may pass through without being a hop of its own (method → inline SQL → table).
PASS_THROUGH = {
    "INLINE_SQL", "API_CALL_REFERENCE", "EXECUTABLE", "EXECUTABLE_ENTRY_POINT", "UNRESOLVED_REFERENCE",
    "SQL_STATEMENT", "STORED_PROCEDURE_CALL", "DATABASE_LINK",
}
KIND_TYPES = {
    "table": {"TABLE", "VIEW", "MATERIALIZED_VIEW"},
    "external": {"EXTERNAL_DATABASE_OBJECT", "TABLE", "UNRESOLVED_REFERENCE"},
    "sequence": {"SEQUENCE"},
    "procedure": {"PROCEDURE", "FUNCTION", "LOCAL_ROUTINE"},
    "trigger": {"TRIGGER"},
    "method": {"METHOD"},
    "api": {"API_OPERATION"},
    "screen": {"SCREEN"},
    "component": {"ANGULAR_COMPONENT"},
    "ui_action": {"UI_ACTION"},
    "angular_service": {"ANGULAR_SERVICE"},
    "job": {"JOB"},
    "command_mode": {"COMMAND_MODE"},
    "sql_file": {"SQL_FILE"},
    "loader": {"LOADER_CONTROL"},
    "file": set(),
}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _route(text: str) -> str:
    """'DELETE /api/invoices/{id}' and 'DELETE api/invoices/:id' compare equal."""
    text = re.sub(r":(\w+)", r"{\1}", str(text))
    text = re.sub(r"\{[^}]*\}", "{}", text)
    return re.sub(r"[\s/]+", "/", text.strip().lower()).strip("/")


class GraphIndex:
    def __init__(self, serving_path: Path) -> None:
        with closing(sqlite3.connect(f"{serving_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            self.nodes = {
                node_id: {"id": node_id, "stable": stable, "type": node_type, "name": name or "", "qualified": qualified or "",
                          "file": file}
                for node_id, stable, node_type, name, qualified, file in db.execute(
                    "SELECT id, stable_id, type, name, qualified_name, file FROM search_nodes"
                )
            }
            self.out: dict[int, list[tuple[int, str]]] = defaultdict(list)
            self.into: dict[int, list[tuple[int, str]]] = defaultdict(list)
            for src, dst, edge_type in db.execute("SELECT src, dst, type FROM search_edges"):
                self.out[src].append((dst, edge_type))
                self.into[dst].append((src, edge_type))
            self.issues: dict[int, set[str]] = defaultdict(set)
            for node_id, issue_type in db.execute("SELECT node_id, type FROM search_issues WHERE node_id IS NOT NULL"):
                self.issues[node_id].add(issue_type)
        self.by_type: dict[str, list[int]] = defaultdict(list)
        for node in self.nodes.values():
            self.by_type[node["type"]].append(node["id"])
        self._cache: dict[tuple[str, str], list[int]] = {}

    def find(self, kind: str, name: str) -> list[int]:
        key = (kind, name)
        if key not in self._cache:
            self._cache[key] = self._find(kind, name)
        return self._cache[key]

    def _find(self, kind: str, name: str) -> list[int]:
        types = KIND_TYPES.get(kind, set())
        candidates = [node_id for node_type in types for node_id in self.by_type.get(node_type, [])]
        if kind == "file":
            return [node["id"] for node in self.nodes.values() if node["file"] and str(node["file"]).endswith(name)]
        if kind == "api":
            wanted = _route(name)
            return [node_id for node_id in candidates
                    if _route(self.nodes[node_id]["name"]) == wanted or _route(self.nodes[node_id]["name"]).endswith(wanted)]
        if kind == "screen":
            wanted = _route(name)
            return [node_id for node_id in candidates if _route(self.nodes[node_id]["stable"].split(":", 2)[-1]) == wanted]
        owner, _, short = name.rpartition(".") if kind in {"procedure", "method"} else ("", "", name)
        hits = []
        for node_id in candidates:
            node = self.nodes[node_id]
            if _key(node["name"]) != _key(short or name):
                continue
            if owner and _key(owner) not in _key(node["qualified"]) and _key(owner) not in _key(node["stable"]):
                continue
            hits.append(node_id)
        return hits

    def connected(self, sources: list[int], targets: list[int], kind: str, max_hops: int = 3) -> bool:
        """A dependency of the given class from any source to any target (through pass-through nodes)."""
        wanted = set(targets)
        if not wanted or not sources:
            return False
        if kind == "contains":
            # Structure is recorded in either direction (screen BELONGS_TO component, screen CONTAINS action).
            return any(other in wanted for source in sources for other, _ in [*self.out[source], *self.into[source]])
        frontier = [(source, 0) for source in sources]
        seen = set(sources)
        while frontier:
            node, hops = frontier.pop()
            for other, edge_type in self.out[node]:
                if edge_type in STRUCTURE:
                    continue
                if other in wanted and _edge_matches(kind, edge_type):
                    return True
                if hops + 1 < max_hops and other not in seen and self.nodes.get(other, {}).get("type") in PASS_THROUGH:
                    seen.add(other)
                    frontier.append((other, hops + 1))
        return False

    def reach_in(self, starts: list[int], max_nodes: int = 2000) -> set[int]:
        """Everything with a dependency path to the starts (what the graph alone says is affected)."""
        seen = set(starts)
        queue = list(starts)
        while queue and len(seen) < max_nodes:
            node = queue.pop(0)
            for other, edge_type in self.into[node]:
                if edge_type in STRUCTURE and self.nodes.get(other, {}).get("type") not in {"SCREEN", "JOB_NETWORK"}:
                    continue
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        return seen


# Which source folder declares a truth endpoint; an edge is scored only when both ends were extracted.
_KIND_SOURCE = {"api": "api", "method": "api", "command_mode": "batch", "job": "batch"}


def _within(edge: dict[str, Any], sources: set[str]) -> bool:
    folder = edge["file"].split("/", 1)[0]
    if edge["file"] != "jobnet.csv" and folder not in sources and not any(folder.startswith(item) for item in sources):
        return False
    for side in ("source", "target"):
        needed = _KIND_SOURCE.get(edge[side]["kind"])
        if needed and not any(item.startswith(needed) for item in sources):
            return False
    return True


def _edge_matches(kind: str, edge_type: str) -> bool:
    if kind == "write":
        return edge_type in WRITE
    if kind == "read":
        return edge_type in READ or edge_type in WRITE  # MERGE/INSERT…SELECT may be recorded as the write only
    return True  # call / api: any dependency edge


def load_truth(corpus: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (corpus / "truth" / "edges.jsonl").read_text(encoding="utf-8").splitlines() if line]


def score_extract(corpus: Path, output: Path, *, only_sources: set[str] | None = None) -> dict[str, Any]:
    """Recall of every truth edge in the extract, by pattern and by expectation."""
    graph = GraphIndex(output / "codemap.sqlite")
    rows = []
    for edge in load_truth(corpus):
        if only_sources and not _within(edge, only_sources):
            continue
        sources = graph.find(edge["source"]["kind"], edge["source"]["name"])
        targets = graph.find(edge["target"]["kind"], edge["target"]["name"])
        found = graph.connected(sources, targets, edge["class"])
        reason = "" if found else ("source not in graph" if not sources else "target not in graph" if not targets else "no edge")
        # A blind spot is acceptable only when the graph says so (an issue on the node), never silently.
        flagged = sorted({issue for node_id in sources for issue in graph.issues.get(node_id, ())})
        rows.append({**edge, "found": found, "reason": reason, "flagged": flagged})
    by_pattern: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_pattern.setdefault(row["pattern"], {"expected": row["expected"], "total": 0, "found": 0, "flagged": 0,
                                                        "missed": []})
        entry["total"] += 1
        entry["found"] += int(row["found"])
        entry["flagged"] += int(not row["found"] and bool(row["flagged"]))
        if not row["found"]:
            entry["missed"].append(f"{row['source']['name']} -{row['class']}-> {row['target']['name']} ({row['reason']})")
    by_expected: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = by_expected.setdefault(row["expected"], {"total": 0, "found": 0})
        entry["total"] += 1
        entry["found"] += int(row["found"])
    regressions = [row for row in rows if row["expected"] == "detectable" and not row["found"]]
    silent = [row for row in rows if row["expected"] != "detectable" and not row["found"] and not row["flagged"]]
    return {
        "total": len(rows),
        "found": sum(row["found"] for row in rows),
        "recall": _ratio(sum(row["found"] for row in rows), len(rows)),
        "by_expected": {key: {**value, "recall": _ratio(value["found"], value["total"])} for key, value in by_expected.items()},
        "by_pattern": {key: {**value, "recall": _ratio(value["found"], value["total"]), "missed": value["missed"][:10]}
                       for key, value in sorted(by_pattern.items())},
        "regressions": [f"{row['pattern']}: {row['source']['name']} -{row['class']}-> {row['target']['name']} ({row['reason']})"
                        for row in regressions],
        "silent_blind_spots": [f"{row['pattern']}: {row['source']['name']} -{row['class']}-> {row['target']['name']}"
                               for row in silent],
    }


# --------------------------------------------------------------------------- impact (problem B)

def load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a mapping")
    return value


def answer_items(answer: dict[str, Any]) -> list[dict[str, str]]:
    """Items of an impact.yaml (``expected``) or of a bare list of names."""
    items = answer.get("expected") if isinstance(answer, dict) else answer
    result = []
    for item in items or []:
        if isinstance(item, str):
            item = {"kind": "node", "name": item}
        if isinstance(item, dict) and item.get("name"):
            result.append({"kind": str(item.get("kind") or "node"), "name": str(item["name"])})
    return result


def score_impact(scenario: dict[str, Any], answer: list[dict[str, str]], output: Path) -> dict[str, Any]:
    graph = GraphIndex(output / "codemap.sqlite")
    truth = [*scenario["truth"]["must_change"], *scenario["truth"].get("affected", [])]
    target_ids = graph.find(scenario["target"]["kind"], scenario["target"]["name"])
    reachable = graph.reach_in(target_ids)
    answer_ids = {node_id for item in answer for node_id in _answer_nodes(graph, item)}
    answer_keys = {_key(item["name"].rpartition(".")[2]) for item in answer} | {_key(item["name"]) for item in answer}
    rows = []
    for item in truth:
        ids = graph.find(item["kind"], item["name"])
        named = bool(answer_ids & set(ids)) if ids else (_key(item["name"]) in answer_keys or
                                                         _key(item["name"].rpartition(".")[2]) in answer_keys)
        if named:
            reason = ""
        elif not ids:
            reason = "not in graph (extractor)"
        elif set(ids) & reachable:
            reason = "in graph, reachable: retrieval or agent missed it"
        else:
            reason = "in graph, no dependency path: extractor missed the edge"
        rows.append({**item, "found": named, "reason": reason})
    truth_ids = {node_id for item in truth for node_id in graph.find(item["kind"], item["name"])}
    extra = [graph.nodes[node_id]["name"] for node_id in sorted(answer_ids - truth_ids - set(target_ids))]
    found = sum(row["found"] for row in rows)
    by_expected: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = by_expected.setdefault(row.get("expected", "detectable"), {"total": 0, "found": 0})
        entry["total"] += 1
        entry["found"] += int(row["found"])
    return {
        "scenario": scenario["id"],
        "recall": _ratio(found, len(rows)),
        "found": found,
        "total": len(rows),
        "by_expected": {key: {**value, "recall": _ratio(value["found"], value["total"])} for key, value in by_expected.items()},
        "missed": [f"{row['kind']} {row['name']} ({row['reason']})" for row in rows if not row["found"]],
        "not_in_truth": extra[:50],
    }


def graph_impact(scenario: dict[str, Any], output: Path) -> list[dict[str, str]]:
    """The no-LLM baseline: every node the graph says depends on the target."""
    graph = GraphIndex(output / "codemap.sqlite")
    starts = graph.find(scenario["target"]["kind"], scenario["target"]["name"])
    reached = graph.reach_in(starts)
    if scenario.get("direction", "in") != "in" or scenario["target"]["kind"] in {"procedure", "method"}:
        for start in starts:
            reached.update(other for other, edge_type in graph.out[start] if edge_type not in STRUCTURE)
    return [{"kind": "node", "name": graph.nodes[node_id]["stable"]} for node_id in sorted(reached)]


def _answer_nodes(graph: GraphIndex, item: dict[str, str]) -> list[int]:
    stable = [node_id for node_id, node in graph.nodes.items() if node["stable"] == item["name"]]
    if stable:
        return stable
    kinds = [item["kind"]] if item["kind"] in KIND_TYPES else [
        "table", "procedure", "method", "api", "screen", "ui_action", "job", "trigger", "sql_file", "loader", "command_mode",
        "angular_service", "component", "external",
    ]
    return [node_id for kind in kinds for node_id in graph.find(kind, item["name"])]


def _ratio(found: int, total: int) -> float | None:
    return round(found / total, 3) if total else None


def format_extract_report(report: dict[str, Any]) -> str:
    lines = [f"Truth edges found: {report['found']}/{report['total']} (recall {report['recall']})", ""]
    for expected, value in sorted(report["by_expected"].items()):
        lines.append(f"- {expected}: {value['found']}/{value['total']} (recall {value['recall']})")
    lines += ["", "| pattern | expected | found | recall | missed but flagged |", "|---|---|---|---|---|"]
    for pattern, value in report["by_pattern"].items():
        lines.append(f"| {pattern} | {value['expected']} | {value['found']}/{value['total']} | {value['recall']} | "
                     f"{value['flagged']} |")
    if report["silent_blind_spots"]:
        lines += ["", f"Blind spots missed WITHOUT any issue (silent): {len(report['silent_blind_spots'])}"]
        lines += [f"- {item}" for item in report["silent_blind_spots"][:40]]
    if report["regressions"]:
        lines += ["", f"Regressions (detectable but missed): {len(report['regressions'])}"]
        lines += [f"- {item}" for item in report["regressions"][:40]]
    return "\n".join(lines) + "\n"
