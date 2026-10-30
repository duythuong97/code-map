"""Settings: the repository ``.env`` and the extractor config the app serves.

The extractor config already says where the source (``root``) and the extract
output (``output``) are; the web app and the MCP server use the same values,
so a path is written once. An optional ``web`` section overrides the defaults:

    "web": {"host": "0.0.0.0", "port": 8000, "urlPrefix": "/code-map", "threads": 8,
            "knowledgeDb": "./knowledge.sqlite", "logPath": "./logs/backend.log"}

Relative ``knowledgeDb`` and ``logPath`` resolve against the config file.
Without a config the demo is served and its state lives in ``var/``.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from codemap import PROJECT_ROOT

from code_tree_exporter.env_loader import load_dotenv  # noqa: E402
from code_tree_exporter.pipeline import load_config  # noqa: E402

ENV_FILE = PROJECT_ROOT / ".env"
DEMO_CONFIG = PROJECT_ROOT / "examples" / "demo-config.json"
# Demo output, knowledge db and logs: one gitignored folder instead of output/, data/ and logs/.
STATE_DIR = PROJECT_ROOT / "var"
_LEGACY_KNOWLEDGE = PROJECT_ROOT / "data" / "knowledge.sqlite"

# Same loader as the extractor: values are taken literally (C:\new stays C:\new)
# and variables already set in the process (CI, tests, pm2) win over .env.
load_dotenv(ENV_FILE)

# Settings that moved into the extractor config; warn so a stale .env is not silently ignored.
OBSOLETE_ENV = (
    "CODE_MAP_PROJECT_ROOT",
    "CODE_MAP_SOURCE_ROOT",
    "CODE_MAP_GRAPH_OUTPUT",
    "CODE_MAP_GRAPH_API_PREFIX",
    "CODE_MAP_MAX_CONTENT_LENGTH",
    "CODE_TREE_OUTPUT",
)
_obsolete = [name for name in OBSOLETE_ENV if os.environ.get(name)]
if _obsolete:
    logging.getLogger("code_map").warning(
        "%s no longer used: set CODE_MAP_CONFIG to the extractor config instead (see .env.example)",
        ", ".join(_obsolete),
    )


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts).resolve()


def config_path(value: str | Path | None = None) -> Path:
    """The extractor config to serve: ``value``, else $CODE_MAP_CONFIG, else the demo.

    ``value`` (a command-line flag) resolves against the current folder; a
    relative $CODE_MAP_CONFIG against the repository, so ``.env`` can say
    ``CODE_MAP_CONFIG=examples/demo-config.json``.
    """
    if value:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("CODE_MAP_CONFIG")
    if not configured:
        return DEMO_CONFIG
    path = Path(configured).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def default_knowledge_db() -> Path:
    """var/knowledge.sqlite; data/knowledge.sqlite of an older checkout keeps being used."""
    current = STATE_DIR / "knowledge.sqlite"
    return _LEGACY_KNOWLEDGE if _LEGACY_KNOWLEDGE.is_file() and not current.is_file() else current


WEB_DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,
    "urlPrefix": "/code-map",
    "graphApiPrefix": "/api/graph",
    "maxContentLength": 16 * 1024 * 1024,
    "logLevel": "INFO",
    # Requests served at once (web pages and HTTP MCP calls share them); waitress's own default is 4.
    "threads": 8,
}


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    """Settings for ``path``, else $CODE_MAP_CONFIG, else the demo config."""
    config_file = config_path(path)
    config = load_config(config_file)
    web = config.get("web") or {}
    if not isinstance(web, dict):
        raise ValueError(f"{config_file}: web must be an object")
    web = {**WEB_DEFAULTS, **web}

    def config_relative(key: str, default: Path) -> Path:
        value = web.get(key)
        if not value:
            return default
        candidate = Path(str(value)).expanduser()
        return (candidate if candidate.is_absolute() else config_file.parent / candidate).resolve()

    for key in ("root", "output"):
        if not config.get(key):
            raise ValueError(f"{config_file}: {key} is required")
    return {
        "config": config_file,
        "source_root": Path(config["root"]),
        "graph_output": Path(config["output"]),
        "knowledge_db": config_relative("knowledgeDb", default_knowledge_db()),
        "host": str(web["host"]),
        "port": int(web["port"]),
        "threads": max(1, int(web["threads"])),
        "url_prefix": str(web["urlPrefix"]),
        "graph_api_prefix": str(web["graphApiPrefix"]),
        "max_content_length": int(web["maxContentLength"]),
        "log_path": config_relative("logPath", STATE_DIR / "logs" / "backend.log"),
        "log_level": os.environ.get("CODE_MAP_BACKEND_LOG_LEVEL") or str(web["logLevel"]),
    }


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    """The served config's settings, read once per process."""
    return load_settings()


