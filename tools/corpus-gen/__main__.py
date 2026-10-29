"""``python tools/corpus-gen <command>``: see corpus_gen/__main__.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_gen.__main__ import main  # noqa: E402

raise SystemExit(main())
