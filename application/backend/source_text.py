from __future__ import annotations

from pathlib import Path

SOURCE_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp")


def decode_source_bytes(data: bytes) -> str:
    for encoding in SOURCE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # ponytail: lossy fallback supports unknown legacy source; raise instead when strict source integrity is required.
    return data.decode("utf-8", errors="replace")


def read_source_text(path: str | Path) -> str:
    return decode_source_bytes(Path(path).read_bytes())
