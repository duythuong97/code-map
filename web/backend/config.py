from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CSV_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "cp932", "shift_jis", "euc_jp"})
_ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def load_json_config(path: str | Path) -> dict[str, Any]:
    config_path = absolute_path(path, "config path")
    text = config_path.read_text(encoding="utf-8")
    text = _ENV_PATTERN.sub(lambda match: _required_env(match.group(1)), text)
    return json.loads(text)


def load_app_config(path: str | Path) -> dict[str, Any]:
    config = load_json_config(path)
    if "api" not in config:
        return config
    return {**config["api"], "db": config["db"]}








def absolute_path(value: str | Path, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {path}")
    return path.resolve()

def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value























