"""stdio transport: one JSON-RPC message per line on stdin, replies on stdout, logs on stderr."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "extractor"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from web.backend.codemap_mcp.server import CallContext, McpServer  # noqa: E402
from web.backend.codemap_mcp.workspace import Workspace  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codemap-mcp", description=__doc__)
    parser.add_argument(
        "--graph-output",
        default=os.environ.get("CODE_MAP_GRAPH_OUTPUT"),
        help="Extract output folder (graph.sqlite, codemap-rag.sqlite, codemap-web.sqlite). Default: $CODE_MAP_GRAPH_OUTPUT",
    )
    parser.add_argument(
        "--knowledge-db",
        default=os.environ.get("CODE_MAP_KNOWLEDGE_DB"),
        help="knowledge.sqlite with reviewed business documentation (default: <repo>/data/knowledge.sqlite if it exists).",
    )
    parser.add_argument("--source-root", help="Source root for stale checks (default: the one recorded at extract time).")
    parser.add_argument("--user", default=os.environ.get("CODE_MAP_USER", ""), help="Name recorded on proposals and in the call log.")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Offer the propose_* tools (they only add drafts to the review queue).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Print what the server sees (codemap_status) and exit: checks the setup before adding it to an agent.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if not args.graph_output:
        parser.error("--graph-output (or CODE_MAP_GRAPH_OUTPUT) is required")
    knowledge = Path(args.knowledge_db) if args.knowledge_db else ROOT / "data" / "knowledge.sqlite"
    workspace = Workspace(
        output=Path(args.graph_output).expanduser().resolve(),
        knowledge=knowledge if (args.knowledge_db or knowledge.is_file() or args.allow_write) else None,
        source_root=Path(args.source_root).resolve() if args.source_root else None,
    )
    server = McpServer(workspace)
    context = CallContext(user=args.user, allow_write=args.allow_write)
    if args.self_test:
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "codemap_status", "arguments": {"detail": "brief"}}}, context)
        result = reply.get("result", {})
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, context)["result"]["tools"]
        sys.stdout.write(result.get("content", [{}])[0].get("text", str(reply)) + "\n\n")
        sys.stdout.write(f"{len(tools)} tools: {', '.join(tool['name'] for tool in tools)}\n")
        sys.stdout.write(f"knowledge db: {workspace.knowledge or 'not used'}\n")
        return 1 if result.get("isError") else 0
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        reply = server.handle_text(line, context)
        if reply is not None:
            stdout.write(reply.encode("utf-8") + b"\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
