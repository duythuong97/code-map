from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_EXTRACTORS = {"oracle_plsql", "xml_sql", "csharp_sql"}
_OWNERS = {
    "oracle_plsql": {"callable_or_file"},
    "xml_sql": {"file"},
    "csharp_sql": {"repository_or_project"},
}
CSV_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "cp932", "shift_jis", "euc_jp"})


def load_json_config(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def load_app_config(path: str | Path) -> dict[str, Any]:
    config = load_json_config(path)
    if "api" not in config:
        return config
    app_config = dict(config["api"])
    app_config["db"] = config["db"]
    table_definition = config.get("extractors", {}).get("table_definition")
    if table_definition:
        app_config["table_definition"] = table_definition
    return app_config


def load_pipeline_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    config = load_json_config(config_path)
    _validate_pipeline_config(config, config_path.parent)
    return config


def load_extractor_config(path: str | Path) -> dict[str, Any]:
    config = load_pipeline_config(path)
    extractor_config = dict(config.get("extractors") or {})
    extractor_config["db"] = config["db"]
    return extractor_config


def load_import_config(path: str | Path) -> dict[str, Any]:
    config = load_pipeline_config(path)
    import_config = dict(config.get("imports") or {})
    import_config["db"] = config["db"]
    return import_config


def resolve_path(value: str | Path, base_dir: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_hash(config: dict[str, Any]) -> str:
    return stable_hash({"db": config.get("db"), "imports": config.get("imports"), "extractors": config.get("extractors")})


def source_rule_hash(source: dict[str, Any]) -> str:
    return stable_hash({key: source.get(key) for key in ("id", "path", "repo", "db_name", "schema", "priority", "exclude", "rules")})


def rule_context_hash(source: dict[str, Any], rule: dict[str, Any], project: dict[str, Any] | None = None) -> str:
    return stable_hash({
        "source": {key: source.get(key) for key in ("id", "repo", "db_name", "schema", "priority")},
        "rule": rule,
        "project": project or {},
    })


def _validate_pipeline_config(config: dict[str, Any], base_dir: Path) -> None:
    if not isinstance(config, dict):
        raise ValueError("Config must be an object")
    if not isinstance(config.get("db"), str) or not config["db"].strip():
        raise ValueError("Config requires non-empty string db")
    extractors = config.get("extractors", {})
    if extractors is None:
        extractors = {}
    if not isinstance(extractors, dict):
        raise ValueError("extractors must be an object")
    if "reset" in extractors:
        raise ValueError("extractors.reset is unsafe and no longer supported")
    if "table_definition" in extractors or "table_definitions_path" in extractors:
        raise ValueError("Move table metadata to top-level imports.csv")

    features = extractors.get("features", {})
    if features is None:
        features = {}
    if not isinstance(features, dict):
        raise ValueError("extractors.features must be an object")
    for name, enabled in features.items():
        if not isinstance(enabled, bool):
            raise ValueError(f"extractors.features.{name} must be a boolean")

    state = extractors.get("state", {})
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise ValueError("extractors.state must be an object")
    if "log_path" in state and (
        not isinstance(state["log_path"], str) or not state["log_path"].strip()
    ):
        raise ValueError("extractors.state.log_path must be a non-empty string")
    _validate_positive_number(
        state.get("lease_stale_seconds", 120),
        "extractors.state.lease_stale_seconds",
    )
    _validate_positive_number(
        state.get("heartbeat_seconds", 15),
        "extractors.state.heartbeat_seconds",
    )
    if state.get("heartbeat_seconds", 15) >= state.get("lease_stale_seconds", 120):
        raise ValueError(
            "extractors.state.heartbeat_seconds must be less than lease_stale_seconds"
        )
    _validate_nonnegative_integer(
        state.get("log_backups", 5), "extractors.state.log_backups"
    )
    _validate_positive_integer(
        state.get("log_max_bytes", 10 * 1024 * 1024),
        "extractors.state.log_max_bytes",
    )

    source_ids: set[str] = set()
    roots: set[str] = set()
    sources = extractors.get("sources", [])
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        raise ValueError("extractors.sources must be a list")
    for index, source in enumerate(sources):
        where = f"extractors.sources[{index}]"
        _validate_named_path(source, where)
        source_id = str(source["id"]).strip()
        if source_id in source_ids:
            raise ValueError(f"Duplicate source id: {source_id}")
        source_ids.add(source_id)
        root = str(resolve_path(source["path"], base_dir))
        if root in roots:
            raise ValueError(f"Duplicate source path: {source['path']}")
        roots.add(root)
        for legacy in ("table_definition", "table_definitions_path"):
            if legacy in source:
                raise ValueError(f"{where}.{legacy} is unsupported; use imports.csv")
        for key in ("repo", "db_name", "schema"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError(f"{where}.{key} must be a non-empty string")
        _validate_priority(source.get("priority", 0), f"{where}.priority")
        exclude = source.get("exclude", [])
        if exclude is None:
            exclude = []
        if not isinstance(exclude, list) or not all(isinstance(pattern, str) and pattern.strip() for pattern in exclude):
            raise ValueError(f"{where}.exclude must contain non-empty strings")
        rules = source.get("rules") or []
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"{where}.rules must be a non-empty list")
        for rule_index, rule in enumerate(rules):
            _validate_rule(rule, f"{where}.rules[{rule_index}]")

    imports = config.get("imports", {})
    if imports is None:
        imports = {}
    if not isinstance(imports, dict):
        raise ValueError("imports must be an object")
    csv_entries = imports.get("csv", [])
    if csv_entries is None:
        csv_entries = []
    if not isinstance(csv_entries, list):
        raise ValueError("imports.csv must be a list")
    import_ids: set[str] = set()
    import_roots: list[Path] = []
    for index, entry in enumerate(csv_entries):
        where = f"imports.csv[{index}]"
        _validate_named_path(entry, where)
        import_id = str(entry["id"]).strip()
        if import_id in import_ids or import_id in source_ids:
            raise ValueError(f"Duplicate source/import id: {import_id}")
        import_ids.add(import_id)
        if entry.get("kind") != "table_definitions":
            raise ValueError(f"{where}.kind must be table_definitions")
        for key in ("db_name", "schema"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"{where}.{key} must be a non-empty string")
        encoding = entry.get("encoding", "utf-8")
        if encoding not in CSV_ENCODINGS:
            raise ValueError(f"{where}.encoding is unsupported: {encoding}")
        _validate_priority(entry.get("priority", 0), f"{where}.priority")
        root = resolve_path(entry["path"], base_dir)
        if any(
            root == other or root in other.parents or other in root.parents
            for other in import_roots
        ):
            raise ValueError(f"Overlapping import path: {entry['path']}")
        import_roots.append(root)


def _validate_named_path(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    for key in ("id", "path"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"{where}.{key} must be a non-empty string")


def _validate_priority(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an integer")


def _validate_positive_number(value: Any, where: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{where} must be a finite positive number")


def _validate_positive_integer(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{where} must be a positive integer")


def _validate_nonnegative_integer(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where} must be a non-negative integer")


def _validate_rule(rule: Any, where: str) -> None:
    if not isinstance(rule, dict):
        raise ValueError(f"{where} must be an object")
    patterns = rule.get("patterns") or []
    if not isinstance(patterns, list) or not patterns or not all(isinstance(pattern, str) and pattern.strip() for pattern in patterns):
        raise ValueError(f"{where}.patterns must be a non-empty string list")
    extractor = rule.get("extractor")
    if extractor not in _EXTRACTORS:
        raise ValueError(f"{where}.extractor must be one of {sorted(_EXTRACTORS)}")
    owner = rule.get("owner")
    if owner not in _OWNERS[extractor]:
        raise ValueError(f"{where}.owner must be one of {sorted(_OWNERS[extractor])} for {extractor}")
