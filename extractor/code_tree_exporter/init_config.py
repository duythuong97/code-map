"""Write a config by looking into every repository under a folder (``init``).

Each repository becomes one source per kind of code found in it:

* ``angular``: an ``angular.json`` (folders: its applications' ``sourceRoot``)
  or, for Angular 2-5, a ``.angular-cli.json`` (folders: its ``apps[].root``).
* ``dotnet-api`` / ``dotnet-batch``: entry ``.csproj`` files (web SDK or
  ASP.NET references, else ``OutputType`` Exe). Class libraries are left out:
  the pipeline stages the projects an entry project references. Test projects
  are skipped. A repository with ``.cs`` but no project file is an API when it
  has controllers, else a batch when it has a ``Main``.
* ``oracle-plsql``: ``.pks``/``.pkb``/... files, or ``.sql`` that create a
  package, procedure, function or trigger.
* ``sql-files``: other ``.sql`` and SQL*Loader ``.ctl``. A folder holding both
  kinds lists its files one by one, so no file is read by two sources.
* ``api-gateway``: CloudFormation templates (YAML/JSON) declaring an
  ``AWS::ApiGateway::Method``.

Running it again keeps the sources already in the config (and the edits made
to them, such as ``database``) and only adds repositories it has not seen;
names in ``ignoreRepositories`` are never added.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .pipeline import _BLOCKED_DIRECTORIES, load_config

PLSQL_SUFFIXES = {".pks", ".pkb", ".pck", ".pls", ".plb", ".fnc", ".prc", ".trg"}
_PLSQL_DDL = re.compile(
    rb"create\s+(?:or\s+replace\s+)?(?:editionable\s+|noneditionable\s+)?"
    rb"(?:package|procedure|function|trigger|type\s+body)\b",
    re.IGNORECASE,
)
_WEB_PROJECT = re.compile(
    r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"|Microsoft\.AspNetCore|System\.Web\.(?:Mvc|Http)'
    r"|349c5851-65df-11da-9384-00065b846f21",  # Web Application project type (.NET Framework)
    re.IGNORECASE,
)
_EXE_PROJECT = re.compile(r"<OutputType>\s*(?:Win)?Exe\s*</OutputType>", re.IGNORECASE)
_TEST_PROJECT = re.compile(r"Microsoft\.NET\.Test\.Sdk|<IsTestProject>\s*true", re.IGNORECASE)
# Read this much of a .sql file to tell PL/SQL from plain SQL.
_SQL_SNIFF_BYTES = 64 * 1024
_SKIP = _BLOCKED_DIRECTORIES | {".vs", ".idea", "TestResults", "coverage"}
_TEMPLATE_SUFFIXES = {".yml", ".yaml", ".json", ".template"}
_GATEWAY_METHOD = b"AWS::ApiGateway::Method"
_TEMPLATE_SNIFF_BYTES = 4 * 1024 * 1024
# Sources that read no database.
_NO_DATABASE = frozenset({"angular", "api-gateway"})


ANGULAR_WORKSPACES = frozenset({"angular.json", ".angular-cli.json", "angular-cli.json"})


def is_working_copy(path: Path) -> bool:
    """A git repository or a Subversion working copy (1.7+ keeps one .svn at its top)."""
    return (path / ".git").exists() or (path / ".svn").is_dir()


def find_repositories(root: Path, depth: int = 3) -> list[Path]:
    """Folders directly under ``root`` (or deeper, under grouping folders that are no working copy)."""
    repositories = []
    for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir() or child.name.startswith(".") or child.name in _SKIP:
            continue
        nested = [] if is_working_copy(child) or depth <= 1 else find_repositories(child, depth - 1)
        if nested and all(is_working_copy(path) for path in nested):
            repositories.extend(nested)  # a grouping folder such as C:\sources\team-a
        else:
            repositories.append(child)
    return repositories


def detect_sources(root: Path, repository: Path) -> list[dict]:
    """The sources of one repository; ``folders`` are relative to ``root``."""
    prefix = PurePosixPath(repository.relative_to(root).as_posix())
    angular: list[str] = []
    projects: dict[str, list[str]] = {"dotnet-api": [], "dotnet-batch": []}
    sql: dict[PurePosixPath, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    csproj_seen, loose_cs = False, []
    gateway: list[str] = []
    for directory, names, files in os.walk(repository):
        names[:] = sorted(name for name in names if name not in _SKIP and not name.startswith("."))
        here = Path(directory)
        relative = PurePosixPath(prefix, here.relative_to(repository).as_posix()) if here != repository else prefix
        for name in sorted(files):
            suffix = Path(name).suffix.lower()
            if name in ANGULAR_WORKSPACES:
                angular.extend(_angular_folders(here / name, relative))
            elif suffix == ".cs" and not csproj_seen:
                loose_cs.append(here / name)
            elif suffix == ".csproj":
                csproj_seen = True
                kind = _project_kind(here / name)
                if kind:
                    projects[kind].append(str(relative / name))
            elif suffix in PLSQL_SUFFIXES or suffix == ".sql" and _is_plsql(here / name):
                sql[relative]["oracle-plsql"].append(name)
            elif suffix in {".sql", ".ctl"}:
                sql[relative]["sql-files"].append(name)
            elif suffix in _TEMPLATE_SUFFIXES and _is_gateway_template(here / name):
                gateway.append(str(relative / name))
    found: list[tuple[str, list[str]]] = []
    if angular:
        found.append(("angular", sorted(set(angular))))
    found.extend((kind, sorted(paths)) for kind, paths in projects.items() if paths)
    if not csproj_seen and loose_cs:
        # .cs without a project file: an API has controllers, a batch a Main.
        kind = _loose_cs_kind(loose_cs)
        if kind:
            found.append((kind, [str(prefix)]))
    found.extend(_sql_sources(sql, prefix))
    if gateway:
        found.append(("api-gateway", sorted(gateway)))
    name = repository.name
    return [
        {
            "name": name if len(found) == 1 else f"{name}-{_SHORT[kind]}",
            "type": kind,
            "repository": name,
            "folders": folders,
        }
        for kind, folders in found
    ]


_SHORT = {"angular": "web", "dotnet-api": "api", "dotnet-batch": "batch", "oracle-plsql": "db", "sql-files": "sql",
          "api-gateway": "gateway"}


def init_config(
    root: Path | None,
    config_path: Path,
    *,
    name: str | None = None,
    database: str | None = None,
    schema: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Create ``config_path`` or add the newly found sources to it; returns a report.

    ``root`` is needed for a new config; an existing one keeps its own root.
    """
    config_path = config_path.expanduser().resolve()
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        root = Path(load_config(config_path)["root"])
    elif root is None:
        raise ValueError(f"--root is required to create {config_path}")
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")
    if not config_path.is_file():
        config = {"name": name or root.name, "root": _config_value(root, config_path), "output": "./output"}
        for key, folder in (("inputData", "input-data"), ("catalog", "catalog")):
            if (config_path.parent / folder).is_dir():
                config[key] = {"folder": f"./{folder}", "strict": True} if key == "catalog" else f"./{folder}"
        # Repositories init should not add again (removed on purpose); extract ignores this key.
        config["ignoreRepositories"] = []
        config["sources"] = []
    system = name or config.get("name") or root.name
    existing = {str(source.get("name")) for source in config.get("sources", [])}
    known_repositories = {str(source.get("repository") or source.get("name")) for source in config.get("sources", [])}
    known_repositories |= {str(name) for name in config.get("ignoreRepositories", [])}
    report: dict[str, list] = {"added": [], "kept": sorted(existing), "skipped": []}
    for repository in find_repositories(root):
        if repository.name in known_repositories:
            continue
        sources = detect_sources(root, repository)
        if not sources:
            report["skipped"].append(repository.name)
            continue
        for source in sources:
            if source["name"] in existing:
                continue
            source = {"name": source["name"], "type": source["type"], "system": system, **source}
            if source["type"] not in _NO_DATABASE:
                source.update({key: value for key, value in (("database", database), ("schema", schema)) if value})
            config["sources"].append(source)
            existing.add(source["name"])
            report["added"].append(source)
    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["config"] = str(config_path)
    report["dry_run"] = dry_run
    return report


def format_report(report: dict) -> str:
    lines = [f"Config: {report['config']}"]
    if report["kept"]:
        lines.append(f"Kept {len(report['kept'])} source(s) already in the config.")
    for source in report["added"]:
        lines.append(f"  + {source['name']:<30} {source['type']:<13} {', '.join(source['folders'][:3])}"
                     + (f" (+{len(source['folders']) - 3})" if len(source["folders"]) > 3 else ""))
    if not report["added"]:
        lines.append("No new source found.")
    if report["skipped"]:
        lines.append(f"Nothing to scan in: {', '.join(report['skipped'])}")
    missing = [source["name"] for source in report["added"] if source["type"] not in _NO_DATABASE and "database" not in source]
    if missing:
        lines.append("Add \"database\" and \"schema\" to the SQL/.NET sources (or pass --database/--schema): "
                     + ", ".join(missing))
    if report.get("dry_run"):
        lines.append("Dry run: nothing written.")
    else:
        lines.append("Review the config, then run codemap extract (it validates first).")
    return "\n".join(lines)


def _angular_folders(path: Path, relative: PurePosixPath) -> list[str]:
    try:
        workspace = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [str(relative / "src")]
    if isinstance(workspace.get("apps"), list):  # .angular-cli.json (Angular 2-5): apps[].root, default "src"
        roots = [str(app.get("root") or "src") for app in workspace["apps"] if isinstance(app, dict)]
    else:
        roots = [
            str(project.get("sourceRoot") or PurePosixPath(project.get("root") or "", "src"))
            for project in (workspace.get("projects") or {}).values()
            if isinstance(project, dict) and project.get("projectType", "application") == "application"
        ]
    return [str(relative / source_root) for source_root in roots or ["src"]]


def _project_kind(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    if _TEST_PROJECT.search(text) or re.search(r"\.Tests?$", path.stem, re.IGNORECASE):
        return None
    if _WEB_PROJECT.search(text):
        return "dotnet-api"
    return "dotnet-batch" if _EXE_PROJECT.search(text) else None


_CONTROLLER = re.compile(rb"\[ApiController\]|:\s*(?:Api)?Controller(?:Base)?\b")
_MAIN = re.compile(rb"static\s+(?:async\s+)?(?:void|int|Task(?:<int>)?)\s+Main\s*\(")


def _loose_cs_kind(paths: list[Path]) -> str | None:
    kind = None
    for path in paths:
        try:
            with path.open("rb") as handle:
                head = handle.read(_SQL_SNIFF_BYTES)
        except OSError:
            continue
        if _CONTROLLER.search(head):
            return "dotnet-api"
        if _MAIN.search(head):
            kind = "dotnet-batch"
    return kind


def _is_gateway_template(path: Path) -> bool:
    try:
        if path.stat().st_size > _TEMPLATE_SNIFF_BYTES:
            return False
        return _GATEWAY_METHOD in path.read_bytes()
    except OSError:
        return False


def _is_plsql(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return bool(_PLSQL_DDL.search(handle.read(_SQL_SNIFF_BYTES)))
    except OSError:
        return False


def _sql_sources(files: dict[PurePosixPath, dict[str, list[str]]], prefix: PurePosixPath) -> list[tuple[str, list[str]]]:
    """The highest folders whose SQL files are all one source type; files of a mixed
    folder are listed one by one, so no file is read by two sources."""
    kinds: dict[PurePosixPath, set[str]] = defaultdict(set)
    children: dict[PurePosixPath, set[PurePosixPath]] = defaultdict(set)
    for folder, by_kind in files.items():
        for current in (folder, *folder.parents):
            kinds[current] |= set(by_kind)
            if current == prefix:
                break
            children[current.parent].add(current)
    chosen: dict[str, list[str]] = defaultdict(list)

    def visit(folder: PurePosixPath) -> None:
        if len(kinds[folder]) == 1:
            chosen[next(iter(kinds[folder]))].append(str(folder))
            return
        for kind, names in files.get(folder, {}).items():
            chosen[kind].extend(str(folder / name) for name in names)
        for child in sorted(children[folder]):
            visit(child)

    if files:
        visit(prefix)
    return [(kind, sorted(chosen[kind])) for kind in ("oracle-plsql", "sql-files") if chosen.get(kind)]


def _config_value(path: Path, config_path: Path) -> str:
    """``path`` relative to the config when it is close by, else absolute; always with ``/``."""
    try:
        relative = Path(os.path.relpath(path, config_path.parent))
    except ValueError:  # another drive on Windows
        return path.as_posix()
    return relative.as_posix() if len(relative.parts) <= 3 else path.as_posix()
