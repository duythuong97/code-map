"""What one MCP server reads: an extract output folder and the knowledge database."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from code_tree_exporter.stale import stale_report, summary_line
from web.backend.storage import GraphNotReady, open_knowledge, open_rag, open_web

from .retriever import Retriever

# Hashing every scanned file is cheap for thousands of files, but not per tool call.
STALE_TTL_SECONDS = float(os.environ.get("CODE_MAP_MCP_STALE_TTL", "60"))


@dataclass
class Workspace:
    output: Path
    knowledge: Path | None = None
    source_root: Path | None = None
    _stale: tuple[float, tuple[int, int], dict[str, Any]] | None = field(default=None, repr=False)

    @contextmanager
    def retriever(self) -> Iterator[Retriever]:
        with open_rag(self.output) as db:
            yield Retriever(db)

    @contextmanager
    def web(self) -> Iterator[sqlite3.Connection | None]:
        """codemap-web.sqlite + knowledge db, or None when either is missing (tools then skip business docs)."""
        if self.knowledge is None:
            yield None
            return
        try:
            with open_web(self.output, self.knowledge) as db:
                yield db
        except GraphNotReady:
            yield None

    @contextmanager
    def knowledge_db(self) -> Iterator[sqlite3.Connection | None]:
        if self.knowledge is None:
            yield None
            return
        with open_knowledge(self.knowledge) as db:
            yield db

    @property
    def graph_path(self) -> Path:
        return self.output / "graph.sqlite"

    def stale(self) -> dict[str, Any]:
        """Stale report of the graph against the working tree, cached for STALE_TTL_SECONDS."""
        stamp = _stamp(self.graph_path)
        now = time.monotonic()
        if self._stale and self._stale[1] == stamp and now - self._stale[0] < STALE_TTL_SECONDS:
            return self._stale[2]
        if not self.graph_path.is_file():
            report: dict[str, Any] = {"fresh": None, "reason": "graph.sqlite not found", "revisions": []}
        else:
            try:
                report = stale_report(self.graph_path, self.source_root)
            except (OSError, sqlite3.Error) as exc:
                report = {"fresh": None, "reason": str(exc), "revisions": []}
        self._stale = (now, stamp, report)
        return report

    def status_line(self) -> str:
        return summary_line(self.stale())

    def changed_files(self) -> set[str]:
        report = self.stale()
        return set(report.get("changed") or []) | set(report.get("deleted") or [])

    def quality(self) -> dict[str, Any]:
        path = self.output / "quality-report.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def _stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return 0, 0
