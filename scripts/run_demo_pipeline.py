#!/usr/bin/env python3
"""Extract configured sources, validate packages, then import SQLite."""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from application.runtime_env import project_path

PYTHON = project_path(".venv", "bin", "python")
DB = project_path("data", "code-flow-demo.sqlite")
INPUT_ROOT = project_path("input-data")
OUTPUT_ROOT = project_path("output")
STEPS = [
    ("Angular", [PYTHON, project_path("extractors", "angular-extractor", "main.py"), "--config", project_path("configs", "angular-customer-web.json")]),
    (".NET API", [PYTHON, project_path("extractors", "dotnet-api-extractor", "main.py"), "--config", project_path("configs", "dotnet-api-order-api.json")]),
    (".NET batch", [PYTHON, project_path("extractors", "dotnet-batch-extractor", "main.py"), "--config", project_path("configs", "dotnet-batch-order-fulfillment.json")]),
    ("PL/SQL", [PYTHON, project_path("extractors", "plsql-extractor", "main.py"), "--config", project_path("configs", "plsql-order-db.json")]),
    ("SQL files", [PYTHON, project_path("extractors", "sql-file-extractor", "main.py"), "--config", project_path("configs", "sql-order-ops.json")]),
    ("Validate CSV", [PYTHON, "-m", "application.backend.cli", "validate", OUTPUT_ROOT]),
    ("Import SQLite", [PYTHON, "-m", "application.backend.cli", "import", OUTPUT_ROOT, "--db", DB, "--input-root", INPUT_ROOT]),
    ("SQLite integrity", [PYTHON, "-m", "application.backend.cli", "integrity", "--db", DB]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Delete the demo SQLite DB before import")
    args = parser.parse_args()

    for command in ("node", "dotnet"):
        if not shutil.which(command):
            raise SystemExit(f"Missing required command: {command}")
    required = [INPUT_ROOT / name for name in ("tables.csv", "jobnet.csv", "executable-mappings.csv", "localized-metadata.csv")]
    missing = [str(path) for path in required if not path.is_file()]
    if not list((INPUT_ROOT / "tables").glob("*.csv")):
        missing.append(str(INPUT_ROOT / "tables/*.csv"))
    if missing:
        raise SystemExit("Missing authoritative CSV files: " + ", ".join(missing))
    if args.fresh:
        for suffix in ("", "-shm", "-wal"):
            (Path(str(DB) + suffix)).unlink(missing_ok=True)

    for label, command in STEPS:
        print(f"\n== {label} ==", flush=True)
        subprocess.run([str(part) for part in command], check=True)
    print(f"\nDone: {DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
