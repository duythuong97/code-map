"""Client for the .NET Oracle SQL service (extractors/sql-service).

The Python ANTLR runtime is the slowest part of extraction; the same grammar
on the .NET runtime is about ten times faster and parses a batch in
parallel. ``CODE_TREE_SQL_PARSER`` picks the backend:

* ``auto`` (default): .NET when a dotnet executable is available, else Python.
* ``dotnet``: require .NET; fail if it cannot be started.
* ``python``: always use the in-process Python parser.

Both backends return identical results (see tests/test_dotnet_sql_parity.py).
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[1] / "sql-service"
_PROJECT = _SERVICE_DIR / "CodeTreeSqlService.csproj"
_PREFETCH_BATCH = 256

_lock = threading.Lock()
_service: "SqlService | None" = None
_disabled = False
_summaries: dict[str, dict] = {}


class SqlServiceError(RuntimeError):
    pass


class SqlService:
    def __init__(self, dotnet: str, assembly: Path) -> None:
        from code_tree_exporter.extractors.dotnet_runner import dotnet_environment

        self._process = subprocess.Popen(
            [dotnet, str(assembly)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            env=dotnet_environment(dotnet),
        )
        self._next_id = 0
        atexit.register(self.close)

    def request(self, op: str, key: str, items: list) -> list:
        if not items:
            return []
        self._next_id += 1
        payload = json.dumps(
            {"id": self._next_id, "op": op, key: items}, ensure_ascii=False
        )
        assert self._process.stdin and self._process.stdout
        try:
            self._process.stdin.write(payload + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise SqlServiceError(f".NET SQL service stopped: {exc}") from exc
        if not line:
            raise SqlServiceError(".NET SQL service stopped unexpectedly")
        response = json.loads(line)
        if "error" in response:
            raise SqlServiceError(response["error"])
        return response["results"]

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()


def backend() -> str:
    value = os.environ.get("CODE_TREE_SQL_PARSER", "auto").strip().lower()
    if value not in {"auto", "dotnet", "python"}:
        raise ValueError("CODE_TREE_SQL_PARSER must be auto, dotnet or python")
    return value


def service() -> SqlService | None:
    """The process-wide .NET service, or None when the Python parser is used."""
    global _service, _disabled
    if _service is not None or _disabled:
        return _service
    with _lock:
        if _service is not None or _disabled:
            return _service
        mode = backend()
        dotnet = (
            None
            if mode == "python"
            else os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
        )
        if not dotnet:
            if mode == "dotnet":
                raise SqlServiceError(
                    "CODE_TREE_SQL_PARSER=dotnet but no dotnet executable was found"
                )
            _disabled = True
            return None
        try:
            assembly = ensure_built(dotnet)
            _service = SqlService(dotnet, assembly)
        except Exception as exc:
            if mode == "dotnet":
                raise
            print(
                f"warning: .NET SQL parser unavailable, using Python parser: {exc}",
                file=sys.stderr,
            )
            _disabled = True
        return _service


def ensure_built(dotnet: str) -> Path:
    from code_tree_exporter.extractors.dotnet_runner import (
        ensure_worker_built,
        worker_assembly_path,
    )

    script = _SERVICE_DIR / "main.py"
    returncode = ensure_worker_built(script, _PROJECT.name, dotnet=dotnet)
    if returncode:
        raise SqlServiceError(f"dotnet build of {_PROJECT.name} failed ({returncode})")
    return worker_assembly_path(script, _PROJECT.name)


def prefetch(texts) -> None:
    """Parse many texts in one parallel batch; later lookups hit the cache."""
    remote = service()
    if remote is None:
        return
    pending = [text for text in dict.fromkeys(texts) if text not in _summaries]
    for index in range(0, len(pending), _PREFETCH_BATCH):
        batch = pending[index : index + _PREFETCH_BATCH]
        for text, summary in zip(batch, remote.request("parse", "texts", batch)):
            _summaries[text] = summary


def summary(text: str) -> dict | None:
    """Parse summary for ``text`` from the .NET service, or None for Python."""
    remote = service()
    if remote is None:
        return None
    cached = _summaries.get(text)
    if cached is None:
        cached = remote.request("parse", "texts", [text])[0]
        _summaries[text] = cached
    return cached


def steps(text: str, source_path: str, base_line: int) -> list[dict] | None:
    """Unresolved PL/SQL semantic steps, or None when Python is used."""
    remote = service()
    if remote is None:
        return None
    item = {"text": text, "source_path": source_path, "base_line": base_line}
    return remote.request("steps", "items", [item])[0]
