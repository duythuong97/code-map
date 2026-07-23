"""CLI for validating and importing CSV graph packages."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from application.runtime_env import project_path, required_absolute_path
from application.backend.importer.package_validator import validate_package
from application.backend.importer.pipeline import (
    import_roots,
    initialize,
    validation_ids,
)


def package_roots(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.rglob("manifest.json"))


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("root", type=Path)
    publish = commands.add_parser("import")
    publish.add_argument("root", type=Path)
    publish.add_argument("--db", type=Path, default=project_path("data", "code-flow-demo.sqlite"))
    publish.add_argument("--input-root", type=Path, default=project_path("input-data"))
    integrity = commands.add_parser("integrity")
    integrity.add_argument("--db", type=Path, default=project_path("data", "code-flow-demo.sqlite"))
    args = parser.parse_args()
    if args.command == "validate":
        roots = package_roots(args.root)
        allowed = validation_ids(roots, project_path("input-data"))
        result = [
            validate_package(root, allowed, workspace_root=required_absolute_path("CODE_MAP_SOURCE_ROOT"))["manifest"]["packageId"] for root in roots
        ]
        print(json.dumps({"status": "valid", "packages": result}))
    elif args.command == "import":
        print(
            json.dumps(
                {
                    "status": "imported",
                    "counts": import_roots(package_roots(args.root), args.db, args.input_root, required_absolute_path("CODE_MAP_SOURCE_ROOT")),
                }
            )
        )
    else:
        with closing(sqlite3.connect(args.db)) as db:
            initialize(db)
            result = {
                "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
                "foreignKeys": db.execute("PRAGMA foreign_key_check").fetchall(),
            }
        if result != {"integrity": "ok", "foreignKeys": []}:
            raise RuntimeError(result)
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
