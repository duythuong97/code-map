"""What one MCP server reads: an extract output folder and the knowledge database."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from code_tree_exporter.stale import stale_report, summary_line
from codemap.storage import GraphNotReady, open_knowledge, open_search, open_web

from .retriever import Retriever

# Checking the working tree (file times, hashes of touched files, VCS log) is redone this often, in the
# background: an answer uses the last result and never waits for the check, except up to
# STALE_WAIT_SECONDS for the first one.
STALE_TTL_SECONDS = float(os.environ.get("CODE_MAP_MCP_STALE_TTL", "300"))
STALE_WAIT_SECONDS = 3.0
logger = logging.getLogger("code_map.workspace")


@dataclass
class Workspace:
    output: Path
    knowledge: Path | None = None
    source_root: Path | None = None
    _stale: tuple[float, tuple[int, int], dict[str, Any]] | None = field(default=None, repr=False)
    _checking: threading.Thread | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @contextmanager
    def retriever(self) -> Iterator[Retriever]:
        with open_search(self.output) as db:
            yield Retriever(db)

    @contextmanager
    def web(self) -> Iterator[sqlite3.Connection | None]:
        """codemap.sqlite + knowledge db, or None when either is missing (tools then skip business docs)."""
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
        """Stale report of the graph against the working tree, refreshed in the background every STALE_TTL_SECONDS."""
        stamp = _stamp(self.graph_path)
        with self._lock:
            cached = self._stale if self._stale and self._stale[1] == stamp else None
            if cached and time.monotonic() - cached[0] < STALE_TTL_SECONDS:
                return cached[2]
            checking = self._checking
            if checking is None or not checking.is_alive():
                checking = self._checking = threading.Thread(target=self._check, args=(stamp,), daemon=True)
                checking.start()
        if cached:
            return cached[2]
        checking.join(STALE_WAIT_SECONDS)
        with self._lock:
            if self._stale and self._stale[1] == stamp:
                return self._stale[2]
        return {"fresh": None, "reason": "still comparing the working tree with the extract", "revisions": []}

    def _check(self, stamp: tuple[int, int]) -> None:
        if not self.graph_path.is_file():
            report: dict[str, Any] = {"fresh": None, "reason": "graph.sqlite not found", "revisions": []}
        else:
            try:
                report = stale_report(self.graph_path, self.source_root)
            except (OSError, sqlite3.Error) as exc:
                report = {"fresh": None, "reason": str(exc), "revisions": []}
            except Exception as exc:  # noqa: BLE001  (a failed check must not stop the next one)
                logger.exception("stale check failed")
                report = {"fresh": None, "reason": str(exc), "revisions": []}
        with self._lock:
            self._stale = (time.monotonic(), stamp, report)

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


_WORKSPACES: dict[tuple[str, str], Workspace] = {}


def workspace_for(output: Path, knowledge: Path | None) -> Workspace:
    """One Workspace per (output, knowledge) so the stale report cache is shared across requests."""
    key = (str(output), str(knowledge))
    workspace = _WORKSPACES.get(key)
    if workspace is None:
        workspace = _WORKSPACES[key] = Workspace(output=Path(output), knowledge=Path(knowledge) if knowledge else None)
    return workspace
