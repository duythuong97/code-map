"""Which revision (git commit or svn revision) each source was extracted from, so a reader knows how old the graph is.

``source_revisions`` is stored in ``graph.sqlite`` metadata, ``manifest.json``
(``revisions``) and the meta table of ``codemap.sqlite``. ``stale.py`` compares it (and the per-file hashes in
the ``sources`` table) with the working tree.
"""
from __future__ import annotations

import os
from pathlib import Path

from .vcs import repository_at


def source_directory(root: Path, source: dict) -> Path:
    """The first configured folder of a source (a .csproj/.sln/.cs entry counts as its folder)."""
    folders = source.get("folders") or ["."]
    first = folders[0]
    relative = first.get("path", ".") if isinstance(first, dict) else str(first)
    path = (root / relative).resolve()
    return path if path.is_dir() or not path.suffix else path.parent


def source_revisions(root: Path, sources: list[dict]) -> dict[str, dict[str, object]]:
    """{source: {vcs, repository_root, commit, branch, dirty, dirty_files}}; ``commit`` is the
    git hash or the svn revision number, ``vcs`` is ``none`` outside version control."""
    result: dict[str, dict[str, object]] = {}
    repositories: dict[str, dict[str, object]] = {}
    for source in sources:
        directory = source_directory(root, source)
        repository = repository_at(directory)
        if repository is None:
            result[str(source["name"])] = {"vcs": "none"}
            continue
        recorded = repositories.get(str(repository.root))
        if recorded is None:
            recorded = {
                "vcs": repository.name,
                "repository_root": _display_path(repository.root, root),
                "commit": repository.revision(),
                "branch": repository.branch(),
            }
            repositories[str(repository.root)] = recorded
        pathspecs = [_relative_pathspec(repository.root, folder, root) for folder in source.get("folders") or ["."]]
        dirty = repository.dirty_files(pathspecs)
        result[str(source["name"])] = {**recorded, "dirty": bool(dirty), "dirty_files": dirty}
    return result


def source_scopes(sources: list[dict]) -> dict[str, dict[str, object]]:
    """What each source scanned, so a stale check can also find files added since."""
    return {
        str(source["name"]): {"type": str(source["type"]), "folders": list(source.get("folders") or [])}
        for source in sources
    }


def _relative_pathspec(top: Path, folder: object, root: Path) -> str:
    relative = folder.get("path", ".") if isinstance(folder, dict) else str(folder)
    path = (root / relative).resolve()
    try:
        return path.relative_to(top).as_posix() or "."
    except ValueError:
        return "."


def _display_path(path: Path, root: Path) -> str:
    """Relative to root (``..`` for a repository that contains root), so the
    record stays valid for a worktree checkout and on another machine."""
    try:
        return Path(os.path.relpath(path, root.resolve())).as_posix()
    except ValueError:  # another drive on Windows
        return str(path)
