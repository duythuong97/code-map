from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .env_loader import REPOSITORY_ENV, load_dotenv
from .extractors import supported_source_types
from .pipeline import run_pipeline, validate_pipeline_config


def main(argv: list[str] | None = None) -> int:
    # A Windows console or pipe in cp932 cannot print every character
    # (Vietnamese names, some symbols); never crash on output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    arguments = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(
        prog="code-tree-exporter",
        description=(
            "Extract configured source units into a SQLite graph package."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    extract_parser = commands.add_parser(
        "extract", description="Run all configured extractors and publish V3 output."
    )
    extract_parser.add_argument(
        "--config", help="Path to the unified JSON config (default: $CODE_MAP_CONFIG)."
    )
    extract_parser.add_argument(
        "--output", help="Write here instead of the config's output (e.g. a base-commit graph)."
    )
    extract_parser.add_argument(
        "--at",
        action="append",
        metavar="REV|SOURCE=REV",
        help="Extract an older revision from a git worktree; repeat SOURCE=REV per source.",
    )
    extract_parser.add_argument(
        "--at-date", help="Extract each repository at its last commit before this date (YYYY-MM-DD)."
    )
    extract_parser.add_argument(
        "--list-source-types", action="store_true", help=argparse.SUPPRESS
    )
    diff_parser = commands.add_parser(
        "diff",
        description="What a change did to the system: compare two extracts (base and head), optionally "
        "against the approved impact.yaml, and name the rules, documents and owners it touches.",
    )
    diff_parser.add_argument("--base", required=True, help="Base extract output folder or graph.sqlite.")
    diff_parser.add_argument("--head", required=True, help="Head extract output folder or graph.sqlite.")
    diff_parser.add_argument("--plan", help="impact.yaml (or .json) of the change.")
    diff_parser.add_argument("--knowledge-db", help="knowledge.sqlite: business rules and flow documents to check.")
    diff_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    diff_parser.add_argument("--out", help="Write the report here instead of stdout.")
    diff_parser.add_argument(
        "--fail-on-unplanned", action="store_true", help="Exit 1 when a change is outside the plan."
    )
    oracle_parser = commands.add_parser(
        "verify-oracle",
        description="Precision/recall of PL/SQL dependencies against a CSV export of Oracle ALL_DEPENDENCIES.",
    )
    oracle_parser.add_argument("--graph", required=True)
    oracle_parser.add_argument("--dependencies", required=True, help="CSV with OWNER,NAME,TYPE,REFERENCED_OWNER,REFERENCED_NAME,REFERENCED_TYPE")
    oracle_parser.add_argument("--owner", action="append", help="Only these schemas (repeatable).")
    oracle_parser.add_argument("--format", choices=["text", "json"], default="text")
    sample_parser = commands.add_parser("sample-edges", description="A stratified sample of edges to review by hand.")
    sample_parser.add_argument("--graph", required=True)
    sample_parser.add_argument("--n", type=int, default=50)
    sample_parser.add_argument("--seed", type=int, default=1)
    sample_parser.add_argument("--out", required=True, help="CSV to fill in (verdict: correct / wrong / unsure).")
    score_sample_parser = commands.add_parser("score-sample", description="Precision per edge type from a reviewed sample.")
    score_sample_parser.add_argument("sample", help="The CSV written by sample-edges, with verdicts filled in.")
    stale_parser = commands.add_parser(
        "stale",
        description="Compare an extract with the working tree: changed/new files, new commits, affected nodes.",
    )
    stale_parser.add_argument("--graph", required=True, help="graph.sqlite or the extract output folder.")
    stale_parser.add_argument("--source-root", help="Override the source root recorded at extract time.")
    stale_parser.add_argument("--no-added", action="store_true", help="Skip the scan for new files.")
    init_parser = commands.add_parser(
        "init",
        description="Look into every repository under --root and write (or extend) a config with one "
        "source per Angular app, .NET API/batch, PL/SQL and SQL folder found.",
    )
    init_parser.add_argument("--root", help="Folder holding the repositories, e.g. C:/sources (new config only).")
    init_parser.add_argument("--config", help="Config to write or extend (default: $CODE_MAP_CONFIG).")
    init_parser.add_argument("--name", help="System name (default: the root folder's name).")
    init_parser.add_argument("--database", help="database for the SQL and .NET sources, e.g. DB1.")
    init_parser.add_argument("--schema", help="schema for the SQL and .NET sources, e.g. APP.")
    init_parser.add_argument("--dry-run", action="store_true", help="Print what would be added without writing.")
    validate_parser = commands.add_parser(
        "validate", description="Validate V3 config and catalog without extracting."
    )
    validate_parser.add_argument("--config", help="Default: $CODE_MAP_CONFIG.")

    rag_parser = commands.add_parser(
        "serving-export",
        aliases=["rag-export"],
        description="Write codemap.sqlite (read by the web app and the MCP server) from graph.sqlite.",
    )
    rag_parser.add_argument("--graph", required=True, help="graph.sqlite or the extract output folder.")
    rag_parser.add_argument("--input-data", help="Folder with semantic-dictionary.csv, tables.csv, jobnet.csv ...")
    rag_parser.add_argument("--source-root", help="Folder the evidence paths are relative to.")
    rag_parser.add_argument("--out", help="Default: codemap.sqlite next to graph.sqlite.")

    catalog_parser = commands.add_parser(
        "catalog", description="Inspect and validate incoming catalog files."
    )
    catalog_commands = catalog_parser.add_subparsers(
        dest="catalog_command", required=True
    )
    inspect_parser = catalog_commands.add_parser(
        "inspect", description="Show headers and a profile template for one CSV."
    )
    inspect_parser.add_argument("path", help="CSV file to inspect.")
    inspect_parser.add_argument("--encoding", default="auto")

    # Keep the V2 invocation (`code-tree-exporter --config ...`) working.
    if arguments and arguments[0].startswith("-") and arguments[0] not in {
        "-h",
        "--help",
    }:
        arguments.insert(0, "extract")
    args = parser.parse_args(arguments)
    if args.command in {"extract", "validate", "init"}:
        args.config = _config_path(args.config)
    try:
        if args.command == "catalog":
            from .v3.catalog import inspect_catalog_file

            result = inspect_catalog_file(
                Path(args.path), encoding=str(args.encoding)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"serving-export", "rag-export"}:
            from .serving import DATABASE_NAME, write_serving_database
            from .sqlite_graph import resolve_database_path

            graph = resolve_database_path(Path(args.graph))
            output = Path(args.out) if args.out else graph.with_name(DATABASE_NAME)
            counts = write_serving_database(
                graph,
                output,
                input_root=Path(args.input_data) if args.input_data else None,
                source_root=Path(args.source_root).resolve() if args.source_root else None,
            )
            print(json.dumps({"output": str(output), "counts": counts}, ensure_ascii=False))
            return 0
        if args.command == "diff":
            from .diff import affected_knowledge, compare_with_plan, graph_diff, load_plan, to_markdown
            from .sqlite_graph import resolve_database_path

            head_graph = resolve_database_path(Path(args.head))
            result = graph_diff(resolve_database_path(Path(args.base)), head_graph)
            plan = compare_with_plan(result, load_plan(Path(args.plan))) if args.plan else None
            knowledge = affected_knowledge(
                result,
                Path(args.knowledge_db) if args.knowledge_db else None,
                head_graph.with_name("codemap.sqlite"),
            )
            if args.format == "json":
                text = json.dumps({"diff": result, "plan": plan, "knowledge": knowledge}, ensure_ascii=False, indent=2)
            else:
                text = to_markdown(result, plan, knowledge)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            else:
                print(text)
            return 1 if args.fail_on_unplanned and plan and plan["unplanned"] else 0
        if args.command == "verify-oracle":
            from .sqlite_graph import resolve_database_path
            from .verify import format_oracle_report, verify_oracle

            report = verify_oracle(
                resolve_database_path(Path(args.graph)),
                Path(args.dependencies),
                owners={owner.upper() for owner in args.owner} if args.owner else None,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else format_oracle_report(report))
            return 0
        if args.command == "sample-edges":
            from .sqlite_graph import resolve_database_path
            from .verify import sample_edges, write_sample

            rows = sample_edges(resolve_database_path(Path(args.graph)), args.n, args.seed)
            write_sample(rows, Path(args.out))
            print(json.dumps({"out": args.out, "edges": len(rows)}))
            return 0
        if args.command == "score-sample":
            from .verify import score_sample

            print(json.dumps(score_sample(Path(args.sample)), ensure_ascii=False, indent=2))
            return 0
        if args.command == "stale":
            from .sqlite_graph import resolve_database_path
            from .stale import stale_report, summary_line

            report = stale_report(
                resolve_database_path(Path(args.graph)),
                Path(args.source_root) if args.source_root else None,
                check_added=not args.no_added,
            )
            report["summary"] = summary_line(report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("fresh") else 1
        if args.command == "init":
            from .init_config import format_report, init_config

            if not args.config:
                init_parser.error("--config is required (or set CODE_MAP_CONFIG in .env)")
            report = init_config(
                Path(args.root) if args.root else None,
                Path(args.config),
                name=args.name,
                database=args.database,
                schema=args.schema,
                dry_run=args.dry_run,
            )
            print(format_report(report))
            return 0
        if args.command == "validate":
            if not args.config:
                validate_parser.error("--config is required (or set CODE_MAP_CONFIG in .env)")
            result = validate_pipeline_config(Path(args.config))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        if args.list_source_types:
            print("\n".join(supported_source_types()))
            return 0
        if not args.config:
            extract_parser.error("--config is required (or set CODE_MAP_CONFIG in .env)")
        output_override = Path(args.output) if args.output else None
        if args.at or args.at_date:
            from .checkout import checkout_at, parse_at
            from .pipeline import configured_sources

            root, sources = configured_sources(Path(args.config))
            default, per_source = parse_at(args.at)
            with checkout_at(
                root, sources, default=default, per_source=per_source, before_date=args.at_date
            ) as checked_out:
                output = run_pipeline(
                    Path(args.config),
                    root_override=checked_out,
                    output_override=output_override,
                    display_root=root,
                )
        else:
            output = run_pipeline(Path(args.config), output_override=output_override)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0

def _config_path(value: str | None) -> str | None:
    """``--config`` as given, else $CODE_MAP_CONFIG (relative to the repository, like the web app)."""
    if value:
        return value
    load_dotenv(REPOSITORY_ENV)
    configured = os.environ.get("CODE_MAP_CONFIG")
    if not configured:
        return None
    path = Path(configured).expanduser()
    return str(path if path.is_absolute() else REPOSITORY_ENV.parent / path)


if __name__ == "__main__":
    raise SystemExit(main())
