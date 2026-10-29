"""Print the Python retriever's answers as JSON, for the TS/Python parity test in vscode-extension.

    python -m web.backend.codemap_mcp.parity --db codemap-rag.sqlite --cases '[["hủy đơn", "ask", null]]'
    python -m web.backend.codemap_mcp.parity --fixture <folder>   # build the shared fixture database there
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "extractor"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from web.backend.codemap_mcp.retriever import Retriever  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--cases", help="JSON list of [question, mode, file]")
    parser.add_argument("--fixture", help="Write the shared fixture graph and its codemap-rag.sqlite into this folder")
    args = parser.parse_args()
    if args.fixture:
        from code_tree_exporter.rag_export import write_rag_database
        from shared.fixtures.engine_graph import business_inputs, engine_graph

        folder = Path(args.fixture)
        folder.mkdir(parents=True, exist_ok=True)
        engine_graph(folder / "graph.sqlite")
        business_inputs(folder / "input-data")
        write_rag_database(folder / "graph.sqlite", folder / "codemap-rag.sqlite", input_root=folder / "input-data")
        print(folder / "codemap-rag.sqlite")
        return 0
    retriever = Retriever(sqlite3.connect(args.db))
    cases = json.loads(args.cases)
    sys.stdout.write(json.dumps([retriever.retrieve(q, mode=m, file=f).text for q, m, f in cases], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
