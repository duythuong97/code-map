"""What a change did to the system, from two extracts (base and head of a pull request).

``graph_diff`` compares two ``graph.sqlite`` by stable id and keeps what a
reviewer needs: dependencies added or removed (reads, writes, calls, API calls,
jobs), nodes whose behaviour changed (fingerprint), nodes added or removed,
schema changes (columns) and new extractor issues. With the approved impact
analysis of the change (``impact.yaml``) every change is marked planned or
unplanned, and planned items the change never touched are listed too. With the
knowledge database, the business rules, flow documents and owners affected by
the change are named so the right people review it.

    code-tree-exporter diff --base out-base --head out-head --plan docs/changes/ORD-12/impact.yaml \\
        --knowledge-db data/knowledge.sqlite --format markdown
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .contract.vocabulary import ANY_READ_EDGES, ANY_WRITE_EDGES
from .fingerprints import node_fingerprints
from .search_index import compact

STRUCTURAL = frozenset({"CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE", "USES", "DECLARES", "DEFINES"})
# graph.sqlite speaks the engine vocabulary; accept both.
WRITE_EDGES = ANY_WRITE_EDGES
READ_EDGES = ANY_READ_EDGES
DATABASE_TYPES = frozenset({
    "TABLE", "VIEW", "MATERIALIZED_VIEW", "COLUMN", "SEQUENCE", "SYNONYM", "PROCEDURE", "FUNCTION", "TRIGGER",
    "PLSQL_PACKAGE", "LOCAL_ROUTINE", "DATABASE", "DATABASE_SCHEMA", "SQL_FILE", "LOADER_CONTROL",
})
QUIET_NODE_TYPES = frozenset({"FILE", "SYSTEM", "REPOSITORY", "DATABASE", "DATABASE_SCHEMA", "DIRECTORY"})
MAX_ITEMS = 200
_DIGITS = re.compile(r"\d+")


class _Graph:
    def __init__(self, path: Path) -> None:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            self.nodes: dict[str, dict[str, Any]] = {}
            by_id: dict[int, str] = {}
            for node_id, stable_id, node_type, name, qualified, repository, database in db.execute(
                "SELECT node_id, stable_id, node_type, technical_name, qualified_name, repository_key, database_key FROM nodes"
            ):
                by_id[node_id] = stable_id
                self.nodes[stable_id] = {
                    "id": stable_id, "type": node_type, "name": name or "", "qualified_name": qualified or "",
                    "system": (database if node_type in DATABASE_TYPES and database else repository) or database or "",
                    "file": None, "line": None,
                }
            for target, path_value, line in db.execute(
                "SELECT target_id, source_path, min(start_line) FROM evidence WHERE target_type='NODE' "
                "AND evidence_kind <> 'CATALOG_ROW' GROUP BY target_id"
            ):
                node = self.nodes.get(by_id.get(target, ""))
                if node:
                    node["file"], node["line"] = path_value, line
            evidence: dict[int, tuple[str, int, str]] = {}
            for target, path_value, line, snippet in db.execute(
                "SELECT target_id, source_path, start_line, snippet FROM evidence WHERE target_type='EDGE' ORDER BY target_id, start_line"
            ):
                evidence.setdefault(target, (path_value, line, " ".join(str(snippet or "").split())[:160]))
            self.edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for edge_id, source, target, edge_type, operation in db.execute(
                "SELECT edge_id, source_node_id, target_node_id, edge_type, raw_operation FROM edges"
            ):
                key = (by_id.get(source, str(source)), edge_type, operation or "", by_id.get(target, str(target)))
                file, line, snippet = evidence.get(edge_id, (None, None, ""))
                self.edges.setdefault(key, {"file": file, "line": line, "snippet": snippet})
            # Keyed without positions: code added above an old issue moves it, it does not make it new.
            self.issues: dict[tuple[str, str, str, str], tuple[int, str]] = {}
            for issue_type, severity, path_value, line, message in db.execute(
                "SELECT issue_type, severity, source_path, start_line, message FROM issues"
            ):
                key = (issue_type, severity, path_value or "", _DIGITS.sub("#", message or ""))
                self.issues.setdefault(key, (line or 0, message or ""))
            self.fingerprints = node_fingerprints(db)
            self.metadata = {key: _loads(value) for key, value in db.execute("SELECT key, value_json FROM metadata")}


def graph_diff(base_path: Path, head_path: Path) -> dict[str, Any]:
    base, head = _Graph(base_path), _Graph(head_path)

    def node(graph: _Graph, stable_id: str) -> dict[str, Any]:
        return graph.nodes.get(stable_id) or {"id": stable_id, "type": "?", "name": stable_id, "system": "", "file": None}

    def edge_item(key: tuple[str, str, str, str], graph: _Graph) -> dict[str, Any]:
        source, edge_type, operation, target = key
        detail = graph.edges[key]
        return {
            "source": node(graph, source), "type": edge_type, "operation": operation, "target": node(graph, target),
            "kind": "write" if edge_type in WRITE_EDGES else "read" if edge_type in READ_EDGES else "call",
            "file": detail["file"], "line": detail["line"], "snippet": detail["snippet"],
        }

    def meaningful(key: tuple[str, str, str, str], graph: _Graph) -> bool:
        if key[1] not in STRUCTURAL:
            return True
        # A column added to or removed from a table is a schema change; other structure is noise.
        return key[1] == "CONTAINS" and node(graph, key[3])["type"] == "COLUMN"

    added_edges = [edge_item(key, head) for key in sorted(set(head.edges) - set(base.edges)) if meaningful(key, head)]
    removed_edges = [edge_item(key, base) for key in sorted(set(base.edges) - set(head.edges)) if meaningful(key, base)]
    added_edges = [item for item in added_edges if item["type"] != "CONTAINS"]
    removed_edges = [item for item in removed_edges if item["type"] != "CONTAINS"]
    schema_changes = [
        {**item, "change": "added"} for item in (edge_item(key, head) for key in sorted(set(head.edges) - set(base.edges))
                                                 if key[1] == "CONTAINS" and node(head, key[3])["type"] == "COLUMN")
    ] + [
        {**item, "change": "removed"} for item in (edge_item(key, base) for key in sorted(set(base.edges) - set(head.edges))
                                                   if key[1] == "CONTAINS" and node(base, key[3])["type"] == "COLUMN")
    ]
    added_nodes = [head.nodes[key] for key in sorted(set(head.nodes) - set(base.nodes)) if head.nodes[key]["type"] not in QUIET_NODE_TYPES]
    removed_nodes = [base.nodes[key] for key in sorted(set(base.nodes) - set(head.nodes)) if base.nodes[key]["type"] not in QUIET_NODE_TYPES]
    modified = [
        head.nodes[key]
        for key in sorted(set(base.nodes) & set(head.nodes))
        if base.fingerprints.get(key) != head.fingerprints.get(key)
        and head.nodes[key]["type"] not in QUIET_NODE_TYPES | {"COLUMN"}
    ]
    new_issues = [
        (key[0], key[1], key[2], head.issues[key][0], head.issues[key][1])
        for key in sorted(set(head.issues) - set(base.issues))
    ]
    changed_ids = sorted(
        {item["source"]["id"] for item in added_edges + removed_edges}
        | {item["target"]["id"] for item in added_edges + removed_edges}
        | {item["id"] for item in modified + added_nodes + removed_nodes}
        | {item["source"]["id"] for item in schema_changes}
    )
    return {
        "base": _revision(base.metadata),
        "head": _revision(head.metadata),
        "added_edges": added_edges[:MAX_ITEMS],
        "removed_edges": removed_edges[:MAX_ITEMS],
        "modified_nodes": modified[:MAX_ITEMS],
        "added_nodes": added_nodes[:MAX_ITEMS],
        "removed_nodes": removed_nodes[:MAX_ITEMS],
        "schema_changes": schema_changes[:MAX_ITEMS],
        "new_issues": [
            {"type": issue_type, "severity": severity, "file": path_value, "line": line or None, "message": message}
            for issue_type, severity, path_value, line, message in new_issues[:MAX_ITEMS]
        ],
        "changed_node_ids": changed_ids,
        "systems": sorted({head.nodes.get(key, base.nodes.get(key, {})).get("system", "") for key in changed_ids} - {""}),
        "counts": {
            "added_edges": len(added_edges), "removed_edges": len(removed_edges), "modified_nodes": len(modified),
            "added_nodes": len(added_nodes), "removed_nodes": len(removed_nodes), "schema_changes": len(schema_changes),
            "new_issues": len(new_issues),
        },
    }


# --------------------------------------------------------------------------- plan (impact.yaml)

def load_plan(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        plan = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # the extractor alone does not need PyYAML
            raise ValueError("Reading impact.yaml needs PyYAML (pip install PyYAML) or use impact.json") from exc
        plan = yaml.safe_load(text)
    if not isinstance(plan, dict) or not isinstance(plan.get("expected", []), list):
        raise ValueError(f"{path}: expected a mapping with an 'expected' list")
    items = []
    for item in plan.get("expected") or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError(f"{path}: every expected item needs a name")
        items.append({"kind": str(item.get("kind") or "node"), "name": str(item["name"]).strip(),
                      "action": str(item.get("action") or "modify")})
    return {"change": str(plan.get("change") or path.parent.name), "expected": items,
            "uncertain": [str(value) for value in plan.get("uncertain") or []]}


def compare_with_plan(diff: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Mark every change planned or unplanned; list planned items no change touched."""
    expected = plan["expected"]
    used: set[int] = set()

    def matches(node: dict[str, Any]) -> list[int]:
        keys = {compact(node.get("name", "")), compact(node.get("id", "")), compact(_owner_name(node))}
        file = (node.get("file") or "").replace("\\", "/")
        hits = []
        for index, item in enumerate(expected):
            if item["kind"] == "file":
                if file and (file == item["name"] or file.endswith("/" + item["name"].lstrip("./"))):
                    hits.append(index)
            elif compact(item["name"]) in keys or (item["name"] == node.get("id")):
                hits.append(index)
        return hits

    def planned(*nodes: dict[str, Any]) -> bool:
        hits = [index for node in nodes for index in matches(node)]
        used.update(hits)
        return bool(hits)

    unplanned: list[dict[str, Any]] = []
    for key, label in (("added_edges", "dependency added"), ("removed_edges", "dependency removed")):
        for item in diff[key]:
            item["planned"] = planned(item["source"], item["target"])
            if not item["planned"]:
                unplanned.append({"what": label, **_edge_line(item)})
    for key, label in (("modified_nodes", "behaviour changed"), ("added_nodes", "added"), ("removed_nodes", "removed")):
        for node in diff[key]:
            node["planned"] = planned(node)
            if not node["planned"]:
                unplanned.append({"what": label, "text": _node_text(node), "system": node.get("system", "")})
    for item in diff["schema_changes"]:
        item["planned"] = planned(item["source"], item["target"])
        if not item["planned"]:
            unplanned.append({"what": f"column {item['change']}", "text": f"{item['source']['name']}.{item['target']['name']}",
                              "system": item["source"].get("system", "")})
    untouched = [item for index, item in enumerate(expected) if index not in used and item["action"] != "test"]
    return {"change": plan["change"], "unplanned": unplanned, "untouched": untouched, "uncertain": plan["uncertain"]}


# --------------------------------------------------------------------------- knowledge (rules, docs, owners)

def affected_knowledge(diff: dict[str, Any], knowledge_db: Path | None, web_db: Path | None = None) -> dict[str, Any]:
    """Rules and flow documents (knowledge.sqlite) and owners (codemap.sqlite of the head) of what changed."""
    changed = set(diff["changed_node_ids"])
    result: dict[str, Any] = {"rules": [], "flows": [], "owners": []}
    if knowledge_db is not None and knowledge_db.is_file():
        with closing(sqlite3.connect(f"{knowledge_db.resolve().as_uri()}?mode=ro", uri=True)) as db:
            tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "biz_rules" in tables:
                for rule_id, title, status, node_json in db.execute(
                    "SELECT rule_id, title, status, node_ids FROM biz_rules WHERE status <> 'retired' ORDER BY rule_id"
                ):
                    nodes = set(_loads(node_json) or [])
                    if nodes & changed:
                        result["rules"].append({"rule_id": rule_id, "title": title, "status": status,
                                                "nodes": sorted(nodes & changed)})
            if "biz_flows" in tables:
                for flow_id, doc_json in db.execute("SELECT flow_id, doc_json FROM biz_flows ORDER BY flow_id"):
                    doc = _loads(doc_json) or {}
                    nodes = {node for step in doc.get("steps", []) for node in step.get("nodes", [])}
                    if nodes & changed:
                        result["flows"].append({"flow_id": flow_id, "name": doc.get("name", ""),
                                                "approved": (doc.get("summary") or {}).get("status") == "approved"})
    if web_db is not None and web_db.is_file():
        with closing(sqlite3.connect(f"{web_db.resolve().as_uri()}?mode=ro", uri=True)) as db:
            tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "graph_owners" in tables:
                owners = {system: (team, owner, contact) for system, team, owner, contact in db.execute(
                    "SELECT system, team, owner, contact FROM graph_owners")}
                result["owners"] = [
                    {"system": system, "team": owners[system][0], "owner": owners[system][1], "contact": owners[system][2]}
                    for system in diff["systems"] if system in owners
                ]
                result["systems_without_owner"] = [system for system in diff["systems"] if system not in owners]
    return result


# --------------------------------------------------------------------------- markdown

MARKER = "<!-- codemap-diff -->"


def to_markdown(diff: dict[str, Any], plan: dict[str, Any] | None = None, knowledge: dict[str, Any] | None = None) -> str:
    counts = diff["counts"]
    lines = [MARKER, "## Code Map: what this change does to the system", ""]
    lines.append(
        f"Base `{diff['base']}` → head `{diff['head']}` · systems touched: "
        + (", ".join(f"`{system}`" for system in diff["systems"]) or "none")
    )
    if not any(counts.values()):
        lines += ["", "No dependency, behaviour or schema change found by static analysis."]
    if plan is not None:
        lines += ["", f"### Against the approved impact analysis (`{plan['change']}`)"]
        if plan["unplanned"]:
            lines.append(f"⚠️ **{len(plan['unplanned'])} change(s) outside the plan** — confirm with the owners or update impact.yaml:")
            lines += [f"- {item['what']}: {item['text']}" + (f" · `{item['system']}`" if item.get("system") else "")
                      for item in plan["unplanned"][:50]]
        else:
            lines.append("✅ Every change found is in the plan.")
        if plan["untouched"]:
            lines.append("")
            lines.append("Planned but not changed (forgotten, or the plan was wrong):")
            lines += [f"- {item['kind']} `{item['name']}` ({item['action']})" for item in plan["untouched"]]
    if knowledge:
        if knowledge.get("rules"):
            lines += ["", "### Business rules to re-check"]
            lines += [f"- **{rule['rule_id']}** ({rule['status']}) {rule['title']}" for rule in knowledge["rules"]]
        if knowledge.get("flows"):
            lines += ["", "### Documented flows touched"]
            lines += [f"- {flow['name'] or flow['flow_id']}" + (" (approved documentation)" if flow["approved"] else "")
                      for flow in knowledge["flows"]]
        if knowledge.get("owners") or knowledge.get("systems_without_owner"):
            lines += ["", "### Reviewers by system"]
            lines += [f"- `{owner['system']}`: {owner['team']}" + (f" ({owner['owner']})" if owner["owner"] else "")
                      + (f" {owner['contact']}" if owner["contact"] else "") for owner in knowledge.get("owners", [])]
            lines += [f"- `{system}`: owner unknown" for system in knowledge.get("systems_without_owner", [])]

    def section(title: str, items: list[dict[str, Any]], render) -> None:
        if items:
            lines.extend(["", f"<details><summary>{title} ({len(items)})</summary>", ""])
            lines.extend(f"- {render(item)}" for item in items[:100])
            lines.extend(["", "</details>"])

    section("Dependencies added", diff["added_edges"], lambda item: _edge_line(item)["text"] + _flag(item))
    section("Dependencies removed", diff["removed_edges"], lambda item: _edge_line(item)["text"] + _flag(item))
    section("Behaviour changed", diff["modified_nodes"], lambda node: _node_text(node) + _flag(node))
    section("Added", diff["added_nodes"], lambda node: _node_text(node) + _flag(node))
    section("Removed", diff["removed_nodes"], lambda node: _node_text(node) + _flag(node))
    section("Schema", diff["schema_changes"],
            lambda item: f"column {item['change']}: `{item['source']['name']}.{item['target']['name']}`" + _flag(item))
    section("New extractor issues", diff["new_issues"],
            lambda issue: f"{issue['severity']} {issue['type']} {issue['file'] or ''}:{issue['line'] or ''} {issue['message'][:120]}")
    lines += ["", "<sub>Static analysis: SQL built at runtime, ORM queries and reflection are not visible. "
              "Generated by code-map.</sub>"]
    return "\n".join(lines) + "\n"


def _flag(item: dict[str, Any]) -> str:
    if "planned" not in item:
        return ""
    return "" if item["planned"] else " ⚠️ unplanned"


def _edge_line(item: dict[str, Any]) -> dict[str, Any]:
    operation = f"({item['operation']})" if item["operation"] else ""
    where = f" @ {item['file']}:{item['line']}" if item.get("file") else ""
    text = f"`{item['source']['name']}` -{item['type']}{operation}→ `{item['target']['name']}`{where}"
    return {"text": text, "system": item["source"].get("system", "")}


def _node_text(node: dict[str, Any]) -> str:
    where = f" @ {node['file']}:{node['line']}" if node.get("file") else ""
    return f"{node['type']} `{_owner_name(node) or node['name']}`{where}"


def _owner_name(node: dict[str, Any]) -> str:
    """Class.method / PACKAGE.PROCEDURE, the way people and impact.yaml name code."""
    name, qualified = node.get("name", ""), node.get("qualified_name", "")
    if node.get("type") in {"METHOD", "PROCEDURE", "FUNCTION", "LOCAL_ROUTINE"} and qualified:
        parts = [part for part in qualified.split("(")[0].replace(":", ".").split(".") if part]
        if len(parts) >= 2 and parts[-1] == name:
            return f"{parts[-2]}.{name}"
    return name


def _revision(metadata: dict[str, Any]) -> str:
    revisions = metadata.get("source_revisions") or {}
    commits = sorted({str(info.get("commit", ""))[:10] for info in revisions.values() if isinstance(info, dict) and info.get("commit")})
    return ", ".join(commits) or str(metadata.get("generated_at", "?"))


def _loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value
