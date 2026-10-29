"""HTTP routes for the knowledge layer: rules, decisions, questions, the review queue and KB status.

Registered on the graph blueprint under ``/api/graph/business``. Like the other
business routes, the person comes from ``X-Code-Map-User``; approving needs a
name so the record says who approved.
"""
from __future__ import annotations

from flask import jsonify, request

from web.backend import knowledge
from web.backend.api.business_routes import MAX_EDIT_BYTES, _body, _who
from web.backend.api.graph_routes import bp, connection, error, graph_output, knowledge_db
from web.backend.codemap_mcp.workspace import workspace_for


@bp.errorhandler(knowledge.KnowledgeError)
def knowledge_error(exc: knowledge.KnowledgeError):
    return error(exc.code, exc.message, exc.status)


def _approver(body: dict) -> str:
    who = _who()
    approving = body.get("status") in {"approved", "accepted", "rejected"}
    if approving and not who:
        raise knowledge.KnowledgeError("who_required", "Set X-Code-Map-User: an approval records who approved", 400)
    return who


def _revalidated(db) -> None:
    knowledge.revalidate(db, knowledge.graph_stamp(db))


@bp.get("/business/rules")
def rules_list():
    with connection() as db:
        _revalidated(db)
        return jsonify(knowledge.list_rules(
            db, status=request.args.get("status") or None, node_id=request.args.get("node") or None,
            domain=request.args.get("domain") or None,
        ))


@bp.post("/business/rules")
def rules_create():
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        return jsonify(knowledge.create_rule(db, body, _approver(body))), 201


@bp.get("/business/rules/<rule_id>")
def rules_get(rule_id):
    with connection() as db:
        _revalidated(db)
        return jsonify(knowledge.get_rule(db, rule_id))


@bp.patch("/business/rules/<rule_id>")
def rules_update(rule_id):
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        return jsonify(knowledge.update_rule(db, rule_id, body, _approver(body)))


@bp.get("/business/decisions")
def decisions_list():
    with connection() as db:
        return jsonify(knowledge.list_decisions(
            db, status=request.args.get("status") or None, node_id=request.args.get("node") or None,
        ))


@bp.post("/business/decisions")
def decisions_create():
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        return jsonify(knowledge.create_decision(db, body, _approver(body))), 201


@bp.get("/business/decisions/<decision_id>")
def decisions_get(decision_id):
    with connection() as db:
        return jsonify(knowledge.get_decision(db, decision_id))


@bp.patch("/business/decisions/<decision_id>")
def decisions_update(decision_id):
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        return jsonify(knowledge.update_decision(db, decision_id, body, _approver(body)))


@bp.get("/business/questions")
def questions_list():
    with connection() as db:
        return jsonify(knowledge.list_questions(
            db, status=request.args.get("status") or None, node_id=request.args.get("node") or None,
        ))


@bp.post("/business/questions")
def questions_create():
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        return jsonify(knowledge.create_question(db, body, _who())), 201


@bp.post("/business/questions/<question_id>/answer")
def questions_answer(question_id):
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        who = _who()
        if not who:
            raise knowledge.KnowledgeError("who_required", "Set X-Code-Map-User: an answer records who answered", 400)
        return jsonify(knowledge.answer_question(db, question_id, body, who))


@bp.get("/business/review-queue")
def review_queue():
    with connection() as db:
        queue = knowledge.review_queue(db)
        return jsonify({**queue, "counts": knowledge.queue_counts(queue)})


@bp.post("/business/revalidate")
def revalidate():
    with connection() as db:
        return jsonify(knowledge.revalidate(db, knowledge.graph_stamp(db), force=True))


@bp.get("/business/owners")
def owners():
    with connection() as db:
        return jsonify(list(knowledge.owners(db).values()))


@bp.get("/business/ops-facts")
def ops_facts():
    node = request.args.get("node")
    with connection() as db:
        return jsonify(knowledge.ops_facts(db, [node] if node else None))


@bp.get("/business/kb-status")
def kb_status():
    """Freshness of the graph, extraction quality, what waits for review and how agents use the KB."""
    workspace = workspace_for(graph_output(), knowledge_db())
    with connection() as db:
        queue = knowledge.review_queue(db)
        usage = knowledge.mcp_usage(db, int(request.args.get("days", "30") or 30))
    report = workspace.stale()
    return jsonify({
        "summary": workspace.status_line(),
        "stale": report,
        "quality": workspace.quality().get("metrics", {}),
        "review": knowledge.queue_counts(queue),
        "usage": usage,
    })
