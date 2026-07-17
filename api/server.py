"""Production entrypoint for the Code Map Flask app.

PM2 should run this file instead of embedding Python bootstrap code in
``ecosystem.config.js``.  Keeping the startup logic in Python makes it work
consistently on macOS, Linux, and Windows.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import load_app_config, resolve_path

CONFIG_PATH = resolve_path(os.environ.get("CODE_MAP_CONFIG", "code-map.config.json"), ROOT)
APP_CONFIG = load_app_config(CONFIG_PATH)


def main() -> None:
    from waitress import serve
    from api.app import app

    host = APP_CONFIG.get("host", "127.0.0.1")
    port = int(APP_CONFIG.get("port", 8000))
    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()
