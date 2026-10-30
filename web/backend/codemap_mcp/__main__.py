"""Moved to codemap/mcp; this path keeps existing agent configs working.

Point the agent at ``python -m codemap mcp`` instead; this file will be removed.
"""
import sys

from codemap.mcp.__main__ import main

print("codemap: web/backend/codemap_mcp is deprecated; use `python -m codemap mcp`", file=sys.stderr)
raise SystemExit(main())
