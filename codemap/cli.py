"""One command for everything: ``codemap <command>`` (codemap.cmd on Windows, ./codemap.sh elsewhere).

    codemap init --root C:/sources --config D:/code-map-prod/code-map.json   write the config from the repositories
    codemap validate                                                           check the config and the runtimes
    codemap extract [--skip-validate] [extract options such as --at REV]      validate, then extract
    codemap serve                                                              web app + HTTP MCP endpoint
    codemap mcp [--self-test]                                                  MCP server on stdio (for an agent)
    codemap stale                                                              is the graph still true for the code?
    codemap kb revalidate | queue | usage                                      knowledge base checks

Every command reads the config in --config, else CODE_MAP_CONFIG (environment
or the repository .env), else the demo (examples/demo-config.json).
"""
from __future__ import annotations

import argparse
import sys

from codemap.settings import config_path  # loads the repository .env

COMMANDS = ("init", "validate", "extract", "serve", "mcp", "stale", "kb")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"} or arguments[0] not in COMMANDS:
        print(__doc__.strip())
        return 0 if arguments[:1] in ([], ["-h"], ["--help"]) else 2
    command, rest = arguments[0], arguments[1:]
    if command == "serve":
        from web.backend.api.server import main as serve

        serve(rest)
        return 0
    if command == "mcp":
        from codemap.mcp.__main__ import main as mcp

        return mcp(rest)
    if command == "kb":
        from codemap.kb import main as kb

        return kb(rest)
    return _extractor(command, rest)


def _extractor(command: str, rest: list[str]) -> int:
    from code_tree_exporter.cli import main as exporter

    parser = argparse.ArgumentParser(prog=f"codemap {command}", add_help=False)
    parser.add_argument("--config")
    known, extra = parser.parse_known_args(rest)
    if command == "init":
        # init creates the config, so it must not fall back to the demo.
        return exporter(["init", *rest])
    config = config_path(known.config)
    if command == "stale":
        from codemap.settings import load_settings

        graph = load_settings(config)["graph_output"]
        return exporter(["stale", "--graph", str(graph), *extra])
    if not config.is_file():
        print(f"Config not found: {config}", file=sys.stderr)
        return 1
    print(f"Config: {config}", flush=True)
    if command == "validate":
        return exporter(["validate", "--config", str(config)])
    skip_validate = "--skip-validate" in extra
    extra = [value for value in extra if value != "--skip-validate"]
    if not skip_validate:
        print("\n== Validate config and runtimes ==", flush=True)
        if exporter(["validate", "--config", str(config)]) != 0:
            print("\nValidation failed; the problems are listed above.", file=sys.stderr)
            return 1
    print("\n== Extract graph ==", flush=True)
    if exporter(["extract", "--config", str(config), *extra]) != 0:
        print("\nExtract failed; the error is printed above.", file=sys.stderr)
        return 1
    print("\nDone. The web app and the MCP server read the output folder printed above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
