"""Code Map MCP server: the graph and the reviewed business knowledge as tools for AI agents.

Two transports share one implementation (``server.McpServer``):

* stdio: ``codemap mcp`` (or ``python -m codemap.mcp``)
  (a local agent starts it; see ``.vscode/mcp.json``);
* HTTP: ``POST <url_prefix>/mcp`` on the web backend, for a team server.
"""
