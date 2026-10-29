"""Model Context Protocol (JSON-RPC 2.0) for the code map, shared by the stdio and HTTP transports.

Implements what agents use: ``initialize``, ``ping``, ``tools/list``,
``tools/call``, ``prompts/list`` and ``prompts/get``. Tools that write
(proposals into the review queue) are only listed when the server was started
with writes allowed; everything they write waits for a person to approve it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from web.backend.storage import GraphNotReady

from .prompts import PROMPTS, render_prompt
from .tools import TOOLS, Tool, ToolError
from .workspace import Workspace

SERVER_NAME = "codemap"
SERVER_VERSION = "0.2.0"
# Newest first; the client's version is echoed when supported.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Code Map knows the whole system across repositories: screens, APIs, .NET code, PL/SQL, SQL files, tables, "
    "batch jobs, the business dictionary and documentation reviewed by people. Use codemap_impact before changing "
    "a table, API, procedure or job; codemap_current_behavior before writing or reviewing a requirement; "
    "codemap_trace_flow to follow a business flow across systems. Cite facts by their ids ([N3], [E2], [D1]). "
    "Read the first line of every answer: if the graph is stale for the files involved, say so. Never present a "
    "business rule as confirmed unless it is approved; turn every gap into a question for a person."
)
logger = logging.getLogger("code_map.mcp")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


@dataclass
class CallContext:
    user: str = ""
    client: str = ""
    allow_write: bool = False


class McpServer:
    def __init__(self, workspace: Workspace, *, tools: list[Tool] | None = None) -> None:
        self.workspace = workspace
        self.tools = {tool.name: tool for tool in (tools if tools is not None else TOOLS)}

    def handle(self, message: Any, context: CallContext | None = None) -> Any:
        """One JSON-RPC message or a batch; None when nothing is to be sent back (notifications)."""
        context = context or CallContext()
        if isinstance(message, list):
            replies = [reply for reply in (self._one(item, context) for item in message) if reply is not None]
            return replies or None
        return self._one(message, context)

    def handle_text(self, text: str, context: CallContext | None = None) -> str | None:
        try:
            message = json.loads(text)
        except ValueError:
            return json.dumps(_error(None, PARSE_ERROR, "Parse error"))
        reply = self.handle(message, context)
        return None if reply is None else json.dumps(reply, ensure_ascii=False)

    def _one(self, message: Any, context: CallContext) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # a response to a server request; this server sends none
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")
        method, request_id = message["method"], message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message
        try:
            result = self._dispatch(method, params, context)
        except _RpcError as exc:
            return None if is_notification else _error(request_id, exc.code, exc.message)
        except Exception:  # noqa: BLE001  (never crash the transport on one bad call)
            logger.exception("mcp method failed method=%s", method)
            return None if is_notification else _error(request_id, INTERNAL_ERROR, "Internal error")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any], context: CallContext) -> Any:
        if not isinstance(params, dict):
            raise _RpcError(INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            info = params.get("clientInfo") or {}
            if isinstance(info, dict) and info.get("name"):
                context.client = str(info.get("name"))[:100]
            return {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Code Map", "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method in {"ping"}:
            return {}
        if method.startswith("notifications/"):
            return {}
        if method == "tools/list":
            return {"tools": [tool.describe() for tool in self._visible(context)]}
        if method == "tools/call":
            return self._call(params, context)
        if method == "prompts/list":
            return {"prompts": [prompt.describe() for prompt in PROMPTS]}
        if method == "prompts/get":
            name = params.get("name")
            prompt = next((item for item in PROMPTS if item.name == name), None)
            if prompt is None:
                raise _RpcError(INVALID_PARAMS, f"Unknown prompt: {name}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(INVALID_PARAMS, "arguments must be an object")
            try:
                return render_prompt(prompt, {str(key): str(value) for key, value in arguments.items()})
            except ValueError as exc:
                raise _RpcError(INVALID_PARAMS, str(exc)) from exc
        if method in {"resources/list", "resources/templates/list"}:
            return {"resources": []} if method == "resources/list" else {"resourceTemplates": []}
        raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _visible(self, context: CallContext) -> list[Tool]:
        return [tool for tool in self.tools.values() if context.allow_write or not tool.write]

    def _call(self, params: dict[str, Any], context: CallContext) -> dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None or (tool.write and not context.allow_write):
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        started = time.perf_counter()
        error = ""
        try:
            if tool.write:
                arguments = {**arguments, "_user": context.user or context.client or "agent"}
            text = tool.handler(self.workspace, arguments)
            is_error = False
        except ToolError as exc:
            text, is_error, error = f"Invalid arguments: {exc}", True, str(exc)
        except GraphNotReady as exc:
            text, is_error, error = (
                f"The code map has no extract yet ({exc}). Run the extract and point the server at its output folder.",
                True,
                str(exc),
            )
        elapsed = (time.perf_counter() - started) * 1000
        self._log(tool.name, arguments, text, error, elapsed, context)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    def _log(self, tool: str, arguments: dict[str, Any], text: str, error: str, elapsed: float, context: CallContext) -> None:
        empty = int("Nothing in the graph matched" in text or "No graph node matches" in text or "declares nothing" in text)
        logger.info("mcp tool=%s chars=%s empty=%s elapsed_ms=%.1f", tool, len(text), empty, elapsed)
        try:
            with self.workspace.knowledge_db() as db:
                if db is None:
                    return
                db.execute(
                    "INSERT INTO kn.mcp_calls(user_name,client,tool,arguments_json,result_chars,empty,error,elapsed_ms) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        context.user,
                        context.client,
                        tool,
                        json.dumps({k: v for k, v in arguments.items() if not k.startswith("_")}, ensure_ascii=False)[:4000],
                        len(text),
                        empty,
                        error[:500],
                        round(elapsed, 1),
                    ),
                )
                db.commit()
        except Exception:  # noqa: BLE001  (logging must never fail a call)
            logger.warning("could not log mcp call", exc_info=True)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
