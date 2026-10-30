"""Knowledge base commands for scripts and CI (no web server needed).

    codemap kb revalidate [--config CONFIG | --graph-output OUT] [--knowledge-db DB]
    codemap kb queue      [--config CONFIG | --graph-output OUT] [--knowledge-db DB]
    codemap kb usage      [--knowledge-db DB] [--days 30]

Paths default to the served config (output, web.knowledgeDb), like the web app.

``revalidate`` after an extract moves approved rules whose code changed to
needs_review (exit code 1 when any did); ``queue`` prints what waits for review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from codemap import knowledge
from codemap.settings import load_settings
from codemap.storage import open_knowledge, open_web


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codemap kb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["revalidate", "queue", "usage"])
    parser.add_argument("--config", help="Extractor config (default: $CODE_MAP_CONFIG, else the demo).")
    parser.add_argument("--graph-output", help="Extract output folder (default: the config's output).")
    parser.add_argument("--knowledge-db", help="Default: the config's web.knowledgeDb, else var/knowledge.sqlite.")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    knowledge_db = Path(args.knowledge_db) if args.knowledge_db else settings["knowledge_db"]
    graph_output = Path(args.graph_output) if args.graph_output else settings["graph_output"]
    if args.command == "usage":
        with open_knowledge(knowledge_db) as db:
            print(json.dumps(knowledge.mcp_usage(db, args.days), ensure_ascii=False, indent=2))
        return 0
    with open_web(graph_output, knowledge_db) as db:
        if args.command == "revalidate":
            result = knowledge.revalidate(db, knowledge.graph_stamp(db), force=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("changed") else 0
        queue = knowledge.review_queue(db)
        print(json.dumps({"counts": knowledge.queue_counts(queue), **queue}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
