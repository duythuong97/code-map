from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from code_tree_exporter.env_loader import load_dotenv


def worker_assembly_path(script_path: Path, project_name: str) -> Path:
    project = script_path.with_name(project_name)
    return project.parent / "bin" / "codetree" / (project.stem + ".dll")


def ensure_worker_built(
    script_path: Path, project_name: str, *, dotnet: str
) -> int:
    """Build the packaged Roslyn worker if missing/stale. Not safe to call
    concurrently for the same project_name; callers running sources in
    parallel must pre-warm each distinct worker sequentially first."""
    project = script_path.with_name(project_name)
    assembly = worker_assembly_path(script_path, project_name)
    extractors = project.parent.parent
    sources = [
        project,
        *project.parent.glob("*.cs"),
        *extractors.joinpath("_roslyn").glob("*.cs"),
        *extractors.joinpath("_sql").rglob("*.cs"),
        *extractors.joinpath("_sql").glob("*.csproj"),
    ]
    environment = _extractor_environment(dotnet, None)
    # Target the installed SDK's runtime: ReadyToRun code compiled for an
    # older major version is discarded when the app rolls forward.
    framework = f"net{max(9, _sdk_major(dotnet, environment) or 9)}.0"
    stamp = assembly.parent / ".target-framework"
    if (
        assembly.is_file()
        and stamp.is_file()
        and stamp.read_text(encoding="utf-8").strip() == framework
        and not any(
            source.stat().st_mtime_ns > assembly.stat().st_mtime_ns
            for source in sources
            if source.is_file()
        )
    ):
        return 0
    common = [
        "-c", "Release", f"-p:CodeTreeTargetFramework={framework}",
        "-o", str(assembly.parent), "--nologo",
    ]
    rid = _runtime_identifier()
    returncode = 1
    if rid and os.environ.get("CODE_TREE_DOTNET_READY_TO_RUN", "1") != "0":
        # ReadyToRun precompiles the worker (and the large generated PL/SQL
        # parser) so each run skips most JIT work.
        returncode = subprocess.run(
            [
                dotnet, "publish", str(project), *common, "-r", rid,
                "--self-contained", "false", "-p:PublishReadyToRun=true",
            ],
            env=environment,
        ).returncode
        if returncode:
            print(
                f"warning: ReadyToRun publish of {project.name} failed; using a plain build",
                file=sys.stderr,
            )
    if returncode:
        returncode = subprocess.run(
            [dotnet, "build", str(project), *common], env=environment
        ).returncode
    if returncode == 0:
        stamp.write_text(framework, encoding="utf-8")
    return returncode


def _sdk_major(dotnet: str, environment: dict[str, str]) -> int | None:
    try:
        result = subprocess.run(
            [dotnet, "--version"],
            cwd=Path(__file__).resolve().parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return int(result.stdout.strip().split(".", 1)[0]) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _runtime_identifier() -> str | None:
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    system = {"linux": "linux", "darwin": "osx", "win32": "win"}.get(sys.platform)
    return f"{system}-{arch}" if arch and system else None


def run_dotnet_extractor(
    *,
    script_path: Path,
    project_name: str,
    description: str,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", required=True)
    config_path = Path(parser.parse_args(argv).config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    config = _load_config(config_path)
    source_root = _config_root(config)

    dotnet = os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        parser.error(".NET SDK not found; set CODE_TREE_DOTNET or install dotnet")
    environment = _extractor_environment(dotnet, source_root)

    build_returncode = ensure_worker_built(script_path, project_name, dotnet=dotnet)
    if build_returncode:
        return build_returncode

    assembly = worker_assembly_path(script_path, project_name)
    return subprocess.run(
        [dotnet, str(assembly), "--config", str(config_path)],
        env=environment,
    ).returncode


def _load_config(path: Path) -> dict[str, object]:
    try:
        value = json.loads(os.path.expandvars(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _config_root(config: dict[str, object]) -> Path | None:
    value = config.get("root")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_dir() else None


def dotnet_environment(dotnet: str, source_root: Path | None = None) -> dict[str, str]:
    """Environment for running a packaged .NET worker with ``dotnet``."""
    return _extractor_environment(dotnet, source_root)


def _extractor_environment(
    dotnet: str, source_root: Path | None
) -> dict[str, str]:
    resolved = Path(dotnet).expanduser().resolve()
    dotnet_root = resolved.parent if resolved.name.lower().startswith("dotnet") else None
    environment = os.environ | {"CODE_TREE_PYTHON": sys.executable}
    if dotnet_root is not None:
        environment.setdefault("DOTNET_ROOT", str(dotnet_root))
        environment.setdefault("DOTNET_HOST_PATH", str(resolved))
        environment.setdefault("DOTNET_MSBUILD_SDK_RESOLVER_CLI_DIR", str(dotnet_root))
        current_path = environment.get("PATH", "")
        environment["PATH"] = str(dotnet_root) + os.pathsep + current_path
    if source_root is not None:
        sdk_major = _selected_sdk_major(dotnet, source_root, environment)
        if sdk_major is not None and sdk_major > 9:
            # The packaged worker targets net9.0. Run it on the selected newer
            # runtime so MSBuildLocator can discover the source SDK.
            environment.setdefault("DOTNET_ROLL_FORWARD", "LatestMajor")
    return environment


def _selected_sdk_major(
    dotnet: str, source_root: Path, environment: dict[str, str]
) -> int | None:
    try:
        result = subprocess.run(
            [dotnet, "--version"],
            cwd=source_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    value = result.stdout.strip().split(".", 1)[0]
    try:
        return int(value)
    except ValueError:
        return None
