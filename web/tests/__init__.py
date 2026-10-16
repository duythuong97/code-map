import sys
from pathlib import Path

# The web backend reuses extractor/code_tree_exporter (contract helpers).
_EXTRACTOR = Path(__file__).resolve().parents[2] / "extractor"
if str(_EXTRACTOR) not in sys.path:
    sys.path.insert(0, str(_EXTRACTOR))
