"""Install the AIDD kit into a source repository.

    python aidd/init.py <repo> --server-url http://codemap.internal:8000/code-map/mcp
    python aidd/init.py <repo> --graph-output /data/codemap/out        # local stdio server from this checkout
    python aidd/init.py <repo> ... --source-type dotnet-api --system order-system --codemap-repository acme/code-map

Writes (existing files are kept unless --force):

* AGENTS.md, CLAUDE.md, .github/copilot-instructions.md    rules for every agent
* .vscode/mcp.json (Copilot) and .mcp.json (Claude Code)    the codemap MCP server
* .github/prompts/*.prompt.md                              the workflow steps, rendered from the MCP prompts
* .github/agents/*.agent.md                                requirement analyst, impact analyst, challenger, implementer
* docs/changes/_template/                                  requirement, decision table, impact.yaml, design, tests
* .github/workflows/codemap-pr.yml, .codemap/config.json   PR check: planned vs actual impact
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Path(__file__).resolve().parent / "templates"
for path in (ROOT, ROOT / "extractor"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from web.backend.codemap_mcp.prompts import COMMON, PROMPTS  # noqa: E402

SOURCE_TYPES = ("angular", "dotnet-api", "dotnet-batch", "oracle-plsql", "sql-files")


def mcp_servers(args: argparse.Namespace) -> tuple[dict, dict]:
    """(.vscode/mcp.json, .mcp.json) for the chosen server(s)."""
    vscode: dict = {"servers": {}, "inputs": []}
    claude: dict = {"mcpServers": {}}
    if args.server_url:
        vscode["servers"]["codemap"] = {"type": "http", "url": args.server_url,
                                        "headers": {"X-Code-Map-User": "${input:codemapUser}"}}
        vscode["inputs"].append({"type": "promptString", "id": "codemapUser",
                                 "description": "Your name (Code Map call log and proposals)"})
        claude["mcpServers"]["codemap"] = {"type": "http", "url": args.server_url,
                                           "headers": {"X-Code-Map-User": "${USER}"}}
    if args.graph_output:
        python = args.python or str(ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
        stdio = {"type": "stdio", "command": python,
                 "args": ["-m", "web.backend.codemap_mcp", "--graph-output", args.graph_output],
                 "cwd": str(ROOT)}
        if args.knowledge_db:
            stdio["args"] += ["--knowledge-db", args.knowledge_db]
        vscode["servers"]["codemap" if not args.server_url else "codemap-local"] = stdio
        claude["mcpServers"]["codemap" if not args.server_url else "codemap-local"] = {
            key: value for key, value in stdio.items() if key != "type"
        }
    if not vscode["inputs"]:
        del vscode["inputs"]
    return vscode, claude


def prompt_file(prompt) -> str:
    """A VS Code prompt file: the MCP prompt with ${input:…} variables instead of arguments."""
    values = {
        argument["name"]: f"${{input:{argument['name']}:{argument['description']}}}" for argument in prompt.arguments
    }
    body = prompt.template.format(**values).strip()
    description = prompt.description.replace("\n", " ")
    return f"---\ndescription: {json.dumps(description, ensure_ascii=False)}\nmode: agent\n---\n{body}\n"


def install(target: Path, args: argparse.Namespace) -> list[tuple[str, str]]:
    replacements = {
        "__REPO_NAME__": args.repo_name or target.name,
        "__SYSTEM_NAME__": args.system or target.name,
        "__SOURCE_TYPE__": args.source_type,
        "__CODEMAP_REPOSITORY__": args.codemap_repository or "owner/code-map",
    }
    planned: dict[str, str] = {}
    for path in sorted(TEMPLATES.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            for key, value in replacements.items():
                text = text.replace(key, value)
            planned[path.relative_to(TEMPLATES).as_posix()] = text
    for prompt in PROMPTS:
        planned[f".github/prompts/codemap-{prompt.name}.prompt.md"] = prompt_file(prompt)
    if args.server_url or args.graph_output:
        vscode, claude = mcp_servers(args)
        planned[".vscode/mcp.json"] = json.dumps(vscode, indent=2, ensure_ascii=False) + "\n"
        planned[".mcp.json"] = json.dumps(claude, indent=2, ensure_ascii=False) + "\n"
    results = []
    for relative, text in planned.items():
        destination = target / relative
        if destination.exists() and not args.force:
            results.append(("kept", relative))
            continue
        results.append(("would write" if args.dry_run else "wrote", relative))
        if not args.dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidd-init", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="The source repository to set up.")
    parser.add_argument("--server-url", help="Team MCP server, e.g. http://host:8000/code-map/mcp")
    parser.add_argument("--graph-output", help="Extract output for a local stdio server run from this checkout.")
    parser.add_argument("--knowledge-db", help="knowledge.sqlite for the local server.")
    parser.add_argument("--python", help="Python of this checkout's virtualenv (default: .venv).")
    parser.add_argument("--source-type", choices=SOURCE_TYPES, default="dotnet-api", help="For .codemap/config.json.")
    parser.add_argument("--system", help="System name for .codemap/config.json (default: the folder name).")
    parser.add_argument("--repo-name", help="Repository name (default: the folder name).")
    parser.add_argument("--codemap-repository", help="owner/name of the code-map repository, for the PR workflow.")
    parser.add_argument("--force", action="store_true", help="Overwrite files that exist.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        parser.error(f"{target} is not a folder")
    if not (args.server_url or args.graph_output):
        print("note: no --server-url or --graph-output; .vscode/mcp.json and .mcp.json are not written", file=sys.stderr)
    for state, relative in install(target, args):
        print(f"{state:12} {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
