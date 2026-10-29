"""Knowledge base commands for scripts and CI (no web server needed).

    python -m web.backend.kb revalidate --graph-output output/demo --knowledge-db data/knowledge.sqlite
    python -m web.backend.kb queue      --graph-output output/demo --knowledge-db data/knowledge.sqlite
    python -m web.backend.kb usage      --knowledge-db data/knowledge.sqlite [--days 30]

``revalidate`` after an extract moves approved rules whose code changed to
needs_review (exit code 1 when any did); ``queue`` prints what waits for review.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "extractor"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from web.backend import knowledge  # noqa: E402
from web.backend.storage import open_knowledge, open_web  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["revalidate", "queue", "usage"])
    parser.add_argument("--graph-output", help="Extract output folder")
    parser.add_argument("--knowledge-db", default=str(ROOT / "data" / "knowledge.sqlite"))
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    knowledge_db = Path(args.knowledge_db)
    if args.command == "usage":
        with open_knowledge(knowledge_db) as db:
            print(json.dumps(knowledge.mcp_usage(db, args.days), ensure_ascii=False, indent=2))
        return 0
    if not args.graph_output:
        parser.error("--graph-output is required")
    with open_web(Path(args.graph_output), knowledge_db) as db:
        if args.command == "revalidate":
            result = knowledge.revalidate(db, knowledge.graph_stamp(db), force=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("changed") else 0
        queue = knowledge.review_queue(db)
        print(json.dumps({"counts": knowledge.queue_counts(queue), **queue}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
