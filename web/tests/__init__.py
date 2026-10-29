import sys
from pathlib import Path

# The web backend reuses extractor/code_tree_exporter (contract helpers).
_EXTRACTOR = Path(__file__).resolve().parents[2] / "extractor"
if str(_EXTRACTOR) not in sys.path:
    sys.path.insert(0, str(_EXTRACTOR))

# Without a .env (CI, a fresh clone) the app still needs its settings; a local
# .env overrides these.
import os  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
for _name, _value in {
    "CODE_MAP_PROJECT_ROOT": str(_ROOT),
    "CODE_MAP_SOURCE_ROOT": str(_ROOT / "examples" / "sample-source"),
    "CODE_MAP_GRAPH_OUTPUT": str(_ROOT / "output" / "demo"),
    "CODE_MAP_BACKEND_LOG_LEVEL": "WARNING",
    "CODE_MAP_GRAPH_API_PREFIX": "/api/graph",
    "CODE_MAP_MAX_CONTENT_LENGTH": "16777216",
    "CODE_MAP_KNOWLEDGE_TOKEN": "test-token",
}.items():
    os.environ.setdefault(_name, _value)
