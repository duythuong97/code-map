from __future__ import annotations

import argparse
import os
from pathlib import Path

from extractors.oracle_plsql_lineage import OraclePlSqlLineageExtractor
from extractors.oracle_plsql import OraclePlSqlExtractor
from db.entities import ExtractionContext, ExtractionResult
from db.writer import open_db, write_result
from common.config import load_extractor_config
from common.source_text import read_source_text

SCAN_EXTENSIONS = {
    ".sql",
    ".ddl",
    ".fnc",
    ".pks",
    ".pkb",
    ".prc",
    ".trg",
    ".pls",
    ".plb",
    ".pck",
}

# ponytail: static registry until plugin loading is needed; add entry point discovery when external extractor packages exist.
EXTRACTORS = [OraclePlSqlExtractor(), OraclePlSqlLineageExtractor()]


def run(
    path: Path,
    db_path: Path,
    repo: str,
    schema: str,
    db_name: str,
    reset: bool = False,
) -> ExtractionResult:
    root = path.expanduser().resolve()
    context = ExtractionContext(
        repository=repo or root.name,
        repository_path=str(root),
        db_name=db_name,
        extra_tags={"schema": schema} if schema else {},
    )
    result = extract_root(root, context)
    with open_db(db_path, reset=reset) as db:
        write_result(db, result)
        db.commit()
    return result

def run_config(config_path: Path) -> ExtractionResult:
    config_path = config_path.expanduser().resolve()
    config = load_extractor_config(config_path)
    db_path = resolve_config_path(config["db"], config_path.parent)
    combined = ExtractionResult()
    with open_db(db_path, reset=bool(config.get("reset", False))) as db:
        for source in config["sources"]:
            if not source.get("schema"):
                raise ValueError("Each source in config must define schema")
            root = resolve_config_path(source["path"], config_path.parent)
            context = ExtractionContext(
                repository=source["repo"],
                repository_path=str(root),
                db_name=source.get("db_name", config["db_name"]),
                extra_tags={"schema": source["schema"]},
            )
            result = extract_root(root, context)
            write_result(db, result)
            combined = combined.merge(result)
        db.commit()
    return combined

def resolve_config_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def extract_root(root: Path, context: ExtractionContext) -> ExtractionResult:
    combined = ExtractionResult()
    for file_path in scan_files(root):
        text = read_source_text(file_path)
        for extractor in (
            item for item in EXTRACTORS if item.can_handle(str(file_path), text)
        ):
            result = extractor.extract(str(file_path), text, context)
            inject_metadata(result, file_path, type(extractor).__name__)
            combined = combined.merge(result)
    return combined


def scan_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SCAN_EXTENSIONS:
            yield path


def inject_metadata(result: ExtractionResult, path: Path, extractor: str) -> None:
    for node in result.nodes:
        node.properties.setdefault("source_file", str(path))
        node.properties.setdefault("extractor_name", extractor)
    for edge in result.edges:
        edge.properties.setdefault("source_file", str(path))
        edge.properties.setdefault("extractor_name", extractor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract code graph into SQLite for API/webapp."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CODE_MAP_CONFIG"),
        help="JSON config containing sources to extract",
    )
    parser.add_argument(
        "--path", help="Folder containing source files to extract"
    )
    parser.add_argument("--db", help="SQLite output path")
    parser.add_argument("--repo", help="Repository name shown in qnames")
    parser.add_argument(
        "--schema", help="Default DB schema for unqualified tables"
    )
    parser.add_argument(
        "--db-name", help="Database name used in table qnames"
    )
    parser.add_argument(
        "--reset", action="store_true", help="Clear old graph before insert"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.config and not args.path:
        args.config = "code-map.config.json"
    if args.config:
        result = run_config(Path(args.config))
        db_path = load_extractor_config(Path(args.config))["db"]
    else:
        missing = [name for name in ("path", "db", "repo", "schema", "db_name") if not getattr(args, name)]
        if missing:
            raise SystemExit(f"--config or these args are required: {', '.join('--' + name.replace('_', '-') for name in missing)}")
        result = run(
            Path(args.path), Path(args.db), args.repo, args.schema, args.db_name, args.reset
        )
        db_path = args.db
    print(f"SQLite: {db_path}")
    print(f"Total : nodes={len(result.nodes)} edges={len(result.edges)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
