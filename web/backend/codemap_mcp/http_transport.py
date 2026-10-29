"""Streamable HTTP transport (JSON responses, no server-sent stream) on the Flask app: ``POST <prefix>/mcp``.

* The caller's name comes from ``X-Code-Map-User`` (VS Code ``mcp.json`` can send it in ``headers``).
* The propose_* tools are offered only with ``X-Code-Map-Token`` (or ``Authorization: Bearer``) equal to
  ``CODE_MAP_KNOWLEDGE_TOKEN``; read tools need no token.
* A browser ``Origin`` must be this host, localhost or listed in ``CODE_MAP_MCP_ALLOWED_ORIGINS``
  (DNS-rebinding protection the MCP spec asks for).
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from flask import Flask, Response, request

from .server import CallContext, McpServer, PROTOCOL_VERSIONS
from .workspace import workspace_for

MAX_BODY_BYTES = 1024 * 1024


def register(app: Flask, paths: Callable[[], tuple[Path, Path]]) -> None:
    """``paths()`` returns (graph output, knowledge db) for the current request."""

    @app.route("/mcp", methods=["POST", "GET", "DELETE"])
    def mcp_endpoint():
        if request.method != "POST":
            # No server-initiated stream and no sessions to end.
            return Response(status=405, headers={"Allow": "POST"})
        if not _origin_allowed():
            return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Origin not allowed"}}, 403)
        if request.content_length is None or request.content_length > MAX_BODY_BYTES:
            return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Bounded JSON body required"}}, 413)
        output, knowledge = paths()
        workspace = workspace_for(Path(output), Path(knowledge))
        context = CallContext(
            user=(request.headers.get("X-Code-Map-User") or "").strip()[:100],
            allow_write=_write_allowed(),
        )
        reply = McpServer(workspace).handle_text(request.get_data(as_text=True), context)
        if reply is None:
            return Response(status=202)
        version = request.headers.get("MCP-Protocol-Version", "")
        response = Response(reply, status=200, mimetype="application/json")
        if version in PROTOCOL_VERSIONS:
            response.headers["MCP-Protocol-Version"] = version
        return response


def _write_allowed() -> bool:
    token = os.environ.get("CODE_MAP_KNOWLEDGE_TOKEN", "")
    supplied = request.headers.get("X-Code-Map-Token", "")
    authorization = request.headers.get("Authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(token) and bool(supplied) and hmac.compare_digest(supplied, token)


def _origin_allowed() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # not a browser
    host = (urlparse(origin).hostname or "").lower()
    allowed = {"localhost", "127.0.0.1", "::1", (request.host or "").split(":")[0].lower()}
    allowed.update(
        (urlparse(item.strip()).hostname or item.strip()).lower()
        for item in os.environ.get("CODE_MAP_MCP_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )
    return host in allowed


def _json(value: object, status: int) -> Response:
    return Response(json.dumps(value), status=status, mimetype="application/json")
