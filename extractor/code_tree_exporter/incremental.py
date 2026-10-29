"""Reuse a source's extractor package when nothing it depends on changed.

The key covers the extractor code, the source's extractor config (without
temporary paths), the hash of every file it scans, the input data and the
runtime switches that change parser output. A hit copies the cached package
(and the issues the extractor reported) instead of running the extractor, so
re-extracting after a change in one repository only parses that repository.

Turn it off with ``"incremental": false`` in the config or
``CODE_TREE_INCREMENTAL=0``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

CACHE_VERSION = "1"
KEEP_ENTRIES_PER_SOURCE = 3
_SKIPPED_DIRECTORIES = frozenset({"node_modules", "bin", "obj", "__pycache__", ".msbuild", ".vs"})
_RUNTIME_SWITCHES = (
    "CODE_TREE_SQL_PARSER",
    "CODE_TREE_ANGULAR_FALLBACK",
    "CODE_TREE_DOTNET_READY_TO_RUN",
)
_VOLATILE_CONFIG_KEYS = frozenset({"root", "output", "inputData", "_typescriptPath"})


def enabled(config: dict) -> bool:
    if os.environ.get("CODE_TREE_INCREMENTAL", "").strip() in {"0", "false", "no"}:
        return False
    return config.get("incremental", True) is not False


def cache_root(config: dict, output: Path) -> Path:
    configured = config.get("cacheDir")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser().resolve()
    return output.with_name(output.name + ".cache")


def job_key(
    *,
    config_type: str,
    extractor_config: dict,
    file_hashes: list[tuple[str, str]],
    runtime: str = "",
) -> str:
    digest = hashlib.sha256()
    stable_config = {
        key: value for key, value in extractor_config.items() if key not in _VOLATILE_CONFIG_KEYS
    }
    input_data = extractor_config.get("inputData")
    payload = {
        "cache": CACHE_VERSION,
        "type": config_type,
        "extractors": extractors_fingerprint(),
        "config": stable_config,
        "files": sorted(file_hashes),
        "inputData": directory_fingerprint(Path(input_data)) if input_data else "",
        "typescript": _typescript_version(str(extractor_config.get("_typescriptPath") or "")),
        "switches": {name: os.environ.get(name, "") for name in _RUNTIME_SWITCHES},
        "runtime": runtime,
    }
    digest.update(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
    return digest.hexdigest()[:32]


def restore(cache: Path, source_name: str, key: str, destination: Path) -> list[dict] | None:
    """Copy a cached package to ``destination``; the extractor issues it had, or None on a miss."""
    entry = cache / _safe(source_name) / key
    if not (entry / "package" / "manifest.json").is_file() or not (entry / "issues.json").is_file():
        return None
    try:
        issues = json.loads((entry / "issues.json").read_text(encoding="utf-8"))
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(entry / "package", destination)
    except (OSError, ValueError):
        return None
    os.utime(entry)
    return issues if isinstance(issues, list) else []


def store(cache: Path, source_name: str, key: str, package: Path, issues: list[dict]) -> None:
    if not (package / "manifest.json").is_file():
        return
    source_cache = cache / _safe(source_name)
    entry = source_cache / key
    temporary = source_cache / f".{key}.tmp"
    try:
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True)
        shutil.copytree(package, temporary / "package")
        (temporary / "issues.json").write_text(json.dumps(issues, ensure_ascii=False, default=str), encoding="utf-8")
        shutil.rmtree(entry, ignore_errors=True)
        temporary.replace(entry)
    except OSError:
        shutil.rmtree(temporary, ignore_errors=True)
        return
    entries = sorted(
        (path for path in source_cache.iterdir() if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in entries[KEEP_ENTRIES_PER_SOURCE:]:
        shutil.rmtree(old, ignore_errors=True)


@lru_cache(maxsize=1)
def extractors_fingerprint() -> str:
    """Hash of the engine code that produces packages (extractors, parsers, this package)."""
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(_code_files(package_root)):
        digest.update(path.relative_to(package_root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


def directory_fingerprint(directory: Path) -> str:
    if not directory.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:32]


def dotnet_runtime(dotnet: str | None) -> str:
    """The SDK version; a different SDK can resolve projects differently."""
    if not dotnet:
        return ""
    try:
        completed = subprocess.run([dotnet, "--version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def _code_files(root: Path):
    for path in root.rglob("*"):
        if any(part in _SKIPPED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in {".py", ".cs", ".csproj", ".mjs", ".js", ".json", ".g4"}:
            yield path


def _typescript_version(path: str) -> str:
    if not path:
        return ""
    try:
        return str(json.loads((Path(path) / "package.json").read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        return path


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) or "source"
