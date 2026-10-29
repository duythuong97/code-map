"""Flask web app for the Code Map graph.

The app reads the extract output directly: ``codemap-web.sqlite`` (and
``graph.sqlite`` for semantic trees) in the configured ``graph_output``
folder. Notes and business-term mappings people add are kept in
``knowledge_db`` so the next extract does not overwrite them.

Run:
    python -m web.backend.api.app
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, g, jsonify, request, send_from_directory  # noqa: E402

from web.backend.api import graph_routes  # noqa: E402
from web.backend.api import business_routes  # noqa: E402,F401  (registers /business routes on the graph blueprint)
from web.backend.codemap_mcp import http_transport  # noqa: E402
from web.backend.config import absolute_path, load_app_config  # noqa: E402
from web.backend.logging_config import configure_backend_logging  # noqa: E402
from web.runtime_env import project_path, required_env  # noqa: E402

APP_CONFIG_PATH = project_path("web", "configs", "code-map.config.json")
APP_CONFIG = load_app_config(APP_CONFIG_PATH)
GRAPH_OUTPUT = absolute_path(APP_CONFIG["graph_output"], "graph_output")
KNOWLEDGE_DB = absolute_path(APP_CONFIG["knowledge_db"], "knowledge_db")
WEB_DIST = project_path("web", "frontend", "dist")
URL_PREFIX = APP_CONFIG["url_prefix"]

app = Flask(__name__)
BACKEND_LOG_PATH = configure_backend_logging(APP_CONFIG)
logger = logging.getLogger("code_map.backend")
if BACKEND_LOG_PATH:
    logger.info("backend file logging configured path=%s", BACKEND_LOG_PATH)
app.config["MAX_CONTENT_LENGTH"] = int(required_env("CODE_MAP_MAX_CONTENT_LENGTH"))
app.config["CODE_MAP_GRAPH_OUTPUT"] = GRAPH_OUTPUT
app.config["CODE_MAP_KNOWLEDGE_DB"] = KNOWLEDGE_DB
app.register_blueprint(graph_routes.bp)
# MCP for AI agents (Copilot, Claude Code, ...): POST <url_prefix>/mcp. Paths are read per request.
http_transport.register(app, lambda: (GRAPH_OUTPUT, KNOWLEDGE_DB))

# Short /api/... paths kept for older clients; they serve the /api/graph views.
GRAPH_ALIAS_GET = {
    "/api/graph-contract": graph_routes.graph_contract,
    "/api/stats": graph_routes.stats,
    "/api/search": graph_routes.search,
    "/api/localization": graph_routes.localization,
    "/api/node-detail": graph_routes.detail,
    "/api/edge-detail": graph_routes.detail,
    "/api/edge-evidence": graph_routes.evidence,
    "/api/evidence": graph_routes.evidence,
    "/api/snippet": graph_routes.snippet,
    "/api/flow": graph_routes.flow,
    "/api/flow-all": graph_routes.flow,
    "/api/lineage": graph_routes.flow,
    "/api/knowledge": graph_routes.knowledge,
}
GRAPH_ALIAS_POST = {"/api/knowledge": graph_routes.knowledge}


@app.before_request
def start_request_log_context():
    g.code_map_request_started_at = time.perf_counter()


@app.after_request
def log_request(response):
    started_at = getattr(g, "code_map_request_started_at", None)
    elapsed_ms = (time.perf_counter() - started_at) * 1000 if started_at else 0.0
    logger.info(
        "request method=%s path=%s status=%s elapsed_ms=%.1f remote=%s",
        request.method,
        _request_log_path(),
        response.status_code,
        elapsed_ms,
        request.remote_addr or "-",
    )
    return response


@app.teardown_request
def log_request_exception(error):
    if error is None:
        return None
    logger.error(
        "request exception method=%s path=%s remote=%s",
        request.method,
        _request_log_path(),
        request.remote_addr or "-",
        exc_info=(type(error), error, error.__traceback__),
    )
    return None


def _request_log_path() -> str:
    path = request.full_path.rstrip("?")
    return path if len(path) <= 500 else f"{path[:497]}..."


@app.before_request
def graph_aliases():
    app.config["CODE_MAP_GRAPH_OUTPUT"] = GRAPH_OUTPUT
    app.config["CODE_MAP_KNOWLEDGE_DB"] = KNOWLEDGE_DB
    path = request.path.rstrip("/") or "/"
    if path.startswith("/api/graph"):
        return None
    if request.method == "GET" and path.startswith("/api/nodes/"):
        return graph_routes.node(path[len("/api/nodes/"):])
    view = GRAPH_ALIAS_POST.get(path) if request.method == "POST" else GRAPH_ALIAS_GET.get(path)
    return view() if view is not None else None


class PrefixMiddleware:
    def __init__(self, wrapped, prefix: str):
        self.wrapped = wrapped
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if not self.prefix:
            return self.wrapped(environ, start_response)
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(f"{self.prefix}/"):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wrapped(environ, start_response)


app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)


def api_error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status


@app.errorhandler(413)
def request_entity_too_large(_error):
    return api_error("body_too_large", "Request body exceeds configured limit", 413)


@app.errorhandler(404)
def route_not_found(_error):
    return api_error("not_found", "API route not found", 404)


@app.errorhandler(405)
def method_not_allowed(_error):
    return api_error("method_not_allowed", "HTTP method not allowed", 405)


@app.get("/")
def index():
    if (WEB_DIST / "index.html").exists():
        response = send_from_directory(WEB_DIST, "index.html")
    else:
        response = jsonify(
            {
                "error": {
                    "code": "frontend_not_built",
                    "message": "React frontend build not found. Run npm run build in web/frontend.",
                }
            }
        )
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/app-config.js")
def app_config_js():
    return (
        "window.CODE_MAP_CONFIG = " + json.dumps({"urlPrefix": URL_PREFIX}) + ";",
        200,
        {"Content-Type": "application/javascript"},
    )


@app.get("/assets/<path:path>")
def web_assets(path: str):
    return send_from_directory(WEB_DIST / "assets", path)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph-output", help="Extract output folder (contains codemap-web.sqlite)")
    parser.add_argument("--host", default=APP_CONFIG["host"])
    parser.add_argument("--port", type=int, default=int(APP_CONFIG["port"]))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.graph_output:
        GRAPH_OUTPUT = absolute_path(args.graph_output, "--graph-output")
    logger.info("starting backend dev server host=%s port=%s prefix=%s graph_output=%s", args.host, args.port, URL_PREFIX, GRAPH_OUTPUT)
    app.run(host=args.host, port=args.port, debug=False)
