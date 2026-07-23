"""Production entrypoint for the Code Map Flask app.

PM2 should run this file instead of embedding Python bootstrap code in
``ecosystem.config.js``.  Keeping the startup logic in Python makes it work
consistently on macOS, Linux, and Windows.
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.runtime_env import project_path
from application.backend.config import load_app_config
from application.backend.logging_config import configure_backend_logging

CONFIG_PATH = project_path("configs", "code-map.config.json")
APP_CONFIG = load_app_config(CONFIG_PATH)


def main() -> None:
    from waitress import serve
    from application.backend.api.app import app

    host = APP_CONFIG["host"]
    port = int(APP_CONFIG["port"])
    log_path = configure_backend_logging(APP_CONFIG)
    logging.getLogger("code_map.backend").info(
        "starting backend server host=%s port=%s prefix=%s db=%s log_path=%s",
        host,
        port,
        APP_CONFIG.get("url_prefix", ""),
        APP_CONFIG.get("db", ""),
        log_path or "disabled",
    )
    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()
