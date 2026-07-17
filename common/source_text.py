from __future__ import annotations

from pathlib import Path

SOURCE_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp")

def read_source_text(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in SOURCE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # ponytail: lossy fallback for unknown legacy encodings; add charset detection when mixed encodings appear.
    return data.decode("utf-8", errors="replace")
