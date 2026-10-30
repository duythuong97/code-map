"""Imports only point down: web -> codemap.mcp -> codemap -> code_tree_exporter."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Folder -> (module prefixes it must not import, sub-folders that are a layer of their own).
LAYERS = {
    "extractor/code_tree_exporter": ({"codemap", "web"}, ()),
    "codemap": ({"codemap.mcp", "web"}, ("codemap/mcp/",)),
    "codemap/mcp": ({"web"}, ()),
}
# The command line wires every layer together; it is the one place allowed to.
ENTRY_POINTS = {"codemap/cli.py"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _forbidden(name: str, prefixes: set[str]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


class LayerTests(unittest.TestCase):
    def test_no_import_points_up(self) -> None:
        violations = []
        for folder, (prefixes, own_layers) in LAYERS.items():
            for path in sorted((ROOT / folder).rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                if (
                    relative in ENTRY_POINTS
                    or "/tests/" in relative
                    or "/node_modules/" in relative
                    or any(relative.startswith(layer) for layer in own_layers)
                ):
                    continue
                for name in sorted(name for name in _imports(path) if _forbidden(name, prefixes)):
                    violations.append(f"{relative} imports {name}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
