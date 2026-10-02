#!/usr/bin/env python3
"""Extract the demo sources into graph.sqlite, then import it for the UI."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.runtime_env import project_path  # noqa: E402

# The interpreter running this script (.venv/bin/python on macOS/Linux,
# .venv\Scripts\python.exe on Windows); a hardcoded bin/python breaks Windows.
PYTHON = Path(sys.executable)
CONFIG = project_path("demo-config.json")
OUTPUT = project_path("output", "demo")
DB = project_path("data", "code-flow-demo.sqlite")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Delete the demo SQLite DB before import")
    args = parser.parse_args()
    if args.fresh:
        for suffix in ("", "-shm", "-wal"):
            Path(str(DB) + suffix).unlink(missing_ok=True)
    steps = [
        ("Validate config and runtimes", [PYTHON, "-m", "code_tree_exporter", "validate", "--config", CONFIG]),
        ("Extract graph", [PYTHON, "-m", "code_tree_exporter", "extract", "--config", CONFIG]),
        ("Import graph", [PYTHON, "-m", "application.backend.cli", "import-graph", OUTPUT, "--db", DB]),
        ("SQLite integrity", [PYTHON, "-m", "application.backend.cli", "integrity", "--db", DB]),
    ]
    for label, command in steps:
        print(f"\n== {label} ==", flush=True)
        subprocess.run([str(part) for part in command], check=True)
    print(f"\nDone: {DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
