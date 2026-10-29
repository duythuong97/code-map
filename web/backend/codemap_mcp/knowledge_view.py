"""Rules, decisions, questions and operational facts for the nodes of an answer ([R…], [K…], [Q…])."""
from __future__ import annotations

import sqlite3

from web.backend import knowledge

from .business_view import knowledge_sections

STATUS = {
    "approved": "approved",
    "needs_review": "NEEDS REVIEW",
    "draft": "draft, NOT approved",
    "retired": "retired",
}


def _cited(node_ids: list[str], labels: dict[str, str]) -> str:
    cited = [labels[node] for node in node_ids if node in labels]
    return f" | nodes: {', '.join(cited)}" if cited else ""


def _touching(items: list[dict], stable_ids: set[str]) -> list[dict]:
    return [item for item in items if stable_ids & set(item.get("node_ids") or [])]


def rules_section(db: sqlite3.Connection, stable_ids: list[str], labels: dict[str, str]) -> list[str]:
    if not knowledge.has_table(db, "biz_rules", "kn"):
        return []
    knowledge.revalidate(db, knowledge.graph_stamp(db))
    wanted = set(stable_ids)
    rules = [rule for rule in _touching(knowledge.list_rules(db), wanted) if rule["status"] != "retired"]
    lines: list[str] = []
    if rules:
        lines.append("## Business rules (knowledge base)")
        order = {"approved": 0, "needs_review": 1, "draft": 2}
        for index, rule in enumerate(sorted(rules, key=lambda item: (order.get(item["status"], 3), item["rule_id"])), start=1):
            state = STATUS.get(rule["status"], rule["status"])
            if rule["status"] == "approved" and rule.get("approved_by"):
                state += f" by {rule['approved_by']}"
            if rule["status"] == "needs_review" and rule.get("review_reason"):
                state += f": {rule['review_reason']}"
            evidence = rule.get("evidence") or "no evidence"
            lines.append(
                f"[R{index}] {rule['rule_id']} ({state}) {rule['title']}"
                + (f" — {' '.join(rule['text'].split())[:300]}" if rule.get("text") else "")
                + f" | evidence: {evidence}{_cited(rule.get('node_ids') or [], labels)}"
            )
    decisions = [item for item in _touching(knowledge.list_decisions(db), wanted) if item["status"] in {"accepted", "proposed"}]
    if decisions:
        lines.append("## Decisions")
        for index, decision in enumerate(decisions, start=1):
            state = f"accepted by {decision['decided_by']}" if decision["status"] == "accepted" else "proposed, NOT decided"
            lines.append(
                f"[K{index}] {decision['decision_id']} ({state}) {decision['title']}"
                + (f": {' '.join(decision['decision'].split())[:300]}" if decision.get("decision") else "")
                + (f" Reason: {' '.join(decision['reason'].split())[:200]}" if decision.get("reason") else "")
                + _cited(decision.get("node_ids") or [], labels)
            )
    questions = [item for item in _touching(knowledge.list_questions(db), wanted) if item["status"] != "dropped"]
    if questions:
        lines.append("## Questions to people")
        for index, question in enumerate(questions, start=1):
            if question["status"] == "answered":
                lines.append(
                    f"[Q{index}] {question['question_id']} answered by {question['answered_by']}: {question['text']} → "
                    f"{' '.join(question['answer'].split())[:300]}"
                )
            else:
                options = " / ".join(option["title"] for option in question.get("options") or [])
                lines.append(
                    f"[Q{index}] {question['question_id']} OPEN: {question['text']}" + (f" (options: {options})" if options else "")
                )
    facts = knowledge.ops_facts(db, stable_ids)
    if facts:
        lines.append("## Operational facts (ops-facts.csv)")
        for fact in facts:
            lines.append(
                f"- {labels.get(fact['node_id'], '')} {fact['target']} {fact['kind']}: {fact['value']}"
                + (f" ({fact['note']})" if fact.get("note") else "")
            )
    return ["", *lines] if lines else []


def uncertain_lines(db: sqlite3.Connection | None, stable_ids: list[str]) -> list[str]:
    if db is None or not knowledge.has_table(db, "biz_rules", "kn"):
        return []
    wanted = set(stable_ids)
    lines = []
    review = [rule["rule_id"] for rule in _touching(knowledge.list_rules(db, status="needs_review"), wanted)]
    if review:
        lines.append(f"- Approved rules whose code changed since approval (do not rely on them): {', '.join(review)}")
    drafts = [rule["rule_id"] for rule in _touching(knowledge.list_rules(db, status="draft"), wanted)]
    if drafts:
        lines.append(f"- Draft rules not approved by anyone yet: {', '.join(drafts)}")
    open_questions = [item["question_id"] for item in _touching(knowledge.list_questions(db, status="open"), wanted)]
    if open_questions:
        lines.append(f"- Open questions on these nodes: {', '.join(open_questions)}")
    return lines


knowledge_sections.append(rules_section)
