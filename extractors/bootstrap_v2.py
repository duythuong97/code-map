from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from common.config import ROOT, load_pipeline_config, resolve_path
from extractors.run_all import run_pipeline


def staging_paths(config_path: Path) -> tuple[Path, Path, Path]:
    config_path = config_path.expanduser().resolve()
    live = resolve_path(load_pipeline_config(config_path)["db"], config_path.parent)
    return live, Path(f"{live}.v2.tmp"), Path(f"{live}.v2.ready")


def bootstrap_v2(config_path: Path) -> tuple[Path, dict]:
    live, temporary, ready = staging_paths(config_path)
    lock = Path(f"{temporary}.lock")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"Staging bootstrap already owns: {lock}") from exc
    owns_temporary = False
    try:
        if temporary.exists():
            raise FileExistsError(f"Staging temp already exists: {temporary}")
        if ready.exists():
            raise FileExistsError(f"Staging ready already exists: {ready}")
        owns_temporary = True
        summary = run_pipeline(config_path, rebuild=True, db_override=temporary)
        if summary["status"] != "completed" or summary["failed_count"]:
            raise RuntimeError(f"Staging extraction incomplete: {summary}")
        validate_staging(temporary)
        with sqlite3.connect(temporary) as db:
            db.execute("PRAGMA optimize")
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise RuntimeError(f"WAL checkpoint busy: {checkpoint}")
        try:
            os.link(temporary, ready)
        except FileExistsError as exc:
            raise FileExistsError(f"Staging ready already exists: {ready}") from exc
        temporary.unlink()
        owns_temporary = False
        return ready, summary
    except BaseException:
        if owns_temporary:
            for path in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
                path.unlink(missing_ok=True)
        raise
    finally:
        os.close(lock_fd)
        lock.unlink(missing_ok=True)


def validate_staging(path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"SQLite foreign key check failed: {foreign_keys[:10]}")
        latest = db.execute(
            "SELECT status,failed_count FROM extraction_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest != ("completed", 0):
            raise RuntimeError(f"Staging run is not complete: {latest}")
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "nodes",
                "edges",
                "table_definitions",
                "table_columns",
                "node_facts",
                "edge_facts",
                "metadata_file_facts",
            )
        }
        dangling_edges = db.execute(
            """
            SELECT COUNT(*) FROM edges e
            LEFT JOIN nodes source ON source.qualified_name=e.from_qname
            LEFT JOIN nodes target ON target.qualified_name=e.to_qname
            WHERE source.id IS NULL OR target.id IS NULL
            """
        ).fetchone()[0]
        if dangling_edges:
            raise RuntimeError(f"Staging contains {dangling_edges} dangling edges")
        for table in ("node_facts", "edge_facts", "metadata_file_facts"):
            orphan_count = db.execute(
                f"SELECT COUNT(*) FROM {table} fact "
                "LEFT JOIN extraction_files file ON file.id=fact.file_id "
                "WHERE file.id IS NULL"
            ).fetchone()[0]
            if orphan_count:
                raise RuntimeError(f"Staging contains {orphan_count} orphan {table}")
        orphan_attempts = db.execute(
            """
            SELECT COUNT(*) FROM extraction_run_files attempt
            LEFT JOIN extraction_runs run ON run.id=attempt.run_id
            LEFT JOIN extraction_files file ON file.id=attempt.file_id
            WHERE run.id IS NULL OR file.id IS NULL
            """
        ).fetchone()[0]
        if orphan_attempts:
            raise RuntimeError(
                f"Staging contains {orphan_attempts} orphan extraction attempts"
            )
        db.execute("SELECT qualified_name,properties_json FROM nodes LIMIT 1").fetchall()
        db.execute(
            "SELECT from_qname,to_qname,rel_type,properties_json FROM edges LIMIT 1"
        ).fetchall()
        return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and validate a sibling v2 SQLite staging database."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"),
    )
    return parser.parse_args()


def main() -> int:
    ready, summary = bootstrap_v2(resolve_path(parse_args().config, ROOT))
    print(
        f"Staging ready: {ready} run_id={summary['id']} "
        f"nodes={summary['node_count']} edges={summary['edge_count']}"
    )
    print("Live DB unchanged. Cutover requires an explicit controlled operation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
