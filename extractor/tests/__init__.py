"""Extractor tests. ``code_tree_exporter`` comes from the editable install; the
shared fixtures (``testdata``) live at the repository root, which unittest only
puts on sys.path for the read-side suites (``-t .``)."""
import sys
from pathlib import Path

_REPOSITORY = str(Path(__file__).resolve().parents[2])
if _REPOSITORY not in sys.path:
    sys.path.append(_REPOSITORY)
