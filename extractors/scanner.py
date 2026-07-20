from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from itertools import batched
from pathlib import Path, PurePosixPath
from typing import Iterable

from common.config import (
    config_hash,
    load_pipeline_config,
    resolve_path,
    rule_context_hash,
    source_rule_hash,
    stable_hash,
)
from common.source_text import decode_source_bytes
from db.entities import ExtractionContext, ExtractionResult
from db.writer import delete_file_result, ensure_db_schema, replace_file_result
from extractors.csharp_sql import CSharpSqlExtractor
from extractors.oracle_plsql import OraclePlSqlExtractor
from extractors.oracle_plsql_antlr_calls import OraclePlSqlAntlrCallExtractor
from extractors.oracle_plsql_lineage import OraclePlSqlLineageExtractor
from extractors.xml_sql import XmlSqlExtractor
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
    source_registration_changed,
    stat_matches,
    update_run_current,
    utc_now,
)

HANDLER_VERSION = "4"
HANDLER_VERSIONS = {"xml_sql": "5", "csharp_sql": "5"}


@dataclass(frozen=True)
class ProjectInfo:
    root: Path
    name: str
    project_file: Path

    def state(self, source_root: Path) -> dict[str, str]:
        return {
            "name": self.name,
            "relative_root": self.root.relative_to(source_root).as_posix() or ".",
            "project_file": self.project_file.relative_to(source_root).as_posix(),
        }


def build_plsql_extractors(config: dict | None = None) -> list:
    extractors = [OraclePlSqlExtractor()]
    features = (config or {}).get("features") or {}
    if features.get("antlr_plsql_calls") or (config or {}).get("antlr_plsql_calls"):
        extractors.append(OraclePlSqlAntlrCallExtractor())
    extractors.append(OraclePlSqlLineageExtractor())
    return extractors


def extractor_registry(config: dict) -> dict[str, list]:
    return {
        "oracle_plsql": build_plsql_extractors(config),
        "xml_sql": [XmlSqlExtractor()],
        "csharp_sql": [CSharpSqlExtractor()],
    }


def handler_version(name: str, extractors: list) -> str:
    return stable_hash(
        {
            "contract": HANDLER_VERSIONS.get(name, HANDLER_VERSION),
            "handler": name,
            "extractors": [
                f"{type(item).__module__}.{type(item).__name__}" for item in extractors
            ],
        }
    )


def run_incremental(
    config_path: Path,
    *,
    manifest_path: Path | None = None,
    rebuild: bool = False,
) -> dict:
    config_path = config_path.expanduser().resolve()
    config = load_pipeline_config(config_path)  # Validation must precede DB access.
    db_path = resolve_path(config["db"], config_path.parent)
    state_config = config.get("extractors", {}).get("state") or {}
    manifest = read_manifest(manifest_path) if manifest_path else None
    logger = configure_logging(state_config, config_path.parent)
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
                db_path,
                lease,
                float(state_config.get("heartbeat_seconds", 15)),
            ) as heartbeat:
                failures = process_sources(
                    db,
                    config,
                    config_path.parent,
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
                logger.exception("failed to persist extraction run failure run_id=%s", run_id)
            raise
        finally:
            if lease:
                try:
                    release_lease(db, lease)
                except BaseException:
                    if not failed:
                        raise
                    logger.exception("failed to release extraction lease run_id=%s", run_id)
        return run_summary(db, run_id)


def process_sources(
    db: sqlite3.Connection,
    config: dict,
    base_dir: Path,
    run_id: int,
    lease: Lease,
    manifest: dict[str, str] | None,
    rebuild: bool,
    heartbeat: LeaseHeartbeat,
    logger: logging.Logger,
) -> int:
    extractor_config = config.get("extractors") or {}
    registry = extractor_registry(extractor_config)
    prepared = prepare_sources(
        db, extractor_config.get("sources") or [], base_dir, manifest, registry
    )
    failures = 0
    for source, root, projects, candidates in prepared:
        if manifest is not None and not candidates:
            continue
        db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS seen_files("
            "source_id TEXT,relative_path TEXT,handler_name TEXT,"
            "PRIMARY KEY(source_id,relative_path,handler_name)) WITHOUT ROWID"
        )
        db.execute("DELETE FROM seen_files WHERE source_id=?", (source["id"],))
        db.commit()
        scan_ok = False
        try:
            for candidate_batch in batched(candidates, 1000):
                record_discovered_files(
                    db,
                    lease,
                    run_id,
                    source["id"],
                    [
                        (relative_path, rule["extractor"])
                        for _, _, relative_path, rule in candidate_batch
                    ],
                )
                deferred_counters: dict[str, int] = {}
                try:
                    for action, file_path, relative_path, rule in candidate_batch:
                        handler_name = rule["extractor"]
                        project = project_for(file_path, projects)
                        project_state = (
                            project.state(root)
                            if project
                            else {
                                "name": source["repo"],
                                "relative_root": ".",
                                "project_file": "",
                            }
                        )
                        file_state = get_or_create_file(
                            db,
                            lease=lease,
                            source_id=source["id"],
                            source_root=str(root),
                            relative_path=relative_path,
                            handler_name=handler_name,
                            project_name=project_state["name"],
                            project_relative_root=project_state["relative_root"],
                            project_file=project_state["project_file"],
                        )
                        if action == "D":
                            started, clock = utc_now(), time.perf_counter()
                            delete_file_result(
                                db, lease=lease, file_id=file_state.id, run_id=run_id
                            )
                            record_file_attempt(
                                db,
                                lease=lease,
                                run_id=run_id,
                                file_id=file_state.id,
                                action="deleted",
                                status="success",
                                started_at=started,
                                elapsed_ms=_elapsed_ms(clock),
                            )
                            increment_run(db, lease, run_id, deleted_count=1)
                            continue
                        heartbeat.check()
                        failures += process_file(
                            db,
                            source,
                            root,
                            rule,
                            file_path,
                            relative_path,
                            project_state,
                            file_state,
                            registry[handler_name],
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
            if manifest is None and scan_ok:
                cleanup_missing_files(db, source["id"], run_id, lease, logger)
                register_source(
                    db,
                    lease=lease,
                    source_id=source["id"],
                    kind="source",
                    source_root=str(root),
                    rule_hash=source_rule_hash(source),
                    priority=int(source.get("priority", 0)),
                )
                mark_source_scanned(db, lease, source["id"], run_id)
    return failures


def prepare_sources(
    db: sqlite3.Connection,
    sources: list[dict],
    base_dir: Path,
    manifest: dict[str, str] | None,
    registry: dict[str, list],
) -> list[tuple[dict, Path, list[ProjectInfo], Iterable]]:
    checked: list[tuple[dict, Path]] = []
    for source in sources:
        root = resolve_path(source["path"], base_dir)
        if not root.is_dir():
            raise ValueError(f"Source path is not a directory: {root}")
        for rule in source.get("rules") or []:
            handler_name = rule["extractor"]
            if handler_name not in registry:
                raise ValueError(f"Extractor not available: {handler_name}")
            handler_version(handler_name, registry[handler_name])
        if manifest is not None and source_registration_changed(
            db,
            source_id=source["id"],
            source_root=str(root),
            rule_hash=source_rule_hash(source),
            priority=int(source.get("priority", 0)),
        ):
            raise ValueError(
                f"Source {source['id']} is new or its rules/root changed; full scan required"
            )
        checked.append((source, root))

    source_manifests = (
        normalize_manifest(checked, manifest) if manifest is not None else {}
    )
    prepared = []
    for source, root in checked:
        needs_projects = any(
            rule["extractor"] == "csharp_sql" for rule in source["rules"]
        )
        projects: list[ProjectInfo] = []
        if manifest is None:
            candidates = full_scan_candidates(
                root, source, projects if needs_projects else None
            )
        else:
            candidates = list(
                manifest_candidates(
                    db, root, source, source_manifests[source["id"]]
                )
            )
            if candidates and needs_projects:
                projects = build_project_index(root, source.get("exclude") or [])
        prepared.append((source, root, projects, candidates))
    return prepared


def normalize_manifest(
    sources: list[tuple[dict, Path]], manifest: dict[str, str]
) -> dict[str, dict[str, str]]:
    normalized = {source["id"]: {} for source, _ in sources}
    unmatched = []
    for raw_path, action in manifest.items():
        matches = []
        for source, root in sources:
            relative = manifest_relative_path(raw_path, root, source)
            if relative is not None:
                matches.append((source, relative))
        if len(matches) > 1:
            raise ValueError(f"Manifest path matches multiple sources: {raw_path}")
        if not matches:
            unmatched.append(raw_path)
            continue
        source, relative = matches[0]
        entries = normalized[source["id"]]
        previous = entries.get(relative)
        if previous and previous != action:
            raise ValueError(
                f"Contradictory manifest actions for {source['id']}:{relative}: "
                f"{previous} and {action}"
            )
        entries[relative] = action
    if unmatched:
        raise ValueError(f"Manifest paths match no configured source: {unmatched}")
    return normalized


def process_file(
    db: sqlite3.Connection,
    source: dict,
    source_root: Path,
    rule: dict,
    file_path: Path,
    relative_path: str,
    project: dict[str, str],
    file_state,
    extractors: list,
    run_id: int,
    lease: Lease,
    rebuild: bool,
    logger: logging.Logger,
    deferred_counters: dict[str, int] | None = None,
) -> int:
    started, clock = utc_now(), time.perf_counter()
    context_hash = rule_context_hash(source, rule, project)
    version = handler_version(rule["extractor"], extractors)
    try:
        stat = file_path.stat()
    except ExtractionLeaseLost:
        raise
    except Exception as exc:
        return record_extraction_failure(
            db, source, project, relative_path, file_state.id, run_id, lease,
            started, clock, exc, logger
        )
    if not rebuild and stat_matches(
        file_state,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
        ctime_ns=stat.st_ctime_ns,
        context_hash=context_hash,
        handler_version=version,
    ):
        if deferred_counters is None:
            increment_run(db, lease, run_id, skipped_count=1)
        else:
            deferred_counters["skipped_count"] = (
                deferred_counters.get("skipped_count", 0) + 1
            )
        logger.debug(
            "file skipped source=%s project=%s path=%s handler=%s reason=stat_unchanged",
            source["id"],
            project["name"],
            relative_path,
            rule["extractor"],
        )
        return 0
    update_run_current(
        db, lease, run_id, source["id"], project["name"], relative_path
    )
    try:
        data, stat = read_stable_bytes(file_path)
        digest = hashlib.sha256(data).hexdigest()
    except ExtractionLeaseLost:
        raise
    except Exception as exc:
        return record_extraction_failure(
            db, source, project, relative_path, file_state.id, run_id, lease,
            started, clock, exc, logger
        )
    if (
        not rebuild
        and file_state.status == "success"
        and file_state.successful_sha256 == digest
        and file_state.successful_rule_context_hash == context_hash
        and file_state.successful_handler_version == version
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
        text = decode_source_bytes(data)
        context = ExtractionContext(
            repository=source["repo"],
            repository_path=str(source_root),
            db_name=source["db_name"],
            schema_name=source["schema"],
            source_id=source["id"],
            relative_source_path=relative_path,
            project_name=project["name"],
            project_relative_root=project["relative_root"],
            project_file=project["project_file"],
            owner_policy=rule["owner"],
            extra_tags={"schema": source["schema"]},
        )
        result = extract_file(file_path, relative_path, text, context, extractors)
        replace_file_result(
            db,
            lease=lease,
            file_id=file_state.id,
            run_id=run_id,
            result=result,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=digest,
            rule_context_hash=context_hash,
            handler_version=version,
            device=stat.st_dev,
            inode=stat.st_ino,
            ctime_ns=stat.st_ctime_ns,
            priority=int(source.get("priority", 0)),
        )
    except ExtractionLeaseLost:
        raise
    except Exception as exc:
        return record_extraction_failure(
            db, source, project, relative_path, file_state.id, run_id, lease,
            started, clock, exc, logger
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
        node_count=len(result.nodes),
        edge_count=len(result.edges),
    )
    increment_run(
        db,
        lease,
        run_id,
        extracted_count=1,
        node_count=len(result.nodes),
        edge_count=len(result.edges),
    )
    logger.info(
        "file extracted source=%s project=%s path=%s handler=%s "
        "elapsed_ms=%d nodes=%d edges=%d",
        source["id"],
        project["name"],
        relative_path,
        rule["extractor"],
        elapsed,
        len(result.nodes),
        len(result.edges),
    )
    return 0


def record_extraction_failure(
    db: sqlite3.Connection,
    source: dict,
    project: dict[str, str],
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
        "file failed source=%s project=%s path=%s error=%s",
        source["id"],
        project["name"],
        relative_path,
        error,
    )
    return 1


def extract_file(
    file_path: Path,
    relative_path: str,
    text: str,
    context: ExtractionContext,
    extractors: Iterable,
) -> ExtractionResult:
    combined = ExtractionResult(source_file=relative_path)
    matched = False
    for extractor in extractors:
        if not extractor.can_handle(str(file_path), text):
            continue
        matched = True
        result = extractor.extract(str(file_path), text, context)
        inject_metadata(
            result, relative_path, type(extractor).__name__, context.source_id
        )
        combined = combined.merge(result)
    if not matched:
        raise ValueError(f"Configured handler rejected file: {relative_path}")
    combined.source_file = relative_path
    return combined


def inject_metadata(
    result: ExtractionResult,
    relative_path: str,
    extractor: str,
    source_id: str,
) -> None:
    for item in (*result.nodes, *result.edges):
        item.properties["source_file"] = relative_path
        item.properties["source_path"] = relative_path
        item.properties["source_id"] = source_id
        item.properties.setdefault("extractor_name", extractor)


def full_scan_candidates(
    root: Path, source: dict, projects: list[ProjectInfo] | None = None
):
    for file_path in walk_source_files(root, root, source.get("exclude") or []):
        if projects is not None and file_path.suffix.lower() == ".csproj":
            projects.append(
                ProjectInfo(file_path.parent, read_project_name(file_path), file_path)
            )
            projects.sort(key=lambda item: len(item.root.parts), reverse=True)
        relative = file_path.relative_to(root).as_posix()
        rule = matching_rule(relative, source)
        if rule:
            yield "M", file_path, relative, rule


def manifest_candidates(
    db: sqlite3.Connection,
    root: Path,
    source: dict,
    manifest: dict[str, str],
):
    expanded = dict(manifest)
    expand_project_changes(db, root, source, expanded)
    for relative, action in sorted(expanded.items()):
        rule = matching_rule(relative, source)
        if not rule:
            continue
        file_path = root / relative
        if action != "D" and not file_path.is_file():
            raise ValueError(f"Manifest {action} path does not exist: {relative}")
        yield action, file_path, relative, rule


def expand_project_changes(
    db: sqlite3.Connection,
    root: Path,
    source: dict,
    manifest: dict[str, str],
) -> None:
    project_changes = [
        (relative, action)
        for relative, action in manifest.items()
        if relative.lower().endswith(".csproj")
    ]
    for project_file, action in project_changes:
        tracked = db.execute(
            """
            SELECT relative_path FROM extraction_files
            WHERE source_id=? AND project_file=? AND status<>'deleted'
            """,
            (source["id"], project_file),
        ).fetchall()
        for (relative_path,) in tracked:
            manifest.setdefault(relative_path, "M")
        if action == "D":
            continue
        project_root = (root / project_file).parent
        for file_path in walk_source_files(
            root, project_root, source.get("exclude") or []
        ):
            relative_path = file_path.relative_to(root).as_posix()
            if matching_rule(relative_path, source):
                manifest.setdefault(relative_path, "M")


def matching_rule(relative_path: str, source: dict) -> dict | None:
    if matches_any(relative_path, source.get("exclude") or []):
        return None
    for rule in source.get("rules") or []:
        if matches_any(relative_path, rule["patterns"]):
            return rule
    return None


def matches_any(relative_path: str, patterns: Iterable[str]) -> bool:
    path = PurePosixPath(relative_path)
    return any(
        path.match(pattern)
        or fnmatch.fnmatchcase(relative_path, pattern)
        or (
            pattern.startswith("**/")
            and fnmatch.fnmatchcase(relative_path, pattern[3:])
        )
        for pattern in patterns
    )


def walk_source_files(
    source_root: Path, start_root: Path, excludes: list[str]
) -> Iterable[Path]:
    for directory, dirnames, filenames in os.walk(
        start_root, onerror=_raise_scan_error
    ):
        directory_path = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not matches_any(
                (directory_path / name)
                .relative_to(source_root)
                .as_posix()
                + "/__entry__",
                excludes,
            )
        )
        for name in sorted(
            filenames, key=lambda item: (not item.lower().endswith(".csproj"), item)
        ):
            file_path = directory_path / name
            relative = file_path.relative_to(source_root).as_posix()
            if not matches_any(relative, excludes):
                yield file_path


def build_project_index(root: Path, excludes: list[str]) -> list[ProjectInfo]:
    projects = [
        ProjectInfo(project_file.parent, read_project_name(project_file), project_file)
        for project_file in walk_source_files(root, root, excludes)
        if project_file.suffix.lower() == ".csproj"
    ]
    return sorted(projects, key=lambda item: len(item.root.parts), reverse=True)


def read_project_name(project_file: Path) -> str:
    try:
        root = ET.fromstring(project_file.read_bytes())
    except ET.ParseError as exc:
        raise ValueError(f"Invalid csproj XML: {project_file}") from exc
    for element in root.iter():
        if (
            element.tag.rsplit("}", 1)[-1] == "AssemblyName"
            and element.text
            and element.text.strip()
        ):
            return element.text.strip()
    return project_file.stem


def project_for(
    file_path: Path, projects: list[ProjectInfo]
) -> ProjectInfo | None:
    for project in projects:
        if file_path == project.root or project.root in file_path.parents:
            return project
    return None


def read_stable_bytes(file_path: Path) -> tuple[bytes, os.stat_result]:
    for _ in range(2):
        with file_path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read()
            after = os.fstat(stream.fileno())
        path_after = file_path.stat()
        descriptor_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            descriptor_identity
            == (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            == (
                path_after.st_dev,
                path_after.st_ino,
                path_after.st_size,
                path_after.st_mtime_ns,
                path_after.st_ctime_ns,
            )
            and after.st_size == len(data)
        ):
            return data, after
    raise RuntimeError(f"File changed while reading: {file_path}")


def cleanup_missing_files(
    db: sqlite3.Connection,
    source_id: str,
    run_id: int,
    lease: Lease,
    logger: logging.Logger,
) -> None:
    rows = db.execute(
        """
        SELECT f.id,f.relative_path,f.handler_name
        FROM extraction_files f
        LEFT JOIN seen_files exact ON exact.source_id=f.source_id
          AND exact.relative_path=f.relative_path
          AND exact.handler_name=f.handler_name
        WHERE f.source_id=? AND f.status<>'deleted' AND exact.source_id IS NULL
          AND (
            NOT EXISTS (
              SELECT 1 FROM seen_files current
              WHERE current.source_id=f.source_id
                AND current.relative_path=f.relative_path
            )
            OR EXISTS (
              SELECT 1 FROM seen_files current
              JOIN extraction_files replacement
                ON replacement.source_id=current.source_id
                AND replacement.relative_path=current.relative_path
                AND replacement.handler_name=current.handler_name
              WHERE current.source_id=f.source_id
                AND current.relative_path=f.relative_path
                AND replacement.status='success'
            )
          )
        """,
        (source_id,),
    ).fetchall()
    for file_id, relative_path, handler_name in rows:
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
            handler_name,
        )


def read_manifest(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in {"A", "M", "D"}:
            raise ValueError(f"Invalid manifest line {number}: {raw}")
        path = Path(parts[1]).as_posix()
        previous = entries.get(path)
        if previous and previous != parts[0]:
            raise ValueError(
                f"Contradictory manifest actions for {path}: {previous} and {parts[0]}"
            )
        entries[path] = parts[0]
    return entries


def manifest_relative_path(
    raw_path: str, root: Path, source: dict
) -> str | None:
    path = Path(raw_path).expanduser()
    normalized = path.as_posix()
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"Manifest path traversal is not allowed: {raw_path}")
    resolved_root = root.resolve()
    if path.is_absolute():
        try:
            return path.resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            return None
    configured_path = Path(source["path"])
    if configured_path.is_absolute():
        relative = normalized
    else:
        configured = Path(os.path.normpath(configured_path.as_posix())).as_posix()
        if configured in {"", "."}:
            relative = normalized
        elif normalized.startswith(configured + "/"):
            relative = normalized[len(configured) + 1 :]
        else:
            return None
    if relative in {"", "."}:
        return None
    try:
        (resolved_root / relative).resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Manifest path escapes source root: {raw_path}") from exc
    return relative


def _raise_scan_error(error: OSError) -> None:
    raise error


def _elapsed_ms(started_clock: float) -> int:
    return int((time.perf_counter() - started_clock) * 1000)
