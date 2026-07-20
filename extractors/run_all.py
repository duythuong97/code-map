from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import time
from itertools import batched
from pathlib import Path
from typing import Iterable

from common.config import (
    ROOT,
    config_hash,
    load_pipeline_config,
    resolve_path,
    stable_hash,
)
from db.writer import delete_file_result, ensure_db_schema, replace_metadata_file
from extractors.import_csv import parse_metadata_facts
from extractors.scanner import process_sources, read_manifest, read_stable_bytes
from extractors.state import (
    ExtractionLeaseLost,
    Lease,
    LeaseHeartbeat,
    acquire_lease,
    begin_run,
    compact_error,
    configure_logging,
    finish_run,
    finish_unleased_run,
    get_or_create_file,
    increment_run,
    mark_file_content_unchanged,
    mark_file_failed,
    mark_source_scanned,
    record_discovered_files,
    record_file_attempt,
    register_source,
    release_lease,
    run_summary,
    stat_matches,
    update_run_current,
    utc_now,
)

_METADATA_HANDLER = "table_definitions"
_METADATA_VERSION = stable_hash({"handler": _METADATA_HANDLER, "contract": "2"})


def run_pipeline(
    config_path: Path,
    *,
    manifest_path: Path | None = None,
    rebuild: bool = False,
    table_definitions: Path | None = None,
    db_override: Path | None = None,
) -> dict:
    config_path = config_path.expanduser().resolve()
    config = load_pipeline_config(config_path)
    imports = list((config.get("imports") or {}).get("csv") or [])
    if table_definitions is not None:
        if len(imports) != 1:
            raise ValueError(
                "--tables requires exactly one configured imports.csv entry"
            )
        imports[0] = {**imports[0], "path": str(table_definitions)}
        config = {
            **config,
            "imports": {**(config.get("imports") or {}), "csv": imports},
        }
    manifest = read_manifest(manifest_path) if manifest_path else None
    base_dir = config_path.parent
    db_path = (
        db_override.expanduser().resolve()
        if db_override is not None
        else resolve_path(config["db"], base_dir)
    )
    state_config = (config.get("extractors") or {}).get("state") or {}
    logger = configure_logging(state_config, base_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path, timeout=30) as db:
        ensure_db_schema(db)
        run_id = begin_run(
            db, config_hash(config), "manifest" if manifest is not None else "full"
        )
        lease: Lease | None = None
        failed = False
        try:
            lease = acquire_lease(
                db, run_id, float(state_config.get("lease_stale_seconds", 120))
            )
            with LeaseHeartbeat(
                db_path, lease, float(state_config.get("heartbeat_seconds", 15))
            ) as heartbeat:
                failures = process_imports(
                    db, imports, base_dir, run_id, lease, rebuild, heartbeat, logger
                )
                failures += process_sources(
                    db,
                    config,
                    base_dir,
                    run_id,
                    lease,
                    manifest,
                    rebuild,
                    heartbeat,
                    logger,
                )
            finish_run(
                db,
                lease,
                run_id,
                "completed_with_errors" if failures else "completed",
            )
        except BaseException as exc:
            failed = True
            try:
                if lease:
                    finish_run(db, lease, run_id, "failed", compact_error(exc))
                else:
                    finish_unleased_run(db, run_id, "failed", compact_error(exc))
            except Exception:
                logger.exception(
                    "failed to persist extraction run failure run_id=%s", run_id
                )
            raise
        finally:
            if lease:
                try:
                    release_lease(db, lease)
                except BaseException:
                    if not failed:
                        raise
                    logger.exception(
                        "failed to release extraction lease run_id=%s", run_id
                    )
        return run_summary(db, run_id)


def process_imports(
    db: sqlite3.Connection,
    imports: list[dict],
    base_dir: Path,
    run_id: int,
    lease: Lease,
    rebuild: bool,
    heartbeat: LeaseHeartbeat,
    logger: logging.Logger,
) -> int:
    failures = 0
    for entry in imports:
        configured_path = resolve_path(entry["path"], base_dir)
        source_root, candidates = _metadata_candidates(configured_path)
        db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS seen_files("
            "source_id TEXT,relative_path TEXT,handler_name TEXT,"
            "PRIMARY KEY(source_id,relative_path,handler_name)) WITHOUT ROWID"
        )
        db.execute("DELETE FROM seen_files WHERE source_id=?", (entry["id"],))
        db.commit()
        scan_ok = False
        try:
            for candidate_batch in batched(candidates, 1000):
                relative_paths = [
                    file_path.relative_to(source_root).as_posix()
                    for file_path in candidate_batch
                ]
                record_discovered_files(
                    db,
                    lease,
                    run_id,
                    entry["id"],
                    [
                        (relative_path, _METADATA_HANDLER)
                        for relative_path in relative_paths
                    ],
                )
                deferred_counters: dict[str, int] = {}
                try:
                    for file_path, relative_path in zip(
                        candidate_batch, relative_paths, strict=True
                    ):
                        file_state = get_or_create_file(
                            db,
                            lease=lease,
                            source_id=entry["id"],
                            source_root=str(source_root),
                            relative_path=relative_path,
                            handler_name=_METADATA_HANDLER,
                        )
                        heartbeat.check()
                        failures += _process_metadata_file(
                            db,
                            entry,
                            file_path,
                            relative_path,
                            file_state,
                            run_id,
                            lease,
                            rebuild,
                            logger,
                            deferred_counters,
                        )
                finally:
                    if deferred_counters:
                        increment_run(db, lease, run_id, **deferred_counters)
            scan_ok = True
        finally:
            if scan_ok:
                _cleanup_metadata_files(db, entry["id"], run_id, lease, logger)
                register_source(
                    db,
                    lease=lease,
                    source_id=entry["id"],
                    kind="import",
                    source_root=str(source_root),
                    rule_hash=_metadata_context_hash(entry),
                    priority=int(entry.get("priority", 0)),
                )
                mark_source_scanned(db, lease, entry["id"], run_id)
    return failures


def _metadata_candidates(path: Path) -> tuple[Path, Iterable[Path]]:
    if path.is_file():
        if path.suffix.lower() != ".csv":
            raise ValueError(f"Metadata import is not a CSV file: {path}")
        return path.parent, (path,)
    if path.is_dir():
        return path, _walk_csv(path)
    if path.suffix.lower() == ".csv" and path.parent.is_dir():
        return path.parent, ()
    raise ValueError(f"Metadata import path does not exist: {path}")


def _walk_csv(root: Path) -> Iterable[Path]:
    def raise_error(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(root, onerror=raise_error):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.suffix.lower() == ".csv":
                yield path


def _metadata_context_hash(entry: dict) -> str:
    return stable_hash(
        {
            "kind": entry["kind"],
            "db_name": entry["db_name"],
            "schema": entry["schema"],
            "encoding": entry.get("encoding", "utf-8"),
            "priority": int(entry.get("priority", 0)),
        }
    )


def _process_metadata_file(
    db: sqlite3.Connection,
    entry: dict,
    file_path: Path,
    relative_path: str,
    file_state,
    run_id: int,
    lease: Lease,
    rebuild: bool,
    logger: logging.Logger,
    deferred_counters: dict[str, int] | None = None,
) -> int:
    started, clock = utc_now(), time.perf_counter()
    context_hash = _metadata_context_hash(entry)
    try:
        stat = file_path.stat()
    except ExtractionLeaseLost:
        raise
    except Exception as exc:
        return _record_metadata_failure(
            db,
            entry["id"],
            relative_path,
            file_state.id,
            run_id,
            lease,
            started,
            clock,
            exc,
            logger,
        )
    if not rebuild and stat_matches(
        file_state,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
        ctime_ns=stat.st_ctime_ns,
        context_hash=context_hash,
        handler_version=_METADATA_VERSION,
    ):
        if deferred_counters is None:
            increment_run(db, lease, run_id, skipped_count=1)
        else:
            deferred_counters["skipped_count"] = (
                deferred_counters.get("skipped_count", 0) + 1
            )
        logger.debug(
            "file skipped source=%s path=%s handler=%s reason=stat_unchanged",
            entry["id"],
            relative_path,
            _METADATA_HANDLER,
        )
        return 0
    update_run_current(db, lease, run_id, entry["id"], "", relative_path)
    try:
        data, stat = read_stable_bytes(file_path)
        digest = hashlib.sha256(data).hexdigest()
    except ExtractionLeaseLost:
        raise
    except Exception as exc:
        return _record_metadata_failure(
            db,
            entry["id"],
            relative_path,
            file_state.id,
            run_id,
            lease,
            started,
            clock,
            exc,
            logger,
        )
    if (
        not rebuild
        and file_state.status == "success"
        and file_state.successful_sha256 == digest
        and file_state.successful_rule_context_hash == context_hash
        and file_state.successful_handler_version == _METADATA_VERSION
    ):
        mark_file_content_unchanged(
            db,
            lease=lease,
            file_id=file_state.id,
            run_id=run_id,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            device=stat.st_dev,
            inode=stat.st_ino,
            ctime_ns=stat.st_ctime_ns,
        )
        record_file_attempt(
            db,
            lease=lease,
            run_id=run_id,
            file_id=file_state.id,
            action="content_unchanged",
            status="success",
            started_at=started,
            elapsed_ms=_elapsed_ms(clock),
            skip_reason="sha256 unchanged",
        )
        if deferred_counters is None:
            increment_run(db, lease, run_id, skipped_count=1)
        else:
            deferred_counters["skipped_count"] = (
                deferred_counters.get("skipped_count", 0) + 1
            )
        return 0
    try:
        facts = parse_metadata_facts(
            data,
            file_path,
            {"db_name": entry["db_name"], "schema": entry["schema"]},
            encoding=entry.get("encoding", "utf-8"),
        )
        replace_metadata_file(
            db,
            lease=lease,
            file_id=file_state.id,
            run_id=run_id,
            facts=facts,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=digest,
            rule_context_hash=context_hash,
            handler_version=_METADATA_VERSION,
            device=stat.st_dev,
            inode=stat.st_ino,
            ctime_ns=stat.st_ctime_ns,
            priority=int(entry.get("priority", 0)),
        )
    except ExtractionLeaseLost:
        raise
    except Exception as exc:
        return _record_metadata_failure(
            db,
            entry["id"],
            relative_path,
            file_state.id,
            run_id,
            lease,
            started,
            clock,
            exc,
            logger,
        )
    elapsed = _elapsed_ms(clock)
    record_file_attempt(
        db,
        lease=lease,
        run_id=run_id,
        file_id=file_state.id,
        action="extracted",
        status="success",
        started_at=started,
        elapsed_ms=elapsed,
        node_count=len(facts),
    )
    increment_run(db, lease, run_id, extracted_count=1, node_count=len(facts))
    logger.info(
        "file extracted source=%s path=%s handler=%s elapsed_ms=%d nodes=%d edges=0",
        entry["id"],
        relative_path,
        _METADATA_HANDLER,
        elapsed,
        len(facts),
    )
    return 0


def _record_metadata_failure(
    db: sqlite3.Connection,
    source_id: str,
    relative_path: str,
    file_id: int,
    run_id: int,
    lease: Lease,
    started: str,
    clock: float,
    exc: BaseException,
    logger: logging.Logger,
) -> int:
    error = compact_error(exc)
    mark_file_failed(db, lease, file_id, error)
    record_file_attempt(
        db,
        lease=lease,
        run_id=run_id,
        file_id=file_id,
        action="failed",
        status="failed",
        started_at=started,
        elapsed_ms=_elapsed_ms(clock),
        error=error,
    )
    increment_run(db, lease, run_id, failed_count=1)
    logger.error(
        "file failed source=%s path=%s error=%s", source_id, relative_path, error
    )
    return 1


def _cleanup_metadata_files(
    db: sqlite3.Connection,
    source_id: str,
    run_id: int,
    lease: Lease,
    logger: logging.Logger,
) -> None:
    rows = db.execute(
        """
        SELECT f.id,f.relative_path FROM extraction_files f
        LEFT JOIN seen_files s ON s.source_id=f.source_id
          AND s.relative_path=f.relative_path AND s.handler_name=f.handler_name
        WHERE f.source_id=? AND f.handler_name=? AND f.status<>'deleted'
          AND s.source_id IS NULL
        """,
        (source_id, _METADATA_HANDLER),
    ).fetchall()
    for file_id, relative_path in rows:
        started, clock = utc_now(), time.perf_counter()
        delete_file_result(db, lease=lease, file_id=file_id, run_id=run_id)
        record_file_attempt(
            db,
            lease=lease,
            run_id=run_id,
            file_id=file_id,
            action="deleted",
            status="success",
            started_at=started,
            elapsed_ms=_elapsed_ms(clock),
        )
        increment_run(db, lease, run_id, deleted_count=1)
        logger.info(
            "file deleted source=%s path=%s handler=%s",
            source_id,
            relative_path,
            _METADATA_HANDLER,
        )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally import metadata, then extract source lineage."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"),
    )
    parser.add_argument("--tables", help="Override the single imports.csv path.")
    parser.add_argument("--files-from", help="Git name-status source manifest.")
    parser.add_argument(
        "--rebuild", action="store_true", help="Reparse discovered files."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = resolve_path(args.config, ROOT)
    summary = run_pipeline(
        config_path,
        manifest_path=(
            resolve_path(args.files_from, Path.cwd()) if args.files_from else None
        ),
        rebuild=args.rebuild,
        table_definitions=(
            resolve_path(args.tables, config_path.parent) if args.tables else None
        ),
    )
    print(
        "Extraction run "
        f"id={summary['id']} status={summary['status']} "
        f"discovered={summary['discovered_count']} extracted={summary['extracted_count']} "
        f"skipped={summary['skipped_count']} failed={summary['failed_count']} "
        f"deleted={summary['deleted_count']} nodes={summary['node_count']} "
        f"edges={summary['edge_count']}"
    )
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
