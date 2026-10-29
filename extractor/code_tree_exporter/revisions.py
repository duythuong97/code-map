"""Which commit each source was extracted from, so a reader knows how old the graph is.

``source_revisions`` is stored in ``graph.sqlite`` metadata, ``manifest.json``
(``revisions``) and the meta table of ``codemap-rag.sqlite`` /
``codemap-web.sqlite``. ``stale.py`` compares it (and the per-file hashes in
the ``sources`` table) with the working tree.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def git(directory: Path, *arguments: str, timeout: float = 30) -> str | None:
    """Output of a git command in ``directory``, or None when git or the repository is missing."""
    executable = shutil.which("git")
    if not executable or not directory.exists():
        return None
    try:
        completed = subprocess.run(
            [executable, "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def source_directory(root: Path, source: dict) -> Path:
    """The first configured folder of a source (a .csproj/.sln/.cs entry counts as its folder)."""
    folders = source.get("folders") or ["."]
    first = folders[0]
    relative = first.get("path", ".") if isinstance(first, dict) else str(first)
    path = (root / relative).resolve()
    return path if path.is_dir() or not path.suffix else path.parent


def source_revisions(root: Path, sources: list[dict]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    repositories: dict[str, dict[str, object]] = {}
    for source in sources:
        directory = source_directory(root, source)
        top = git(directory, "rev-parse", "--show-toplevel")
        if not top:
            result[str(source["name"])] = {"vcs": "none"}
            continue
        top_path = Path(top).resolve()
        repository = repositories.get(str(top_path))
        if repository is None:
            repository = {
                "vcs": "git",
                "repository_root": _display_path(top_path, root),
                "commit": git(top_path, "rev-parse", "HEAD") or "",
                "branch": git(top_path, "rev-parse", "--abbrev-ref", "HEAD") or "",
            }
            repositories[str(top_path)] = repository
        pathspecs = [_relative_pathspec(top_path, folder, root) for folder in source.get("folders") or ["."]]
        status = git(top_path, "status", "--porcelain", "--untracked-files=no", "--", *pathspecs) or ""
        dirty = [line for line in status.splitlines() if line.strip()]
        result[str(source["name"])] = {**repository, "dirty": bool(dirty), "dirty_files": len(dirty)}
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
