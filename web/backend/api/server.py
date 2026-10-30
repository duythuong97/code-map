"""``codemap serve``: the web app and the HTTP MCP endpoint on waitress (PM2 runs the same command)."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codemap serve", description="Serve the web app and the HTTP MCP endpoint.")
    parser.add_argument("--config", help="Extractor config to serve (default: $CODE_MAP_CONFIG, else the demo).")
    args = parser.parse_args(argv)
    if args.config:
        # Settings are read when the app module is imported.
        os.environ["CODE_MAP_CONFIG"] = str(Path(args.config).expanduser().resolve())

    from waitress import serve

    from web.backend.api.app import APP_CONFIG, app
    from web.backend.logging_config import configure_backend_logging

    host = APP_CONFIG["host"]
    port = APP_CONFIG["port"]
    log_path = configure_backend_logging(APP_CONFIG)
    logging.getLogger("code_map.backend").info(
        "starting backend server config=%s host=%s port=%s prefix=%s graph_output=%s log_path=%s",
        APP_CONFIG["config"],
        host,
        port,
        APP_CONFIG["url_prefix"],
        APP_CONFIG["graph_output"],
        log_path or "disabled",
    )
    print(f"Code Map: http://{host}:{port}{APP_CONFIG['url_prefix']}/  (config {APP_CONFIG['config']})", flush=True)
    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()
