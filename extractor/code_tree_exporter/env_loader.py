from __future__ import annotations

import json
import os
import re
from pathlib import Path

# The code-map checkout's .env (extractor/code_tree_exporter/env_loader.py -> repository root).
REPOSITORY_ENV = Path(__file__).resolve().parents[2] / ".env"
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_json(text: str) -> str:
    """Replace ``${NAME}`` in a JSON document with the JSON-escaped value.

    The references sit inside JSON strings, so a Windows path such as
    ``C:\\src\\new`` must become ``C:\\\\src\\\\new``; inserted raw, ``\\n`` would
    turn into a line break or the JSON would not parse.
    """

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Config uses ${{{name}}} but {name} is not set (check .env)")
        return json.dumps(os.environ[name], ensure_ascii=False)[1:-1]

    return _ENV_REFERENCE.sub(value, text)


def load_dotenv(*paths: Path) -> None:
    """Load simple KEY=VALUE files without overriding the process environment."""
    for path in paths:
        if not path.is_file():
            continue
        # utf-8-sig: Notepad on Windows may save the file with a BOM.
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"Invalid .env entry at {path}:{line_number}")
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
                raise ValueError(f"Invalid .env key at {path}:{line_number}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)
