"""``extract --at``: build the graph of an older commit without touching the working tree.

Each git repository that holds a configured source is checked out with
``git worktree add --detach`` into a temporary folder laid out like ``root``,
so the config's relative folders still resolve. The revision is one of:

* ``--at REV``                 the same revision in every repository (monorepo, tag, ``main``)
* ``--at source=REV``          per source name (repeatable); other sources use the working tree's HEAD
* ``--at-date 2026-01-31``     per repository, the last commit on HEAD before that date

Sources outside git are copied as they are. This is what the graph diff of a
pull request (base vs head) and the backtests use.
"""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .revisions import git, source_directory


class CheckoutError(ValueError):
    pass


def parse_at(values: list[str] | None) -> tuple[str | None, dict[str, str]]:
    """``["main"]`` -> ("main", {}); ``["order-api=abc", "web=v2"]`` -> (None, {...})."""
    default: str | None = None
    per_source: dict[str, str] = {}
    for value in values or []:
        if "=" in value:
            name, revision = value.split("=", 1)
            if not name or not revision:
                raise CheckoutError(f"--at expects REV or SOURCE=REV, got {value!r}")
            per_source[name.strip()] = revision.strip()
        elif default is None:
            default = value.strip()
        else:
            raise CheckoutError("--at REV can be given once; use SOURCE=REV for the others")
    return default, per_source


@contextmanager
def checkout_at(
    root: Path,
    sources: list[dict],
    *,
    default: str | None = None,
    per_source: dict[str, str] | None = None,
    before_date: str | None = None,
) -> Iterator[Path]:
    """Yield a temporary root with every source at the requested revision."""
    per_source = dict(per_source or {})
    unknown = sorted(set(per_source) - {str(source["name"]) for source in sources})
    if unknown:
        raise CheckoutError(f"--at names unknown source(s): {', '.join(unknown)}")
    root = root.resolve()
    repositories: dict[Path, str] = {}
    plain: list[Path] = []
    for source in sources:
        directory = source_directory(root, source)
        top = git(directory, "rev-parse", "--show-toplevel")
        if not top:
            plain.append(directory)
            continue
        top_path = Path(top).resolve()
        revision = per_source.get(str(source["name"])) or default
        if before_date and not revision:
            revision = git(top_path, "rev-list", "-1", f"--before={before_date}", "HEAD")
            if not revision:
                raise CheckoutError(f"{top_path}: no commit before {before_date}")
        revision = revision or "HEAD"
        previous = repositories.get(top_path)
        if previous and previous != revision:
            raise CheckoutError(
                f"sources in {top_path} asked for different revisions ({previous}, {revision})"
            )
        repositories[top_path] = revision

    temporary = Path(tempfile.mkdtemp(prefix="code-tree-at-"))
    added: list[tuple[Path, Path]] = []
    try:
        # A repository that contains root (monorepo) maps root to a folder inside its worktree.
        new_root = temporary / "root"
        for top, revision in sorted(repositories.items(), key=lambda item: len(item[0].parts)):
            commit = git(top, "rev-parse", "--verify", f"{revision}^{{commit}}")
            if not commit:
                raise CheckoutError(f"{top}: unknown revision {revision!r}")
            if top == root or top in root.parents:
                target = temporary / "repo"
                new_root = target.joinpath(*root.relative_to(top).parts)
            else:
                target = new_root.joinpath(*top.relative_to(root).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if git(top, "worktree", "add", "--detach", "--force", str(target), commit, timeout=600) is None:
                raise CheckoutError(f"git worktree add failed for {top} at {revision}")
            added.append((top, target))
        for directory in plain:
            target = new_root.joinpath(*directory.relative_to(root).parts)
            if not target.exists():
                shutil.copytree(directory, target)
        new_root.mkdir(parents=True, exist_ok=True)
        yield new_root
    finally:
        for top, target in reversed(added):
            git(top, "worktree", "remove", "--force", str(target), timeout=600)
            git(top, "worktree", "prune")
        shutil.rmtree(temporary, ignore_errors=True)
