"""The read side of Code Map, shared by the web app and the MCP server (no Flask here).

Layers, each importing only the ones below it:

    web/backend (Flask routes)   codemap.mcp (MCP tools)
                 \\                 /
                  codemap (settings, storage, knowledge, business, retriever, workspace, cli)
                                    |
                  code_tree_exporter (extractor; its contract/ package is what the readers share)
"""
from __future__ import annotations

from pathlib import Path

# The checkout (pyproject.toml installs it editable): .env, examples/ and var/ live here.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
