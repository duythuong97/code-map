"""Cross-source resolver for the CSV graph database."""
from __future__ import annotations

import sqlite3

from application.backend.importer.pipeline import resolve as _resolve

def resolve(db: sqlite3.Connection) -> None:
    """Resolve explicit source references into canonical graph edges."""
    _resolve(db)
