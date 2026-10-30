"""Is the graph still true for the working tree?

Compares what an extract recorded (``sources`` table: path + sha256 of every
scanned file; ``source_revisions``: the commit of each source) with the files
and commits on disk now. The answer lists changed, deleted and new files, the
commits made since, and the graph nodes declared in changed files, so a reader
(or an AI agent) knows which facts to double-check against the code.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .vcs import recorded_repository

MAX_LISTED = 200
# A file last written this long before the extract finished is taken as the one the extract read and is not
# hashed again; an edit, checkout or update after that gives it a newer time. Reading every file of a large
# source on each check is what made it slow.
UNTOUCHED_MARGIN_SECONDS = 24 * 3600


def stale_report(graph_path: Path, source_root: Path | None = None, *, check_added: bool = True) -> dict[str, object]:
    with closing(sqlite3.connect(f"{graph_path.resolve().as_uri()}?mode=ro", uri=True)) as graph:
        metadata = {key: _json(value) for key, value in graph.execute("SELECT key, value_json FROM metadata")}
        root = source_root or (Path(str(metadata["source_root"])) if metadata.get("source_root") else None)
        root = root.resolve() if root is not None else None
        recorded = {
            path: (source, digest)
            for source, path, digest in graph.execute("SELECT source_key, relative_path, raw_sha256 FROM sources")
        }
        skipped = {
            str(path)
            for (path,) in graph.execute("SELECT DISTINCT source_path FROM issues WHERE source_path IS NOT NULL")
        }
        result: dict[str, object] = {
            "generated_at": metadata.get("generated_at", ""),
            "source_root": str(root or ""),
            "files_recorded": len(recorded),
        }
        if root is None or not root.is_dir():
            result.update(
                {
                    "fresh": None,
                    "reason": "source root not found; pass --source-root",
                    "changed": [],
                    "deleted": [],
                    "added": [],
                    "revisions": [],
                }
            )
            return result
        changed: list[str] = []
        deleted: list[str] = []
        untouched_before = _epoch(metadata.get("generated_at")) - UNTOUCHED_MARGIN_SECONDS
        for path, (_, digest) in sorted(recorded.items()):
            file = root / path
            try:
                modified = file.stat().st_mtime
            except OSError:
                deleted.append(path)
                continue
            if not file.is_file():
                deleted.append(path)
            elif modified >= untouched_before and hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                changed.append(path)
        added = (
            _added_files(root, metadata.get("source_scopes") or {}, set(recorded) | skipped)
            if check_added
            else []
        )
        revisions = _revision_changes(root, metadata.get("source_revisions") or {})
        affected = _nodes_in_files(graph, changed + deleted)
    moved = [item for item in revisions if item.get("commits_since")]
    result.update(
        {
            "fresh": not (changed or deleted or added),
            "changed": changed[:MAX_LISTED],
            "deleted": deleted[:MAX_LISTED],
            "added": added[:MAX_LISTED],
            "counts": {"changed": len(changed), "deleted": len(deleted), "added": len(added)},
            "revisions": revisions,
            "sources_with_new_commits": [item["source"] for item in moved],
            "affected_nodes": affected,
        }
    )
    return result


def summary_line(report: dict[str, object]) -> str:
    """One line for the top of an AI answer: when and from what the graph was built, and how stale it is."""
    revisions = report.get("revisions") or []
    commits = sorted({str(item.get("commit", ""))[:8] for item in revisions if item.get("commit")})
    built = f"KB built {report.get('generated_at', '?')}" + (f" @ {', '.join(commits)}" if commits else "")
    if report.get("fresh") is None:
        return f"{built} · freshness unknown ({report.get('reason', '')})"
    counts = report.get("counts") or {}
    if report.get("fresh"):
        state = "up to date with the working tree"
    else:
        state = (
            f"STALE: {counts.get('changed', 0)} changed, {counts.get('deleted', 0)} deleted, "
            f"{counts.get('added', 0)} new file(s) since extract"
        )
    moved = report.get("sources_with_new_commits") or []
    if moved:
        state += f"; new commits in {', '.join(moved)}"
    return f"{built} · {state}"


def _added_files(root: Path, scopes: dict, known: set[str]) -> list[str]:
    from .extractors import extractor_spec
    from .pipeline import _selected_files

    added: set[str] = set()
    for scope in scopes.values():
        try:
            spec = extractor_spec(str(scope.get("type")))
        except ValueError:
            continue
        try:
            files = _selected_files(root, list(scope.get("folders") or []), spec.suffixes)
        except (OSError, ValueError):
            continue
        for path in files:
            relative = path.relative_to(root).as_posix()
            if relative not in known:
                added.add(relative)
    return sorted(added)


def _revision_changes(root: Path, recorded: dict) -> list[dict[str, object]]:
    result = []
    for source, info in sorted(recorded.items()):
        repository = recorded_repository(info, root) if isinstance(info, dict) else None
        if repository is None:
            result.append({"source": source, "vcs": "none"})
            continue
        commit = str(info.get("commit") or "")
        head = repository.revision()
        since = repository.revisions_since(commit) if commit and head and commit != head else None
        result.append(
            {
                "source": source,
                "vcs": repository.name,
                "commit": commit,
                "head": head,
                "branch": info.get("branch", ""),
                "dirty_at_extract": bool(info.get("dirty")),
                "commits_since": since if since is not None else (0 if commit == head else None),
            }
        )
    return result


def _nodes_in_files(graph: sqlite3.Connection, paths: list[str]) -> list[dict[str, str]]:
    if not paths:
        return []
    rows: list[dict[str, str]] = []
    for start in range(0, len(paths), 400):
        chunk = paths[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for stable_id, node_type, name, path in graph.execute(
            f"""SELECT DISTINCT n.stable_id, n.node_type, n.technical_name, e.source_path FROM evidence e
                JOIN nodes n ON n.node_id = e.target_id
                WHERE e.target_type = 'NODE' AND e.evidence_kind <> 'CATALOG_ROW' AND e.source_path IN ({marks})""",
            chunk,
        ):
            rows.append({"stable_id": stable_id, "type": node_type, "name": name or "", "file": path})
    rows.sort(key=lambda row: (row["file"], row["type"], row["name"]))
    return rows[:MAX_LISTED]


def _epoch(value: object) -> float:
    """Seconds since the epoch of an ISO time ("…Z"); 0 when unknown, so every file is hashed."""
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _json(value: str | None) -> object:
    try:
        return json.loads(value) if value is not None else None
    except ValueError:
        return value
