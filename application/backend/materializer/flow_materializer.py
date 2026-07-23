"""Flow materializer for graph paths and data-flow edges."""
from __future__ import annotations

import sqlite3

from application.backend.importer.pipeline import materialize as _materialize

def materialize(db: sqlite3.Connection) -> None:
    """Materialize graph paths and DATA_FLOW edges."""
    _materialize(db)
