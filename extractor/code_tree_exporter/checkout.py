"""``extract --at``: build the graph of an older commit without touching the working tree.

Each repository that holds a configured source is checked out into a
temporary folder laid out like ``root`` (git: ``git worktree add --detach``;
Subversion: ``svn export`` of the working copy's URL at that revision), so the
config's relative folders still resolve. The revision is one of:

* ``--at REV``                 the same revision in every repository (monorepo, tag, ``main``)
* ``--at source=REV``          per source name (repeatable); other sources use the working tree's HEAD
* ``--at-date 2026-01-31``     per repository, the last commit (svn: revision) before that date

Sources outside version control are copied as they are. This is what the graph diff of a
pull request (base vs head) and the backtests use.
"""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .revisions import source_directory
from .vcs import Repository, repository_at


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
    repositories: dict[Path, tuple[Repository, str]] = {}
    plain: list[Path] = []
    for source in sources:
        directory = source_directory(root, source)
        repository = repository_at(directory)
        if repository is None:
            plain.append(directory)
            continue
        revision = per_source.get(str(source["name"])) or default
        if before_date and not revision:
            revision = repository.revision_before(before_date)
            if not revision:
                raise CheckoutError(f"{repository.root}: no revision before {before_date}")
        revision = revision or "HEAD"
        previous = repositories.get(repository.root)
        if previous and previous[1] != revision:
            raise CheckoutError(
                f"sources in {repository.root} asked for different revisions ({previous[1]}, {revision})"
            )
        repositories[repository.root] = (repository, revision)

    temporary = Path(tempfile.mkdtemp(prefix="code-tree-at-"))
    added: list[tuple[Repository, Path]] = []
    try:
        # A repository that contains root (monorepo) maps root to a folder inside its checkout.
        new_root = temporary / "root"
        for top, (repository, revision) in sorted(repositories.items(), key=lambda item: len(item[0].parts)):
            exact = repository.resolve(revision)
            if not exact:
                raise CheckoutError(f"{top}: unknown revision {revision!r}")
            if top == root or top in root.parents:
                target = temporary / "repo"
                new_root = target.joinpath(*root.relative_to(top).parts)
            else:
                target = new_root.joinpath(*top.relative_to(root).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                repository.checkout(exact, target)
            except RuntimeError as exc:
                raise CheckoutError(str(exc)) from exc
            added.append((repository, target))
        for directory in plain:
            target = new_root.joinpath(*directory.relative_to(root).parts)
            if not target.exists():
                shutil.copytree(directory, target)
        new_root.mkdir(parents=True, exist_ok=True)
        yield new_root
    finally:
        for repository, target in reversed(added):
            repository.release(target)
        shutil.rmtree(temporary, ignore_errors=True)
