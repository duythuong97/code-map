"""Subversion working copies are recorded and checked like git ones (with a stand-in svn client)."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from code_tree_exporter.revisions import source_revisions
from code_tree_exporter.stale import _revision_changes
from code_tree_exporter.vcs import SvnRepository, repository_at

# Answers the few svn commands vcs.py runs, for a working copy at $FAKE_SVN_ROOT checked out at r42.
FAKE_SVN = textwrap.dedent(
    """\
    #!{python}
    import os, sys
    root = os.environ["FAKE_SVN_ROOT"]
    args = sys.argv[1:]
    if args[:1] == ["info"]:
        item = args[args.index("--show-item") + 1]
        target = args[-1]
        if "-r" in args:
            print({{"{{2026-01-31}}": "40", "HEAD": "45"}}.get(args[args.index("-r") + 1], "41"), end="")
        elif not os.path.realpath(target).startswith(os.path.realpath(root)) and "://" not in target:
            sys.exit(1)
        else:
            print({{"wc-root": root, "revision": "42", "relative-url": "^/trunk",
                   "url": "https://svn.example/repo/trunk"}}[item], end="")
    elif args[:2] == ["status", "-q"]:
        print("M       ops/job.sql")
    elif args[:2] == ["log", "-q"]:
        print("-" * 20 + "\\nr41 | dev | 2026-02-01\\n" + "-" * 20 + "\\nr42 | dev | 2026-02-02\\n" + "-" * 20)
    else:
        sys.exit(1)
    """
)


@unittest.skipIf(os.name == "nt", "the stand-in svn client is a POSIX script")
class SubversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "wc"
        (self.root / "ops").mkdir(parents=True)
        (self.root / "ops" / "job.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8")
        tools = base / "bin"
        tools.mkdir()
        svn = tools / "svn"
        svn.write_text(FAKE_SVN.format(python=sys.executable), encoding="utf-8")
        svn.chmod(svn.stat().st_mode | stat.S_IEXEC)
        # No git on PATH: the working copy must be found as svn, not as a parent git checkout.
        environment = {"PATH": str(tools), "FAKE_SVN_ROOT": str(self.root)}
        self.patch = mock.patch.dict(os.environ, environment)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def test_extract_records_the_svn_revision(self) -> None:
        repository = repository_at(self.root / "ops")
        self.assertIsInstance(repository, SvnRepository)
        revisions = source_revisions(self.root, [{"name": "ops", "type": "sql-files", "folders": ["ops"]}])
        self.assertEqual(
            revisions["ops"],
            {"vcs": "svn", "repository_root": ".", "commit": "42", "branch": "^/trunk", "dirty": True, "dirty_files": 1},
        )

    def test_stale_check_counts_revisions_since_the_extract(self) -> None:
        recorded = {"ops": {"vcs": "svn", "repository_root": ".", "commit": "40", "branch": "^/trunk"}}
        [change] = _revision_changes(self.root, recorded)
        self.assertEqual((change["vcs"], change["head"], change["commits_since"]), ("svn", "42", 2))

    def test_dates_and_keywords_resolve_on_the_server(self) -> None:
        repository = SvnRepository(self.root)
        self.assertEqual(repository.revision_before("2026-01-31"), "40")
        self.assertEqual(repository.resolve("HEAD"), "45")
        self.assertIsNone(repository.resolve("main"))  # a git branch name means nothing to svn


if __name__ == "__main__":
    unittest.main()
