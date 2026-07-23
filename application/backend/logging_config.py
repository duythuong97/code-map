from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5


def configure_backend_logging(app_config: dict[str, Any], base_dir: Path) -> Path | None:
    """Configure backend API logs to a rotating file.

    The file path is controlled by ``CODE_MAP_BACKEND_LOG_PATH`` or
    ``api.log_path`` in ``configs/code-map.config.json``.  The handler is
    shared by application, request, Werkzeug, and Waitress loggers so API
    errors and access summaries land in the same file.
    """
    raw_path = os.environ.get("CODE_MAP_BACKEND_LOG_PATH") or app_config.get("log_path")
    if not raw_path:
        return None

    log_path = Path(str(raw_path)).expanduser()
    log_path = log_path if log_path.is_absolute() else (base_dir / log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = _log_level(app_config.get("log_level") or os.environ.get("CODE_MAP_BACKEND_LOG_LEVEL") or "INFO")
    handler = _existing_file_handler(log_path)
    if handler is None:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=int(app_config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
            backupCount=int(app_config.get("log_backups", DEFAULT_LOG_BACKUPS)),
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        handler._code_map_backend_log_path = str(log_path)  # type: ignore[attr-defined]
    handler.setLevel(level)

    for name in ("code_map.backend", "application.backend", "werkzeug", "waitress"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not _has_backend_file_handler(logger, log_path):
            logger.addHandler(handler)
    return log_path


def _log_level(value: Any) -> int:
    if isinstance(value, int):
        return value
    return getattr(logging, str(value).strip().upper(), logging.INFO)


def _existing_file_handler(log_path: Path) -> RotatingFileHandler | None:
    for name in ("code_map.backend", "application.backend", "werkzeug", "waitress"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if getattr(handler, "_code_map_backend_log_path", None) == str(log_path):
                return handler if isinstance(handler, RotatingFileHandler) else None
    return None


def _has_backend_file_handler(logger: logging.Logger, log_path: Path) -> bool:
    return any(
        getattr(handler, "_code_map_backend_log_path", None) == str(log_path)
        for handler in logger.handlers
    )