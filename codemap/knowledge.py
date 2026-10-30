"""The knowledge people own: business rules, decisions and questions, kept in knowledge.sqlite.

Code facts come from the extract; this module keeps what only people can say,
keyed by graph node ids so it survives re-extracts:

* ``biz_rules``      RULE-ORD-012: a business rule, its evidence and the nodes that implement it;
* ``biz_decisions``  DEC-0007: a decision with its options and reason (ADR-style);
* ``biz_questions``  Q-0012: a question for a person, with options, until it is answered.

AI may only propose (rules ``draft``, decisions ``proposed``, questions ``open``);
a person approves. An approved rule stores the behaviour fingerprint of its
nodes (``code_tree_exporter/fingerprints.py``); after an extract that changes
any of them, ``revalidate`` moves the rule to ``needs_review`` with the reason,
so approved knowledge never silently describes code that changed.

Every function takes a connection opened by ``storage.open_web`` (codemap.sqlite
read-only + knowledge attached as ``kn``); none needs Flask.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from code_tree_exporter.fingerprints import combined

RULE_STATUSES = ("draft", "approved", "needs_review", "retired")
RULE_SOURCES = ("code", "sme", "doc", "ai")
DECISION_STATUSES = ("proposed", "accepted", "rejected", "superseded")
QUESTION_STATUSES = ("open", "answered", "dropped")
MAX_TEXT = 8000
MAX_NODES = 50



class KnowledgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- graph lookups

def has_table(db: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    return db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def fingerprints(db: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, str]:
    values = list(dict.fromkeys(node_ids))
    if not values or not has_table(db, "graph_fingerprints"):
        return {}
    found: dict[str, str] = {}
    for start in range(0, len(values), 400):
        chunk = values[start:start + 400]
        marks = ",".join("?" * len(chunk))
        found.update(db.execute(f"SELECT node_id, fingerprint FROM graph_fingerprints WHERE node_id IN ({marks})", chunk).fetchall())
    return found


def fingerprint_of(db: sqlite3.Connection, node_ids: Iterable[str]) -> str:
    """Combined fingerprint of nodes now; "" when this extract has no fingerprints (older extract)."""
    values = list(dict.fromkeys(node_ids))
    if not values or not has_table(db, "graph_fingerprints"):
        return ""
    return combined(fingerprints(db, values), values)


def existing_nodes(db: sqlite3.Connection, node_ids: Iterable[str]) -> list[str]:
    values = list(dict.fromkeys(node_ids))
    if not values:
        return []
    found: set[str] = set()
    for start in range(0, len(values), 400):
        chunk = values[start:start + 400]
        marks = ",".join("?" * len(chunk))
        found.update(row[0] for row in db.execute(f"SELECT node_id FROM graph_nodes WHERE node_id IN ({marks})", chunk))
    return [value for value in values if value in found]


def node_names(db: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, str]:
    values = list(dict.fromkeys(node_ids))
    names: dict[str, str] = {}
    for start in range(0, len(values), 400):
        chunk = values[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for node_id, node_type, name in db.execute(
            f"SELECT node_id, node_type, technical_name FROM graph_nodes WHERE node_id IN ({marks})", chunk
        ):
            names[node_id] = f"{node_type} {name}"
    return names


def resolve_references(db: sqlite3.Connection, references: Iterable[str]) -> tuple[list[str], list[str]]:
    """Node ids for references that are node ids or technical names ("ORDER_HEADER", "PKG_ORDER.CREATE_ORDER",
    "POST /api/orders"); returns (resolved ids, references that matched nothing or more than one node)."""
    resolved: list[str] = []
    unresolved: list[str] = []
    for reference in references:
        reference = str(reference).strip()
        if not reference:
            continue
        if ":" in reference and existing_nodes(db, [reference]):
            resolved.append(reference)
            continue
        name = reference.split(": ", 1)[1] if re.match(r"^[a-z_]+: ", reference) else reference
        tail = name.rsplit(".", 1)[-1] if " " not in name else name
        rows = db.execute(
            "SELECT node_id, technical_name, qualified_name FROM graph_nodes WHERE node_type <> 'COLUMN' AND "
            "(upper(technical_name)=upper(?) OR upper(technical_name)=upper(?))",
            (name, tail),
        ).fetchall()
        if len(rows) > 1 and tail != name:
            owner = name.rsplit(".", 1)[0].lower()
            rows = [row for row in rows if owner in str(row[2]).lower()] or rows
        if len(rows) == 1:
            resolved.append(rows[0][0])
        else:
            unresolved.append(reference)
    return list(dict.fromkeys(resolved)), unresolved


# --------------------------------------------------------------------------- ids and validation

def _next_id(db: sqlite3.Connection, table: str, column: str, prefix: str, width: int) -> str:
    numbers = [
        int(match.group(1))
        for (value,) in db.execute(f"SELECT {column} FROM kn.{table} WHERE {column} LIKE ?", (prefix + "%",))
        for match in [re.match(re.escape(prefix) + r"(\d+)$", value)]
        if match
    ]
    return f"{prefix}{(max(numbers) + 1) if numbers else 1:0{width}d}"


def _text(value: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit:
        raise KnowledgeError("invalid_knowledge", f"{field} must be text up to {limit} characters")
    value = value.strip()
    if required and not value:
        raise KnowledgeError("invalid_knowledge", f"{field} is required")
    return value


def _list(value: Any, field: str, *, limit: int = MAX_NODES) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, str) for item in value):
        raise KnowledgeError("invalid_knowledge", f"{field} must be a list of up to {limit} strings")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _domain(value: Any) -> str:
    domain = re.sub(r"[^A-Z0-9]", "", str(value or "GEN").upper())[:8]
    return domain or "GEN"


def _nodes(db: sqlite3.Connection, references: list[str]) -> list[str]:
    resolved, unresolved = resolve_references(db, references)
    if unresolved:
        raise KnowledgeError(
            "unknown_nodes", "Not in the graph (or ambiguous): " + ", ".join(unresolved[:20]) + ". Use node ids or exact names."
        )
    return resolved


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("node_ids", "requirement_refs", "test_refs", "rule_ids"):
        if key in item:
            item[key] = _loads(item[key])
    if "options_json" in item:
        item["options"] = _loads(item.pop("options_json"))
    return item


def _loads(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


# --------------------------------------------------------------------------- rules

def list_rules(db: sqlite3.Connection, *, status: str | None = None, node_id: str | None = None,
               domain: str | None = None) -> list[dict[str, Any]]:
    query, params = "SELECT * FROM kn.biz_rules WHERE 1=1", []
    if status:
        query += " AND status=?"
        params.append(status)
    if domain:
        query += " AND domain=?"
        params.append(_domain(domain))
    if node_id:
        query += " AND EXISTS (SELECT 1 FROM json_each(node_ids) WHERE value=?)"
        params.append(node_id)
    return [_row(row) for row in db.execute(query + " ORDER BY rule_id", params)]


def get_rule(db: sqlite3.Connection, rule_id: str) -> dict[str, Any]:
    rule = _row(db.execute("SELECT * FROM kn.biz_rules WHERE rule_id=?", (rule_id,)).fetchone())
    if rule is None:
        raise KnowledgeError("not_found", "Rule not found", 404)
    return rule


def create_rule(db: sqlite3.Connection, body: dict[str, Any], who: str, *, proposed: bool = False) -> dict[str, Any]:
    """A person may create an approved rule directly; an AI proposal is always a draft."""
    title = _text(body.get("title"), "title", required=True, limit=400)
    status = "draft" if proposed else body.get("status", "draft")
    if status not in {"draft", "approved"}:
        raise KnowledgeError("invalid_knowledge", "a new rule is draft or approved")
    source = "ai" if proposed else body.get("source", "sme")
    if source not in RULE_SOURCES:
        raise KnowledgeError("invalid_knowledge", f"source must be one of {', '.join(RULE_SOURCES)}")
    nodes = _nodes(db, _list(body.get("nodes") or body.get("node_ids"), "nodes"))
    domain = _domain(body.get("domain"))
    rule_id = _next_id(db, "biz_rules", "rule_id", f"RULE-{domain}-", 3)
    approved = status == "approved"
    db.execute(
        """INSERT INTO kn.biz_rules(rule_id,domain,title,text,status,source,evidence,node_ids,requirement_refs,test_refs,
           fingerprint,proposed_by,approved_by,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rule_id, domain, title, _text(body.get("text"), "text"), status, source,
            _text(body.get("evidence"), "evidence", limit=400), json.dumps(nodes),
            json.dumps(_list(body.get("requirement_refs"), "requirement_refs")),
            json.dumps(_list(body.get("test_refs"), "test_refs")),
            fingerprint_of(db, nodes) if approved else "", who, who if approved else "", now() if approved else None,
        ),
    )
    db.commit()
    return get_rule(db, rule_id)


def update_rule(db: sqlite3.Connection, rule_id: str, body: dict[str, Any], who: str) -> dict[str, Any]:
    rule = get_rule(db, rule_id)
    allowed = {"title", "text", "evidence", "nodes", "requirement_refs", "test_refs", "status", "domain"}
    if not body or set(body) - allowed:
        raise KnowledgeError("invalid_knowledge", f"Only {', '.join(sorted(allowed))} can change")
    fields: dict[str, Any] = {}
    if "title" in body:
        fields["title"] = _text(body["title"], "title", required=True, limit=400)
    if "text" in body:
        fields["text"] = _text(body["text"], "text")
    if "evidence" in body:
        fields["evidence"] = _text(body["evidence"], "evidence", limit=400)
    if "nodes" in body:
        fields["node_ids"] = json.dumps(_nodes(db, _list(body["nodes"], "nodes")))
    for key in ("requirement_refs", "test_refs"):
        if key in body:
            fields[key] = json.dumps(_list(body[key], key))
    requested = body.get("status")
    if requested is not None and requested not in RULE_STATUSES:
        raise KnowledgeError("invalid_knowledge", f"status must be one of {', '.join(RULE_STATUSES)}")
    content_changed = bool(set(fields) & {"title", "text", "node_ids", "evidence"})
    if requested == "approved":
        nodes = json.loads(fields.get("node_ids", json.dumps(rule["node_ids"])))
        fields.update({"status": "approved", "approved_by": who, "approved_at": now(),
                       "fingerprint": fingerprint_of(db, nodes), "review_reason": ""})
    elif requested == "needs_review":
        raise KnowledgeError("invalid_knowledge", "needs_review is set by revalidation, not by hand")
    elif requested is not None:
        fields["status"] = requested
    elif rule["status"] in {"approved", "needs_review"} and content_changed:
        # Changing an approved rule's meaning needs a new approval.
        fields["status"] = "draft"
    fields["updated_at"] = now()
    assignments = ",".join(f"{key}=?" for key in fields)
    db.execute(f"UPDATE kn.biz_rules SET {assignments} WHERE rule_id=?", [*fields.values(), rule_id])
    db.commit()
    return get_rule(db, rule_id)


# --------------------------------------------------------------------------- decisions

def list_decisions(db: sqlite3.Connection, *, status: str | None = None, node_id: str | None = None) -> list[dict[str, Any]]:
    query, params = "SELECT * FROM kn.biz_decisions WHERE 1=1", []
    if status:
        query += " AND status=?"
        params.append(status)
    if node_id:
        query += " AND EXISTS (SELECT 1 FROM json_each(node_ids) WHERE value=?)"
        params.append(node_id)
    return [_row(row) for row in db.execute(query + " ORDER BY decision_id", params)]


def get_decision(db: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
    decision = _row(db.execute("SELECT * FROM kn.biz_decisions WHERE decision_id=?", (decision_id,)).fetchone())
    if decision is None:
        raise KnowledgeError("not_found", "Decision not found", 404)
    return decision


def _options(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise KnowledgeError("invalid_knowledge", "options must be a list of up to 10 items")
    options = []
    for item in value:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            raise KnowledgeError("invalid_knowledge", "each option is text or {title, consequence}")
        options.append({
            "title": _text(item.get("title"), "option title", required=True, limit=400),
            "consequence": _text(item.get("consequence"), "option consequence", limit=2000),
        })
    return options


def create_decision(db: sqlite3.Connection, body: dict[str, Any], who: str, *, proposed: bool = False) -> dict[str, Any]:
    status = "proposed" if proposed else body.get("status", "proposed")
    if status not in {"proposed", "accepted"}:
        raise KnowledgeError("invalid_knowledge", "a new decision is proposed or accepted")
    if status == "accepted" and not _text(body.get("decision"), "decision"):
        raise KnowledgeError("invalid_knowledge", "an accepted decision needs the decision text")
    decision_id = _next_id(db, "biz_decisions", "decision_id", "DEC-", 4)
    db.execute(
        """INSERT INTO kn.biz_decisions(decision_id,title,context,options_json,decision,reason,status,node_ids,rule_ids,
           proposed_by,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, _text(body.get("title"), "title", required=True, limit=400), _text(body.get("context"), "context"),
            json.dumps(_options(body.get("options")), ensure_ascii=False), _text(body.get("decision"), "decision"),
            _text(body.get("reason"), "reason"), status, json.dumps(_nodes(db, _list(body.get("nodes"), "nodes"))),
            json.dumps(_list(body.get("rule_ids"), "rule_ids")), who if status == "proposed" else "",
            who if status == "accepted" else "", now() if status == "accepted" else None,
        ),
    )
    db.commit()
    return get_decision(db, decision_id)


def update_decision(db: sqlite3.Connection, decision_id: str, body: dict[str, Any], who: str) -> dict[str, Any]:
    current = get_decision(db, decision_id)
    allowed = {"title", "context", "options", "decision", "reason", "status", "nodes", "rule_ids", "superseded_by"}
    if not body or set(body) - allowed:
        raise KnowledgeError("invalid_knowledge", f"Only {', '.join(sorted(allowed))} can change")
    fields: dict[str, Any] = {}
    for key in ("title", "context", "decision", "reason"):
        if key in body:
            fields[key] = _text(body[key], key, required=key == "title", limit=400 if key == "title" else MAX_TEXT)
    if "options" in body:
        fields["options_json"] = json.dumps(_options(body["options"]), ensure_ascii=False)
    if "nodes" in body:
        fields["node_ids"] = json.dumps(_nodes(db, _list(body["nodes"], "nodes")))
    if "rule_ids" in body:
        fields["rule_ids"] = json.dumps(_list(body["rule_ids"], "rule_ids"))
    if "superseded_by" in body:
        fields["superseded_by"] = _text(body["superseded_by"], "superseded_by", limit=40)
    status = body.get("status", current["status"])
    if status not in DECISION_STATUSES:
        raise KnowledgeError("invalid_knowledge", f"status must be one of {', '.join(DECISION_STATUSES)}")
    if status == "accepted" and not fields.get("decision", current["decision"]):
        raise KnowledgeError("invalid_knowledge", "an accepted decision needs the decision text")
    if status != current["status"]:
        fields["status"] = status
        if status in {"accepted", "rejected"}:
            fields["decided_by"], fields["decided_at"] = who, now()
    fields["updated_at"] = now()
    assignments = ",".join(f"{key}=?" for key in fields)
    db.execute(f"UPDATE kn.biz_decisions SET {assignments} WHERE decision_id=?", [*fields.values(), decision_id])
    db.commit()
    return get_decision(db, decision_id)


# --------------------------------------------------------------------------- questions

def list_questions(db: sqlite3.Connection, *, status: str | None = None, node_id: str | None = None) -> list[dict[str, Any]]:
    query, params = "SELECT * FROM kn.biz_questions WHERE 1=1", []
    if status:
        query += " AND status=?"
        params.append(status)
    if node_id:
        query += " AND EXISTS (SELECT 1 FROM json_each(node_ids) WHERE value=?)"
        params.append(node_id)
    return [_row(row) for row in db.execute(query + " ORDER BY question_id", params)]


def get_question(db: sqlite3.Connection, question_id: str) -> dict[str, Any]:
    question = _row(db.execute("SELECT * FROM kn.biz_questions WHERE question_id=?", (question_id,)).fetchone())
    if question is None:
        raise KnowledgeError("not_found", "Question not found", 404)
    return question


def create_question(db: sqlite3.Connection, body: dict[str, Any], who: str) -> dict[str, Any]:
    question_id = _next_id(db, "biz_questions", "question_id", "Q-", 4)
    db.execute(
        """INSERT INTO kn.biz_questions(question_id,text,context,options_json,status,node_ids,rule_ids,flow_id,asked_by)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            question_id, _text(body.get("text"), "text", required=True, limit=2000), _text(body.get("context"), "context"),
            json.dumps(_options(body.get("options")), ensure_ascii=False), "open",
            json.dumps(_nodes(db, _list(body.get("nodes"), "nodes"))), json.dumps(_list(body.get("rule_ids"), "rule_ids")),
            _text(body.get("flow_id"), "flow_id", limit=400), who,
        ),
    )
    db.commit()
    return get_question(db, question_id)


def answer_question(db: sqlite3.Connection, question_id: str, body: dict[str, Any], who: str) -> dict[str, Any]:
    get_question(db, question_id)
    if body.get("status") == "dropped":
        db.execute("UPDATE kn.biz_questions SET status='dropped',answered_by=?,answered_at=? WHERE question_id=?",
                   (who, now(), question_id))
    else:
        answer = _text(body.get("answer"), "answer", required=True)
        db.execute("UPDATE kn.biz_questions SET status='answered',answer=?,answered_by=?,answered_at=? WHERE question_id=?",
                   (answer, who, now(), question_id))
    db.commit()
    return get_question(db, question_id)


# --------------------------------------------------------------------------- revalidation

def revalidate(db: sqlite3.Connection, graph_stamp: str, *, force: bool = False) -> dict[str, Any]:
    """Send approved rules whose nodes changed behaviour (or disappeared) back to review.

    Runs once per extract (``graph_stamp`` identifies it); cheap to call on every read.
    """
    if not has_table(db, "graph_fingerprints"):
        return {"checked": 0, "changed": [], "skipped": "extract has no fingerprints; re-run the extract"}
    last = db.execute("SELECT value FROM kn.knowledge_meta WHERE key='revalidated_for'").fetchone()
    if last and last[0] == graph_stamp and not force:
        return {"checked": 0, "changed": [], "skipped": "already checked for this extract"}
    changed = []
    rules = db.execute("SELECT rule_id, node_ids, fingerprint FROM kn.biz_rules WHERE status='approved'").fetchall()
    for rule_id, node_json, stored in rules:
        nodes = json.loads(node_json or "[]")
        if not nodes or not stored:
            continue
        current = fingerprints(db, nodes)
        if combined(current, nodes) == stored:
            continue
        missing = [node for node in nodes if node not in current]
        names = node_names(db, nodes)
        reason = (
            f"Removed from the graph: {', '.join(missing)}" if missing
            else "Code changed since approval in: " + ", ".join(names.get(node, node) for node in nodes)
        )
        db.execute(
            "UPDATE kn.biz_rules SET status='needs_review', review_reason=?, updated_at=? WHERE rule_id=?",
            (f"{reason} (extract {graph_stamp})", now(), rule_id),
        )
        changed.append(rule_id)
    db.execute("INSERT OR REPLACE INTO kn.knowledge_meta(key,value) VALUES('revalidated_for',?)", (graph_stamp,))
    db.commit()
    return {"checked": len(rules), "changed": changed}


def part_changed(db: sqlite3.Connection, part: dict[str, Any], nodes: list[str]) -> bool:
    """An approved flow-document part whose nodes changed since it was approved."""
    stored = part.get("fingerprint")
    if part.get("status") != "approved" or not stored or not nodes:
        return False
    current = fingerprint_of(db, nodes)
    return bool(current) and current != stored


def graph_stamp(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value FROM meta WHERE key='generated_at'").fetchone() if has_table(db, "meta") else None
    return str(row[0]) if row else ""


# --------------------------------------------------------------------------- review queue

def review_queue(db: sqlite3.Connection) -> dict[str, Any]:
    """Everything waiting for a person: AI drafts, rules whose code changed, open questions and decisions."""
    revalidate(db, graph_stamp(db))
    flows_ai, flows_changed = [], []
    if has_table(db, "biz_flows", "kn"):
        for flow_id, feature_id, doc_json in db.execute("SELECT flow_id, feature_id, doc_json FROM kn.biz_flows"):
            try:
                doc = json.loads(doc_json or "{}")
            except ValueError:
                continue
            all_nodes = [node for step in doc.get("steps", []) for node in step.get("nodes", [])]
            parts = [("summary", doc.get("summary") or {}, all_nodes), ("edge_cases", doc.get("edge_cases") or {}, all_nodes)]
            parts += [(f"step:{step.get('id')}", step, step.get("nodes", [])) for step in doc.get("steps", [])]
            for part_id, part, nodes in parts:
                has_content = part.get("text") or part.get("items") or part.get("name")
                entry = {"flow_id": flow_id, "feature_id": feature_id, "flow_name": doc.get("name", ""), "part": part_id,
                         "title": part.get("name") or part_id}
                if part.get("status", "ai") == "ai" and has_content:
                    flows_ai.append(entry)
                elif part_changed(db, part, nodes):
                    flows_changed.append(entry)
    return {
        "rules_draft": list_rules(db, status="draft"),
        "rules_needs_review": list_rules(db, status="needs_review"),
        "decisions_proposed": list_decisions(db, status="proposed"),
        "questions_open": list_questions(db, status="open"),
        "flow_parts_ai": flows_ai,
        "flow_parts_changed": flows_changed,
    }


def queue_counts(queue: dict[str, Any]) -> dict[str, int]:
    return {key: len(value) for key, value in queue.items()}


# --------------------------------------------------------------------------- owners and operational facts

def owners(db: sqlite3.Connection) -> dict[str, dict[str, str]]:
    if not has_table(db, "graph_owners"):
        return {}
    return {row["system"]: dict(row) for row in db.execute("SELECT * FROM graph_owners ORDER BY system")}


def ops_facts(db: sqlite3.Connection, node_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    if not has_table(db, "graph_ops_facts"):
        return []
    if node_ids is None:
        return [dict(row) for row in db.execute("SELECT * FROM graph_ops_facts ORDER BY target, kind")]
    values = list(dict.fromkeys(node_ids))
    if not values:
        return []
    marks = ",".join("?" * len(values))
    return [dict(row) for row in db.execute(f"SELECT * FROM graph_ops_facts WHERE node_id IN ({marks}) ORDER BY target", values)]


# --------------------------------------------------------------------------- usage of the MCP server

def mcp_usage(db: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    if not has_table(db, "mcp_calls", "kn"):
        return {"by_tool": [], "unanswered": []}
    since = f"-{int(days)} days"
    by_tool = [
        {"tool": tool, "calls": calls, "empty": empty, "users": users}
        for tool, calls, empty, users in db.execute(
            """SELECT tool, count(*), sum(empty), count(DISTINCT user_name) FROM kn.mcp_calls
               WHERE called_at >= datetime('now', ?) GROUP BY tool ORDER BY count(*) DESC""",
            (since,),
        )
    ]
    unanswered = [
        {"tool": tool, "arguments": arguments, "count": count, "last": last}
        for tool, arguments, count, last in db.execute(
            """SELECT tool, arguments_json, count(*), max(called_at) FROM kn.mcp_calls
               WHERE empty=1 AND called_at >= datetime('now', ?) GROUP BY tool, arguments_json
               ORDER BY count(*) DESC, max(called_at) DESC LIMIT 30""",
            (since,),
        )
    ]
    return {"by_tool": by_tool, "unanswered": unanswered}
