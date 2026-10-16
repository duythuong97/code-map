from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

STATE_DDL = """
CREATE TABLE IF NOT EXISTS extraction_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  current_source_id TEXT,
  current_project TEXT,
  current_relative_path TEXT,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  extracted_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  deleted_count INTEGER NOT NULL DEFAULT 0,
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS extraction_sources (
  source_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  source_root TEXT NOT NULL,
  source_rule_hash TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  last_full_scan_run_id INTEGER,
  retired_at TEXT
);
CREATE TABLE IF NOT EXISTS extraction_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  source_root TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  handler_name TEXT NOT NULL,
  project_name TEXT,
  project_relative_root TEXT,
  project_file TEXT,
  successful_size INTEGER,
  successful_mtime_ns INTEGER,
  successful_device INTEGER,
  successful_inode INTEGER,
  successful_ctime_ns INTEGER,
  successful_sha256 TEXT,
  successful_rule_context_hash TEXT,
  successful_handler_version TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  last_success_run_id INTEGER,
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(source_id, relative_path, handler_name)
);
CREATE TABLE IF NOT EXISTS extraction_run_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  file_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  skip_reason TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  elapsed_ms INTEGER NOT NULL DEFAULT 0,
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS extraction_lease (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  owner_run_id INTEGER,
  fencing_token INTEGER NOT NULL DEFAULT 0,
  heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_files_source ON extraction_files(source_id, handler_name, relative_path);
CREATE INDEX IF NOT EXISTS idx_extraction_run_files_run ON extraction_run_files(run_id);
"""


class ExtractionLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    run_id: int
    fencing_token: int


@dataclass(frozen=True)
class FileState:
    id: int
    successful_size: int | None
    successful_mtime_ns: int | None
    successful_device: int | None
    successful_inode: int | None
    successful_ctime_ns: int | None
    successful_sha256: str | None
    successful_rule_context_hash: str | None
    successful_handler_version: str | None
    status: str
    project_name: str
    project_relative_root: str
    project_file: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_state_schema(db: sqlite3.Connection) -> None:
    db.executescript(STATE_DDL)
    existing = {
        row[1] for row in db.execute("PRAGMA table_info(extraction_files)").fetchall()
    }
    for name in ("successful_device", "successful_inode", "successful_ctime_ns"):
        if name not in existing:
            db.execute(f"ALTER TABLE extraction_files ADD COLUMN {name} INTEGER")
    db.execute(
        "INSERT OR IGNORE INTO extraction_lease(singleton, fencing_token) VALUES(1, 0)"
    )
    db.commit()


def configure_logging(state_config: dict[str, Any], base_dir: Path) -> logging.Logger:
    logger = logging.getLogger("code_map.extraction")
    if getattr(logger, "_code_map_configured", False):
        return logger
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    log_path = state_config.get("log_path")
    if log_path:
        path = Path(log_path).expanduser()
        path = path if path.is_absolute() else (base_dir / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=int(state_config.get("log_max_bytes", 10 * 1024 * 1024)),
            backupCount=int(state_config.get("log_backups", 5)),
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger._code_map_configured = True  # type: ignore[attr-defined]
    return logger


def begin_run(db: sqlite3.Connection, config_hash: str, mode: str) -> int:
    ensure_state_schema(db)
    now = utc_now()
    cursor = db.execute(
        "INSERT INTO extraction_runs(started_at, status, mode, config_hash) VALUES(?, 'running', ?, ?)",
        (now, mode, config_hash),
    )
    db.commit()
    return int(cursor.lastrowid)


def acquire_lease(
    db: sqlite3.Connection, run_id: int, stale_after_seconds: int = 120
) -> Lease:
    ensure_state_schema(db)
    db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        "SELECT owner_run_id, fencing_token, heartbeat_at FROM extraction_lease WHERE singleton=1"
    ).fetchone()
    owner, token, heartbeat = row
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    active = owner is not None and heartbeat and datetime.fromisoformat(heartbeat) > cutoff
    if active and int(owner) != run_id:
        db.rollback()
        raise RuntimeError(f"Extraction lease is held by run {owner}")
    token = int(token) + 1
    now = utc_now()
    db.execute(
        "UPDATE extraction_lease SET owner_run_id=?, fencing_token=?, heartbeat_at=? WHERE singleton=1",
        (run_id, token, now),
    )
    db.execute(
        """
        UPDATE extraction_runs SET status='interrupted', finished_at=?
        WHERE status='running' AND id<>?
        """,
        (now, run_id),
    )
    db.commit()
    return Lease(run_id, token)


def assert_lease(db: sqlite3.Connection, lease: Lease) -> None:
    row = db.execute(
        "SELECT owner_run_id, fencing_token FROM extraction_lease WHERE singleton=1"
    ).fetchone()
    if not row or row != (lease.run_id, lease.fencing_token):
        raise ExtractionLeaseLost("Extraction lease lost; stale writer fenced")


def renew_lease(db: sqlite3.Connection, lease: Lease) -> None:
    cursor = db.execute(
        """
        UPDATE extraction_lease SET heartbeat_at=?
        WHERE singleton=1 AND owner_run_id=? AND fencing_token=?
        """,
        (utc_now(), lease.run_id, lease.fencing_token),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise ExtractionLeaseLost("Extraction lease lost; heartbeat rejected")
    db.commit()


def release_lease(db: sqlite3.Connection, lease: Lease) -> None:
    db.execute(
        """
        UPDATE extraction_lease SET owner_run_id=NULL, heartbeat_at=NULL
        WHERE singleton=1 AND owner_run_id=? AND fencing_token=?
        """,
        (lease.run_id, lease.fencing_token),
    )
    db.commit()


class LeaseHeartbeat(AbstractContextManager["LeaseHeartbeat"]):
    def __init__(self, db_path: Path, lease: Lease, interval_seconds: float = 15.0):
        self.db_path = db_path
        self.lease = lease
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        self._thread.join()
        if exc_type is None:
            self.check()

    def check(self) -> None:
        if self._failure:
            raise ExtractionLeaseLost("Extraction lease heartbeat failed") from self._failure
        try:
            with sqlite3.connect(self.db_path, timeout=30) as db:
                assert_lease(db, self.lease)
        except BaseException as exc:
            raise ExtractionLeaseLost("Extraction lease ownership lost") from exc

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with sqlite3.connect(self.db_path, timeout=30) as db:
                    renew_lease(db, self.lease)
            except BaseException as exc:  # thread boundary; surfaced by check().
                self._failure = exc
                self._stop.set()


def source_registration_changed(
    db: sqlite3.Connection,
    *,
    source_id: str,
    source_root: str,
    rule_hash: str,
    priority: int,
) -> bool:
    old = db.execute(
        "SELECT source_root, source_rule_hash, priority FROM extraction_sources WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return old is None or old != (source_root, rule_hash, priority)


def register_source(
    db: sqlite3.Connection,
    *,
    lease: Lease,
    source_id: str,
    kind: str,
    source_root: str,
    rule_hash: str,
    priority: int,
) -> bool:
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        changed = source_registration_changed(
            db,
            source_id=source_id,
            source_root=source_root,
            rule_hash=rule_hash,
            priority=priority,
        )
        db.execute(
            """
            INSERT INTO extraction_sources(source_id, kind, source_root, source_rule_hash, priority, retired_at)
            VALUES(?,?,?,?,?,NULL)
            ON CONFLICT(source_id) DO UPDATE SET
              kind=excluded.kind,
              source_root=excluded.source_root,
              source_rule_hash=excluded.source_rule_hash,
              priority=excluded.priority,
              retired_at=NULL
            """,
            (source_id, kind, source_root, rule_hash, priority),
        )
        db.commit()
        return changed
    except BaseException:
        db.rollback()
        raise


def mark_source_scanned(
    db: sqlite3.Connection, lease: Lease, source_id: str, run_id: int
) -> None:
    _require_run(lease, run_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.execute(
            "UPDATE extraction_sources SET last_full_scan_run_id=? WHERE source_id=?",
            (run_id, source_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def get_or_create_file(
    db: sqlite3.Connection,
    *,
    lease: Lease,
    source_id: str,
    source_root: str,
    relative_path: str,
    handler_name: str,
    project_name: str = "",
    project_relative_root: str = "",
    project_file: str = "",
) -> FileState:
    row = db.execute(
        """
        SELECT id, successful_size, successful_mtime_ns, successful_device,
               successful_inode, successful_ctime_ns, successful_sha256,
               successful_rule_context_hash, successful_handler_version, status,
               COALESCE(project_name,''), COALESCE(project_relative_root,''),
               COALESCE(project_file,''), source_root
        FROM extraction_files
        WHERE source_id=? AND relative_path=? AND handler_name=?
        """,
        (source_id, relative_path, handler_name),
    ).fetchone()
    desired_identity = (project_name, project_relative_root, project_file, source_root)
    if row is not None and tuple(row[10:]) == desired_identity:
        return FileState(*row[:13])

    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        if row is None:
            db.execute(
                """
                INSERT INTO extraction_files(
                  source_id, source_root, relative_path, handler_name,
                  project_name, project_relative_root, project_file, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    source_id,
                    source_root,
                    relative_path,
                    handler_name,
                    project_name,
                    project_relative_root,
                    project_file,
                    utc_now(),
                ),
            )
        else:
            db.execute(
                """
                UPDATE extraction_files SET source_root=?, project_name=?,
                  project_relative_root=?, project_file=?, updated_at=?
                WHERE id=?
                """,
                (
                    source_root,
                    project_name,
                    project_relative_root,
                    project_file,
                    utc_now(),
                    row[0],
                ),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    if row is None:
        row = db.execute(
            """
            SELECT id, successful_size, successful_mtime_ns, successful_device,
                   successful_inode, successful_ctime_ns, successful_sha256,
                   successful_rule_context_hash, successful_handler_version, status,
                   COALESCE(project_name,''), COALESCE(project_relative_root,''),
                   COALESCE(project_file,''), source_root
            FROM extraction_files
            WHERE source_id=? AND relative_path=? AND handler_name=?
            """,
            (source_id, relative_path, handler_name),
        ).fetchone()
    else:
        row = (*row[:10], project_name, project_relative_root, project_file, source_root)
    return FileState(*row[:13])


def stat_matches(
    state: FileState,
    *,
    size: int,
    mtime_ns: int,
    device: int,
    inode: int,
    ctime_ns: int,
    context_hash: str,
    handler_version: str,
) -> bool:
    return (
        state.status == "success"
        and state.successful_size == size
        and state.successful_mtime_ns == mtime_ns
        and state.successful_device == device
        and state.successful_inode == inode
        and state.successful_ctime_ns == ctime_ns
        and state.successful_rule_context_hash == context_hash
        and state.successful_handler_version == handler_version
    )


def update_run_current(
    db: sqlite3.Connection,
    lease: Lease,
    run_id: int,
    source_id: str,
    project: str,
    relative_path: str,
) -> None:
    _require_run(lease, run_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.execute(
            """
            UPDATE extraction_runs
            SET current_source_id=?, current_project=?, current_relative_path=?
            WHERE id=?
            """,
            (source_id, project, relative_path, run_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def record_discovered_files(
    db: sqlite3.Connection,
    lease: Lease,
    run_id: int,
    source_id: str,
    entries: list[tuple[str, str]],
) -> None:
    if not entries:
        return
    _require_run(lease, run_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.executemany(
            "INSERT OR IGNORE INTO seen_files VALUES(?,?,?)",
            ((source_id, relative_path, handler_name) for relative_path, handler_name in entries),
        )
        db.execute(
            "UPDATE extraction_runs SET discovered_count=discovered_count+? WHERE id=?",
            (len(entries), run_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise

def increment_run(
    db: sqlite3.Connection, lease: Lease, run_id: int, **counters: int
) -> None:
    allowed = {
        "discovered_count",
        "extracted_count",
        "skipped_count",
        "failed_count",
        "deleted_count",
        "node_count",
        "edge_count",
    }
    invalid = set(counters) - allowed
    if invalid:
        raise ValueError(f"Invalid run counters: {sorted(invalid)}")
    if not counters:
        return
    _require_run(lease, run_id)
    assignments = ", ".join(f"{name}={name}+?" for name in counters)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.execute(
            f"UPDATE extraction_runs SET {assignments} WHERE id=?",
            (*counters.values(), run_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def mark_file_content_unchanged(
    db: sqlite3.Connection,
    *,
    lease: Lease,
    file_id: int,
    run_id: int,
    size: int,
    mtime_ns: int,
    device: int,
    inode: int,
    ctime_ns: int,
) -> None:
    _require_run(lease, run_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.execute(
            """
            UPDATE extraction_files SET successful_size=?, successful_mtime_ns=?,
              successful_device=?, successful_inode=?, successful_ctime_ns=?,
              status='success', last_success_run_id=?, last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (
                size,
                mtime_ns,
                device,
                inode,
                ctime_ns,
                run_id,
                utc_now(),
                file_id,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def record_file_attempt(
    db: sqlite3.Connection,
    *,
    lease: Lease,
    run_id: int,
    file_id: int,
    action: str,
    status: str,
    started_at: str,
    elapsed_ms: int,
    node_count: int = 0,
    edge_count: int = 0,
    skip_reason: str = "",
    error: str = "",
) -> None:
    _require_run(lease, run_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.execute(
            """
            INSERT INTO extraction_run_files(
              run_id, file_id, action, status, skip_reason, started_at, finished_at,
              elapsed_ms, node_count, edge_count, error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                file_id,
                action,
                status,
                skip_reason or None,
                started_at,
                utc_now(),
                elapsed_ms,
                node_count,
                edge_count,
                error or None,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def mark_file_failed(
    db: sqlite3.Connection, lease: Lease, file_id: int, error: str
) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        db.execute(
            "UPDATE extraction_files SET status='failed', last_error=?, updated_at=? WHERE id=?",
            (error[:4000], utc_now(), file_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def finish_run(
    db: sqlite3.Connection,
    lease: Lease,
    run_id: int,
    status: str,
    error: str = "",
) -> None:
    _validate_run_status(status)
    _require_run(lease, run_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(db, lease)
        _update_run_finished(db, run_id, status, error)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def finish_unleased_run(
    db: sqlite3.Connection, run_id: int, status: str, error: str = ""
) -> None:
    _validate_run_status(status)
    db.execute("BEGIN IMMEDIATE")
    try:
        owner = db.execute(
            "SELECT owner_run_id FROM extraction_lease WHERE singleton=1"
        ).fetchone()
        if owner and owner[0] == run_id:
            raise RuntimeError("Leased run must be finished with its fencing token")
        _update_run_finished(db, run_id, status, error)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _update_run_finished(
    db: sqlite3.Connection, run_id: int, status: str, error: str
) -> None:
    db.execute(
        """
        UPDATE extraction_runs
        SET status=?, finished_at=?, current_source_id=NULL, current_project=NULL,
            current_relative_path=NULL, error=?
        WHERE id=? AND status='running'
        """,
        (status, utc_now(), error or None, run_id),
    )


def _validate_run_status(status: str) -> None:
    if status not in {"completed", "completed_with_errors", "failed", "interrupted"}:
        raise ValueError(f"Invalid run status: {status}")


def _require_run(lease: Lease, run_id: int) -> None:
    if lease.run_id != run_id:
        raise ExtractionLeaseLost("Lease does not belong to requested run")


def run_summary(db: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM extraction_runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row:
        return {}
    columns = [item[0] for item in db.execute("SELECT * FROM extraction_runs LIMIT 0").description]
    return dict(zip(columns, row))


def compact_error(exc: BaseException) -> str:
    return json.dumps(
        {"type": type(exc).__name__, "message": str(exc)},
        ensure_ascii=False,
        separators=(",", ":"),
    )[:4000]
