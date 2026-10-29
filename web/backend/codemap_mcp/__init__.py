"""Code Map MCP server: the graph and the reviewed business knowledge as tools for AI agents.

Two transports share one implementation (``server.McpServer``):

* stdio: ``python -m web.backend.codemap_mcp --graph-output <extract output>``
  (a local agent starts it; see ``.vscode/mcp.json``);
* HTTP: ``POST <url_prefix>/mcp`` on the web backend, for a team server.
"""
