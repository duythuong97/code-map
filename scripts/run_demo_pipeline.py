#!/usr/bin/env python3
"""Extract demo sources, validate CSV packages, then import them into SQLite."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
DB = ROOT / "data/code-flow-demo.sqlite"

STEPS = [
    ("Angular", [PYTHON, "extractors/angular-extractor/main.py", "--config", "configs/angular-customer-web.json"]),
    (".NET API", [PYTHON, "extractors/dotnet-api-extractor/main.py", "--config", "configs/dotnet-api-order-api.json"]),
    (".NET batch", [PYTHON, "extractors/dotnet-batch-extractor/main.py", "--config", "configs/dotnet-batch-order-fulfillment.json"]),
    ("PL/SQL", [PYTHON, "extractors/plsql-extractor/main.py", "--config", "configs/plsql-order-db.json"]),
    ("SQL files", [PYTHON, "extractors/sql-file-extractor/main.py", "--config", "configs/sql-order-ops.json"]),
    ("Validate CSV", [PYTHON, "-m", "application.backend.cli", "validate", "output"]),
    ("Import SQLite", [PYTHON, "-m", "application.backend.cli", "import", "output", "--db", DB]),
    ("SQLite integrity", [PYTHON, "-m", "application.backend.cli", "integrity", "--db", DB]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Delete the demo SQLite DB before import")
    args = parser.parse_args()

    for command in ("node", "dotnet"):
        if not shutil.which(command):
            raise SystemExit(f"Missing required command: {command}")
    required = [
        ROOT / "input-data/tables.csv",
        ROOT / "input-data/jobnet.csv",
        ROOT / "input-data/executable-mappings.csv",
        ROOT / "input-data/localized-metadata.csv",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if not list((ROOT / "input-data/tables").glob("*.csv")):
        missing.append("input-data/tables/*.csv")
    if missing:
        raise SystemExit("Missing authoritative CSV files: " + ", ".join(missing))
    if args.fresh:
        for suffix in ("", "-shm", "-wal"):
            (Path(str(DB) + suffix)).unlink(missing_ok=True)

    for label, command in STEPS:
        print(f"\n== {label} ==", flush=True)
        subprocess.run([str(part) for part in command], cwd=ROOT, check=True)
    print(f"\nDone: {DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
