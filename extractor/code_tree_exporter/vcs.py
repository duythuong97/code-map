"""Version control behind one interface: git, Subversion, or nothing.

What the extract records (``revisions.py``), how stale a graph is
(``stale.py``) and ``extract --at`` (``checkout.py``) only talk to a
``Repository``. A source outside version control still works everywhere: the
stale check compares file hashes, and ``--at`` copies the folder as it is.

Subversion needs the command-line client (``svn`` on PATH; on Windows the
TortoiseSVN "command line client tools" option or SlikSVN). History questions
(revisions since, ``--at`` an older revision) ask the server; when it cannot
be reached they answer "unknown" instead of failing the extract.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_SVN_KEYWORDS = frozenset({"HEAD", "BASE", "COMMITTED", "PREV"})


def run(executable: str, arguments: list[str], *, cwd: Path | None = None, timeout: float = 30) -> str | None:
    """stdout of a command, or None when the tool is missing or the command fails."""
    path = shutil.which(executable)
    if not path:
        return None
    try:
        completed = subprocess.run(
            [path, *arguments],
            cwd=str(cwd) if cwd else None,
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


def git(directory: Path, *arguments: str, timeout: float = 30) -> str | None:
    """Output of a git command in ``directory``, or None when git or the repository is missing."""
    if not directory.exists():
        return None
    return run("git", ["-C", str(directory), *arguments], timeout=timeout)


class Repository:
    """A working copy. ``revision`` is a commit hash (git) or a revision number (svn)."""

    name = "none"

    def __init__(self, root: Path) -> None:
        self.root = root

    def revision(self) -> str:
        raise NotImplementedError

    def branch(self) -> str:
        raise NotImplementedError

    def dirty_files(self, paths: list[str]) -> int:
        """Modified versioned files under ``paths`` (relative to ``root``)."""
        raise NotImplementedError

    def revisions_since(self, revision: str) -> int | None:
        """Commits between ``revision`` and the working copy, or None when unknown."""
        raise NotImplementedError

    def resolve(self, revision: str) -> str | None:
        """A branch, tag, date or keyword as the exact revision, or None when unknown."""
        raise NotImplementedError

    def revision_before(self, date: str) -> str | None:
        raise NotImplementedError

    def checkout(self, revision: str, target: Path) -> None:
        """Put the tree at ``revision`` in ``target`` (which does not exist yet)."""
        raise NotImplementedError

    def release(self, target: Path) -> None:
        """Undo ``checkout`` bookkeeping; ``target`` itself is removed by the caller."""


class GitRepository(Repository):
    name = "git"

    @classmethod
    def find(cls, directory: Path) -> GitRepository | None:
        top = git(directory, "rev-parse", "--show-toplevel")
        return cls(Path(top).resolve()) if top else None

    def revision(self) -> str:
        return git(self.root, "rev-parse", "HEAD") or ""

    def branch(self) -> str:
        return git(self.root, "rev-parse", "--abbrev-ref", "HEAD") or ""

    def dirty_files(self, paths: list[str]) -> int:
        status = git(self.root, "status", "--porcelain", "--untracked-files=no", "--", *(paths or ["."])) or ""
        return len([line for line in status.splitlines() if line.strip()])

    def revisions_since(self, revision: str) -> int | None:
        count = git(self.root, "rev-list", "--count", f"{revision}..HEAD")
        return int(count) if count and count.isdigit() else None

    def resolve(self, revision: str) -> str | None:
        return git(self.root, "rev-parse", "--verify", f"{revision}^{{commit}}")

    def revision_before(self, date: str) -> str | None:
        return git(self.root, "rev-list", "-1", f"--before={date}", "HEAD")

    def checkout(self, revision: str, target: Path) -> None:
        if git(self.root, "worktree", "add", "--detach", "--force", str(target), revision, timeout=600) is None:
            raise RuntimeError(f"git worktree add failed for {self.root} at {revision}")

    def release(self, target: Path) -> None:
        git(self.root, "worktree", "remove", "--force", str(target), timeout=600)
        git(self.root, "worktree", "prune")


class SvnRepository(Repository):
    name = "svn"

    @classmethod
    def find(cls, directory: Path) -> SvnRepository | None:
        if not directory.exists():
            return None
        top = run("svn", ["info", "--show-item", "wc-root", "--no-newline", str(directory)])
        return cls(Path(top).resolve()) if top else None

    def _info(self, item: str, revision: str | None = None, target: str | None = None) -> str | None:
        arguments = ["info", "--show-item", item, "--no-newline"]
        if revision:
            arguments += ["-r", revision]
        return run("svn", [*arguments, target or str(self.root)], timeout=60)

    def revision(self) -> str:
        return self._info("revision") or ""

    def branch(self) -> str:
        # "^/trunk", "^/branches/release-2": where in the repository this working copy is.
        return self._info("relative-url") or ""

    def dirty_files(self, paths: list[str]) -> int:
        # -q leaves out unversioned files, like git's --untracked-files=no.
        status = run("svn", ["status", "-q", *(paths or ["."])], cwd=self.root) or ""
        return len([line for line in status.splitlines() if line[:1] in {"M", "A", "D", "R", "C", "!", "~"}])

    def revisions_since(self, revision: str) -> int | None:
        if not revision.isdigit():
            return None
        current = self.revision()
        if current == revision:
            return 0
        log = run("svn", ["log", "-q", "-r", f"{int(revision) + 1}:BASE", str(self.root)], timeout=60)
        if log is None:
            return None
        return len([line for line in log.splitlines() if line.startswith("r")])

    def resolve(self, revision: str) -> str | None:
        value = revision.strip()
        if not (value.isdigit() or value.upper() in _SVN_KEYWORDS or value.startswith("{")):
            return None
        url = self._info("url")
        if not url:
            return None
        # last-changed-revision of the folder at that point: what an export there would contain.
        return self._info("last-changed-revision", value, f"{url}@{value}" if value.isdigit() else url)

    def revision_before(self, date: str) -> str | None:
        return self.resolve("{" + date + "}")

    def checkout(self, revision: str, target: Path) -> None:
        url = self._info("url")
        if not url or run("svn", ["export", "-q", "--force", "-r", revision, f"{url}@{revision}", str(target)], timeout=1800) is None:
            raise RuntimeError(f"svn export failed for {self.root} at r{revision}")


_KINDS: tuple[type[Repository], ...] = (GitRepository, SvnRepository)


def repository_at(directory: Path) -> Repository | None:
    """The working copy ``directory`` belongs to (git first: a git-svn clone is a git repository)."""
    for kind in _KINDS:
        found = kind.find(directory)  # type: ignore[attr-defined]
        if found is not None:
            return found
    return None


def recorded_repository(info: dict, root: Path) -> Repository | None:
    """The repository a ``source_revisions`` entry was recorded from, as it is now on disk."""
    kind = next((kind for kind in _KINDS if kind.name == info.get("vcs")), None)
    if kind is None:
        return None
    path = Path(str(info.get("repository_root") or "."))
    return kind((path if path.is_absolute() else root / path).resolve())
