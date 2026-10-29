"""Human knowledge (knowledge.sqlite) for the nodes an answer is about.

Flow documents are written by AI and reviewed by people in the web UI; every
part carries its review status so an agent can tell approved facts from
drafts. The knowledge layer (rules, decisions, owners) adds its own sections
through ``knowledge_sections``.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Iterable

STATUS_LABEL = {
    "approved": "approved",
    "edited": "edited by a person, not approved",
    "ai": "AI draft, NOT reviewed",
}
MAX_FLOW_DOCS = 4
MAX_TEXT = 500

# (web db, node stable ids, labels {stable_id: "N3"}) -> lines; the knowledge layer appends its sections here.
KnowledgeSection = Callable[[sqlite3.Connection, list[str], dict[str, str]], list[str]]
knowledge_sections: list[KnowledgeSection] = []


def _has(db: sqlite3.Connection, schema: str, table: str) -> bool:
    return db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def flow_docs_for(db: sqlite3.Connection, stable_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Stored flow documents that mention any of the nodes, most overlapping first."""
    wanted = set(stable_ids)
    if not wanted or not _has(db, "kn", "biz_flows"):
        return []
    found = []
    for flow_id, feature_id, doc_json, updated_at in db.execute(
        "SELECT flow_id, feature_id, doc_json, updated_at FROM kn.biz_flows"
    ):
        try:
            doc = json.loads(doc_json or "{}")
        except ValueError:
            continue
        nodes = {node for step in doc.get("steps", []) for node in step.get("nodes", [])}
        entry = flow_id[5:] if flow_id.startswith("flow:") else ""
        overlap = len(wanted & (nodes | {entry}))
        if overlap:
            found.append((overlap, flow_id, feature_id, doc, updated_at))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [
        {"flow_id": flow_id, "feature_id": feature_id, "doc": doc, "updated_at": updated_at}
        for _, flow_id, feature_id, doc, updated_at in found[:MAX_FLOW_DOCS]
    ]


def render_flow_docs(docs: list[dict[str, Any]], labels: dict[str, str]) -> list[str]:
    if not docs:
        return []
    lines = ["## Business documentation (knowledge base; review status in brackets)"]
    for index, item in enumerate(docs, start=1):
        doc = item["doc"]
        summary = doc.get("summary") or {}
        lines.append(
            f"[D{index}] Flow \"{doc.get('name') or item['flow_id']}\" ({item['flow_id']}) "
            f"[{_status(summary)}]: {_short(summary.get('text', '')) or '(no summary yet)'}"
        )
        for step in doc.get("steps", []):
            cited = ", ".join(labels[node] for node in step.get("nodes", []) if node in labels)
            text = _short(step.get("text", ""))
            if not (step.get("name") or text or step.get("rules")):
                continue
            actor = f" actor: {step['actor']}" if step.get("actor") else ""
            lines.append(
                f"  - step {step.get('id')} \"{step.get('name') or step.get('system', '')}\" "
                f"[{_status(step)}]{actor}: {text}"
                + (f" | nodes: {cited}" if cited else "")
            )
            for rule in step.get("rules", []):
                evidence = rule.get("evidence") or "NO EVIDENCE — needs confirmation"
                lines.append(
                    f"    - rule ({rule.get('status', 'warn')}): {rule.get('title', '')} — {_short(rule.get('text', ''), 240)} "
                    f"@ {rule.get('at', '')} {evidence}"
                )
        edge_cases = doc.get("edge_cases") or {}
        if edge_cases.get("items"):
            lines.append(f"  - edge cases [{_status(edge_cases)}]: " + "; ".join(_short(text, 160) for text in edge_cases["items"]))
        for question in doc.get("questions", []):
            if question.get("answer"):
                lines.append(
                    f"  - answered: {question.get('text', '')} → {_short(question['answer'], 300)}"
                    + (f" (by {question['answered_by']})" if question.get("answered_by") else "")
                )
            else:
                lines.append(f"  - OPEN question: {question.get('text', '')}")
    return lines


def business_lines(db: sqlite3.Connection | None, stable_ids: list[str], labels: dict[str, str]) -> list[str]:
    if db is None or not stable_ids:
        return []
    lines = render_flow_docs(flow_docs_for(db, stable_ids), labels)
    for section in knowledge_sections:
        lines.extend(section(db, stable_ids, labels))
    return lines


def _status(part: dict[str, Any]) -> str:
    status = part.get("status") or "ai"
    label = STATUS_LABEL.get(status, status)
    return f"{label} by {part['reviewed_by']}" if status == "approved" and part.get("reviewed_by") else label


def _short(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
