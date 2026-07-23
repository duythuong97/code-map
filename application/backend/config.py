from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CSV_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "cp932", "shift_jis", "euc_jp"})


def load_json_config(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def load_app_config(path: str | Path) -> dict[str, Any]:
    config = load_json_config(path)
    if "api" not in config:
        return config
    return {**config["api"], "db": config["db"]}








def resolve_path(value: str | Path, base_dir: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()























