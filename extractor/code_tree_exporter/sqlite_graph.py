from __future__ import annotations

from pathlib import Path


def resolve_database_path(value: Path) -> Path:
    """``graph.sqlite`` given the file itself or the output folder that holds it."""
    path = value.expanduser().resolve()
    if path.is_dir():
        path = path / "graph.sqlite"
    if not path.is_file():
        raise ValueError(f"SQLite graph database not found: {path}")
    return path
