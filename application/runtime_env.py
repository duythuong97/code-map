from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

if not ENV_FILE.is_file():
    raise RuntimeError(f"Missing dotenv file: {ENV_FILE}")
load_dotenv(ENV_FILE, override=True)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def required_absolute_path(name: str) -> Path:
    path = Path(required_env(name)).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path: {path}")
    return path.resolve()

def project_path(*parts: str) -> Path:
    return required_absolute_path("CODE_MAP_PROJECT_ROOT").joinpath(*parts).resolve()
