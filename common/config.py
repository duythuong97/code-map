from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json_config(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def load_app_config(path: str | Path) -> dict[str, Any]:
    config = load_json_config(path)
    if "api" not in config:
        return config
    app_config = dict(config["api"])
    app_config["db"] = config["db"]
    table_definition = config.get("extractors", {}).get("table_definition")
    if table_definition:
        app_config["table_definition"] = table_definition
    return app_config


def load_extractor_config(path: str | Path) -> dict[str, Any]:
    config = load_json_config(path)
    if "extractors" not in config:
        return config
    extractor_config = dict(config["extractors"])
    extractor_config["db"] = config["db"]
    return extractor_config


def resolve_path(value: str | Path, base_dir: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()
