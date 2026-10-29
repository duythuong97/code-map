#!/usr/bin/env python3
"""Extract the demo sources; the web app and the MCP server read the output folder directly."""
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


# .env values the repository layout expects (paths are relative to examples/demo-config.json).
EXPECTED_ENV = {
    "CODE_MAP_SOURCE_ROOT": str(ROOT / "examples" / "sample-source"),
    "CODE_TREE_OUTPUT": "../output/demo",
    "CODE_MAP_INPUT_DATA": "./input-data",
    "CODE_TREE_CATALOG": "./catalog",
    "CODE_MAP_GRAPH_OUTPUT": str(ROOT / "output" / "demo"),
}


def stale_env() -> list[str]:
    """.env lines that still point at the old layout (before extractor/, web/, examples/)."""
    problems = []
    for name, expected in EXPECTED_ENV.items():
        value = os.environ.get(name, "")
        if name in {"CODE_MAP_SOURCE_ROOT", "CODE_MAP_GRAPH_OUTPUT"}:
            ok = bool(value) and (Path(value).is_dir() or name == "CODE_MAP_GRAPH_OUTPUT")
        else:
            ok = bool(value) and (CONFIG.parent / value).resolve() == (CONFIG.parent / expected).resolve()
        if not ok:
            problems.append(f"  {name}={expected}    (now: {value or '<missing>'})")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    problems = stale_env()
    if problems:
        print(f"Update {ROOT / '.env'} for the new layout (see .env.example):\n" + "\n".join(problems), file=sys.stderr)
        return 1
    steps = [
        ("Validate config and runtimes", [PYTHON, "-m", "code_tree_exporter", "validate", "--config", CONFIG]),
        ("Extract graph", [PYTHON, "-m", "code_tree_exporter", "extract", "--config", CONFIG]),
    ]
    # code_tree_exporter lives in extractor/, the web backend in web/.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "extractor"), str(ROOT), os.environ.get("PYTHONPATH", "")])}
    for label, command in steps:
        print(f"\n== {label} ==", flush=True)
        if subprocess.run([str(part) for part in command], env=env).returncode != 0:
            print(f"\n{label} failed; the error is printed above.", file=sys.stderr)
            return 1
    print(f"\nDone: {OUTPUT} (graph.sqlite, codemap-web.sqlite for the web app, codemap-rag.sqlite for VS Code)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
