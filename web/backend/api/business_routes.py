"""HTTP routes for the business layer (menus, features, flow documents, AI export/import).

Registered on the graph blueprint, so they live under ``/api/graph/business``.
Writes follow the business-semantic-mapping route: bounded JSON bodies, no
cookies; the reviewer name comes from the optional ``X-Code-Map-User`` header.
"""
from __future__ import annotations

from urllib.parse import unquote

from flask import jsonify, request

from web.backend.api import business
from web.backend.api.graph_routes import WEB_DATABASE_NAME, bp, connection, error, graph_output, knowledge_db

MAX_EDIT_BYTES = 256 * 1024
MAX_IMPORT_BYTES = 4 * 1024 * 1024


@bp.errorhandler(business.BusinessError)
def business_error(exc: business.BusinessError):
    return error(exc.code, exc.message, exc.status)


_TREE_CACHE: dict[str, tuple[tuple, dict]] = {}


def _stamp(path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return 0, 0


def _context(db):
    """Graph index and tree, rebuilt only when the extract or the knowledge database changes."""
    web_path = graph_output() / WEB_DATABASE_NAME
    index = business.graph_index(db, web_path)
    key = (_stamp(web_path), _stamp(knowledge_db()))
    cached = _TREE_CACHE.get(str(web_path))
    if cached and cached[0] == key:
        return index, cached[1], web_path
    return index, _rebuild(db, index, key), web_path


def _rebuild(db, index, key=None):
    """Build the tree and keep it for the next request, unless a file changed while it was being built."""
    web_path = graph_output() / WEB_DATABASE_NAME
    key = key or (_stamp(web_path), _stamp(knowledge_db()))
    tree = business.build_tree(db, index)
    if key == (_stamp(web_path), _stamp(knowledge_db())):
        _TREE_CACHE[str(web_path)] = (key, tree)
    return tree


def _body(limit: int) -> dict:
    if not request.is_json or request.content_length is None or request.content_length > limit:
        raise business.BusinessError("invalid_business", "Bounded JSON body required", 415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise business.BusinessError("invalid_business", "JSON object required")
    return body


def _who() -> str:
    """The reviewer's name; percent-encoded by the UI because HTTP headers cannot carry "Nguyễn"."""
    return unquote(request.headers.get("X-Code-Map-User") or "").strip()[:100]


def _flag(name: str) -> bool:
    return request.args.get(name, "1").lower() not in {"0", "false", "no"}


@bp.get("/business/tree")
def business_tree():
    with connection() as db:
        _index, tree, _path = _context(db)
        return jsonify(tree)


@bp.get("/business/items/<path:item_id>")
def business_item(item_id):
    with connection() as db:
        index, tree, _path = _context(db)
        return jsonify(business.feature_detail(db, index, tree, item_id))


@bp.patch("/business/items/<path:item_id>")
def business_item_update(item_id):
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        index, tree, _path = _context(db)
        business.update_item(db, tree, item_id, body, _who(), index)
        tree = _rebuild(db, index)
        item = business.find_item(tree, item_id) or business.find_item(tree, body.get("parent_id", ""))
        return jsonify({"tree": tree, "item": business.feature_detail(db, index, tree, item["id"]) if item and item["kind"] != "entry" else None})


@bp.get("/business/flows/<path:flow_id>")
def business_flow(flow_id):
    with connection() as db:
        index, tree, _path = _context(db)
        return jsonify(business.load_flow(db, index, tree, flow_id))


@bp.get("/business/nodes/<path:node_id>")
def business_node(node_id):
    with connection() as db:
        index, tree, _path = _context(db)
        return jsonify(business.node_page(db, index, tree, node_id))


@bp.patch("/business/flows/<path:flow_id>/parts/<part>")
def business_flow_part(flow_id, part):
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        index, tree, _path = _context(db)
        business.update_part(db, index, tree, flow_id, part, body, _who())
        tree = _rebuild(db, index)
        return jsonify({**business.load_flow(db, index, tree, flow_id), "tree": tree})


@bp.post("/business/flows/<path:flow_id>/questions/<question_id>/answer")
def business_flow_answer(flow_id, question_id):
    body = _body(MAX_EDIT_BYTES)
    with connection() as db:
        index, tree, _path = _context(db)
        business.answer_question(db, index, tree, flow_id, question_id, body.get("answer"), _who())
        tree = _rebuild(db, index)
        return jsonify({**business.load_flow(db, index, tree, flow_id), "tree": tree})


@bp.post("/business/features/<path:feature_id>/ai-export")
def business_ai_export(feature_id):
    with connection() as db:
        index, tree, web_path = _context(db)
        return jsonify(business.export_pack(
            db, index, tree, web_path, feature_id,
            include_code=_flag("code"), include_locked=_flag("locked"), include_answers=_flag("answers"),
            include_instructions=_flag("instructions"),
        ))


@bp.post("/business/features/<path:feature_id>/ai-import/check")
def business_ai_import_check(feature_id):
    body = _body(MAX_IMPORT_BYTES)
    with connection() as db:
        index, tree, web_path = _context(db)
        return jsonify(business.check_import(db, index, tree, web_path, feature_id, body.get("text")))


@bp.post("/business/features/<path:feature_id>/ai-import")
def business_ai_import(feature_id):
    body = _body(MAX_IMPORT_BYTES)
    with connection() as db:
        index, tree, web_path = _context(db)
        result = business.apply_import(db, index, tree, web_path, feature_id, body.get("text"))
        return jsonify({**result, "tree": _rebuild(db, index)})


@bp.get("/business/search")
def business_search():
    query = request.args.get("q", "").strip()
    with connection() as db:
        index, tree, _path = _context(db)
        return jsonify({"business": business.search(db, tree, query), "nodes": business.search_nodes(db, index, tree, query)})
