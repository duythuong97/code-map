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

if sys.flags.no_site:
    venv_site_packages = (
        ROOT / ".venv" / "Lib" / "site-packages"
        if os.name == "nt"
        else ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    )
    if venv_site_packages.is_dir():
        venv_site_packages_path = str(venv_site_packages)
        if venv_site_packages_path not in sys.path:
            insert_at = 1 if sys.path and sys.path[0] == str(ROOT) else 0
            sys.path.insert(insert_at, venv_site_packages_path)

from application.backend.config import load_app_config, resolve_path
from application.backend.logging_config import configure_backend_logging

CONFIG_PATH = resolve_path(os.environ.get("CODE_MAP_CONFIG", "configs/code-map.config.json"), ROOT)
APP_CONFIG = load_app_config(CONFIG_PATH)


def main() -> None:
    from waitress import serve
    from application.backend.api.app import app

    host = APP_CONFIG.get("host", "127.0.0.1")
    port = int(APP_CONFIG.get("port", 8000))
    log_path = configure_backend_logging(APP_CONFIG, CONFIG_PATH.parent)
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
