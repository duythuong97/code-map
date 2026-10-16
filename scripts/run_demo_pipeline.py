#!/usr/bin/env python3
"""Extract the demo sources; the web app and the VS Code extension read the output folder directly."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.runtime_env import project_path  # noqa: E402

# The interpreter running this script (.venv/bin/python on macOS/Linux,
# .venv\Scripts\python.exe on Windows); a hardcoded bin/python breaks Windows.
PYTHON = Path(sys.executable)
CONFIG = project_path("examples", "demo-config.json")
OUTPUT = project_path("output", "demo")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    steps = [
        ("Validate config and runtimes", [PYTHON, "-m", "code_tree_exporter", "validate", "--config", CONFIG]),
        ("Extract graph", [PYTHON, "-m", "code_tree_exporter", "extract", "--config", CONFIG]),
    ]
    # code_tree_exporter lives in extractor/, the web backend in web/.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "extractor"), str(ROOT), os.environ.get("PYTHONPATH", "")])}
    for label, command in steps:
        print(f"\n== {label} ==", flush=True)
        subprocess.run([str(part) for part in command], check=True, env=env)
    print(f"\nDone: {OUTPUT} (graph.sqlite, codemap-web.sqlite for the web app, codemap-rag.sqlite for VS Code)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
