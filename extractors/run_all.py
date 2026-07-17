from __future__ import annotations

import argparse
import os
from pathlib import Path

from common.config import ROOT, load_extractor_config, resolve_path
from extractors.import_csv import import_csv
from extractors.run_extract import run_config


def run_pipeline(config_path: Path, table_definitions: Path) -> tuple[int, int, int, int]:
    """Import DB table metadata, then extract code graph into the same SQLite DB."""
    tables, columns = import_csv(table_definitions, config_path)
    result = run_config(config_path)
    return tables, columns, len(result.nodes), len(result.edges)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import table metadata and extract source code graph in one pipeline."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"),
        help="Extractor config containing metadata import and source extraction settings.",
    )
    parser.add_argument("--tables", help="Override CSV file or directory containing table/column metadata.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = resolve_path(args.config, ROOT)
    config = load_extractor_config(config_path)
    table_definitions = args.tables or config.get("table_definitions_path")
    if not table_definitions:
        raise SystemExit("Missing table_definitions_path in extractor config or --tables")
    tables, columns, nodes, edges = run_pipeline(config_path, resolve_path(table_definitions, config_path.parent))
    print(f"Imported metadata: tables={tables} columns={columns}")
    print(f"Extracted graph  : nodes={nodes} edges={edges}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
