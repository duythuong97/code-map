from __future__ import annotations

import argparse
import os
from pathlib import Path
from extractors.oracle_plsql import OraclePlSqlExtractor
from extractors.oracle_plsql_antlr_calls import OraclePlSqlAntlrCallExtractor
from extractors.oracle_plsql_lineage import OraclePlSqlLineageExtractor
from extractors.scanner import run_incremental

# ponytail: static registry covers in-repo extractors; add entry points only when external plugins exist.


def build_extractors(config: dict | None = None):
    extractors = [OraclePlSqlExtractor()]
    if _antlr_plsql_enabled(config or {}):
        extractors.append(OraclePlSqlAntlrCallExtractor())
    extractors.append(OraclePlSqlLineageExtractor())
    return extractors


def _antlr_plsql_enabled(config: dict) -> bool:
    features = config.get("features") or {}
    return bool(features.get("antlr_plsql_calls") or config.get("antlr_plsql_calls"))


def run_config(
    config_path: Path,
    *,
    files_from: Path | None = None,
    rebuild: bool = False,
) -> dict:
    return run_incremental(config_path, manifest_path=files_from, rebuild=rebuild)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally extract source facts into SQLite."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"),
        help="Validated pipeline JSON config",
    )
    parser.add_argument(
        "--files-from",
        type=Path,
        help="Manifest containing A/M/D plus a repository-relative path",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Reparse selected files without deleting serving data first",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run_config(
        Path(args.config), files_from=args.files_from, rebuild=args.rebuild
    )
    print(
        "Run {run_id}: status={status} discovered={discovered_count} "
        "extracted={extracted_count} skipped={skipped_count} "
        "failed={failed_count} deleted={deleted_count}".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
