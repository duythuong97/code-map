"""Persist the ANTLR PL/SQL prediction DFA between processes.

The Python ANTLR runtime builds its prediction DFA lazily. For the grammars-v4
PL/SQL grammar the first parse in a fresh process costs 5-20 seconds even for a
one-line statement, while a parse that reuses an already built DFA takes well
under a second. Every extractor runs in its own subprocess, so without this
cache every run, and every extractor in a run, pays the warm-up again.

The cache is a pickle of the lexer/parser ATN together with their DFAs, keyed
by the generated grammar files, the ANTLR runtime version and the Python
version. Runtime singletons that ANTLR compares by identity are pickled by
reference so the restored DFA behaves exactly like one built in-process.

Set ``CODE_TREE_ANTLR_CACHE=0`` to disable it, or ``CODE_TREE_CACHE_DIR`` to
move it (default: the per-user cache directory). Each entry point (extractor
script, SQL bridge, pipeline) keeps its own file.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import pickle
import sys
import tempfile
import threading
from pathlib import Path

_FORMAT_VERSION = "1"
_STACK_BYTES = 512 * 1024 * 1024
_RECURSION_LIMIT = 1_000_000

_lock = threading.Lock()
_loaded = False
_states_at_load = 0


def ensure_loaded() -> None:
    """Restore the cached DFA once per process, before the first parser is built."""
    global _loaded, _states_at_load
    with _lock:
        if _loaded:
            return
        _loaded = True
        if not _enabled():
            return
        path = _cache_path()
        if path.is_file():
            try:
                state = _in_large_stack(lambda: _read(path))
            except Exception:
                state = None
            if state is not None:
                _install(state)
            else:
                path.unlink(missing_ok=True)
        _states_at_load = _dfa_state_count()
        atexit.register(save)


def save() -> None:
    """Write the DFA back when this process learned new prediction states."""
    if not _enabled() or _dfa_state_count() <= _states_at_load:
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _in_large_stack(lambda: _write(path))
    except Exception:
        # A cache write must never fail an extraction.
        return


def _enabled() -> bool:
    return os.environ.get("CODE_TREE_ANTLR_CACHE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _cache_dir() -> Path:
    configured = os.environ.get("CODE_TREE_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "code-tree" / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "code-tree"


def _cache_path() -> Path:
    return _cache_dir() / f"plsql-antlr-dfa-{_profile()}-{_cache_key()}.pickle"


def _profile() -> str:
    """One cache file per entry point. Extractors run in parallel and a DFA
    cannot be merged, so a shared file would let them overwrite each other's
    learned states on every run and never converge."""
    configured = os.environ.get("CODE_TREE_ANTLR_CACHE_PROFILE", "")
    if not configured:
        script = Path(sys.argv[0] or "python").resolve()
        configured = script.parent.name if script.stem in {"main", "__main__"} else script.stem
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in configured)[:40] or "default"


def _cache_key() -> str:
    import antlr4

    generated = Path(__file__).resolve().parent / "antlr_plsql_generated"
    digest = hashlib.sha256()
    digest.update(_FORMAT_VERSION.encode())
    digest.update(sys.version.split()[0].encode())
    digest.update(str(getattr(antlr4, "__version__", "")).encode())
    digest.update(_antlr_runtime_version().encode())
    for name in ("PlSqlLexer.py", "PlSqlParser.py"):
        digest.update((generated / name).read_bytes())
    return digest.hexdigest()[:20]


def _antlr_runtime_version() -> str:
    try:
        from importlib.metadata import version

        return version("antlr4-python3-runtime")
    except Exception:
        return ""


def _grammar_classes():
    from code_tree_exporter.extractors.package_support.antlr_plsql_generated.PlSqlLexer import (
        PlSqlLexer,
    )
    from code_tree_exporter.extractors.package_support.antlr_plsql_generated.PlSqlParser import (
        PlSqlParser,
    )

    return PlSqlLexer, PlSqlParser


def _singletons() -> dict[str, object]:
    from antlr4.PredictionContext import PredictionContext
    from antlr4.RuleContext import RuleContext
    from antlr4.atn.ATNSimulator import ATNSimulator
    from antlr4.atn.LexerATNSimulator import LexerATNSimulator
    from antlr4.atn.SemanticContext import SemanticContext

    return {
        "prediction_context_empty": PredictionContext.EMPTY,
        "semantic_context_none": SemanticContext.NONE,
        "atn_simulator_error": ATNSimulator.ERROR,
        "lexer_simulator_error": LexerATNSimulator.ERROR,
        "rule_context_empty": RuleContext.EMPTY,
    }


def _dfa_state_count() -> int:
    lexer, parser = _grammar_classes()
    return sum(len(dfa._states) for dfa in parser.decisionsToDFA) + sum(
        len(dfa._states) for dfa in lexer.decisionsToDFA
    )


def _install(state: tuple) -> None:
    lexer, parser = _grammar_classes()
    (
        parser.atn,
        parser.decisionsToDFA,
        parser.sharedContextCache,
        lexer.atn,
        lexer.decisionsToDFA,
    ) = state


def _read(path: Path):
    singletons = _singletons()

    class _Unpickler(pickle.Unpickler):
        def persistent_load(self, key):
            return singletons[key]

    with path.open("rb") as handle:
        return _Unpickler(handle).load()


def _write(path: Path) -> None:
    lexer, parser = _grammar_classes()
    by_identity = {id(value): key for key, value in _singletons().items()}

    class _Pickler(pickle.Pickler):
        def persistent_id(self, value):
            return by_identity.get(id(value))

    handle = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
    )
    try:
        with handle:
            _Pickler(handle, protocol=pickle.HIGHEST_PROTOCOL).dump(
                (
                    parser.atn,
                    parser.decisionsToDFA,
                    parser.sharedContextCache,
                    lexer.atn,
                    lexer.decisionsToDFA,
                )
            )
        # Atomic: concurrent extractors may race, the last complete file wins.
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _in_large_stack(action):
    """The ATN/DFA graph is deeply linked; (un)pickling it needs a deep stack."""
    result: dict[str, object] = {}

    def run() -> None:
        previous_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(previous_limit, _RECURSION_LIMIT))
        try:
            result["value"] = action()
        except BaseException as exc:  # re-raised in the calling thread
            result["error"] = exc
        finally:
            sys.setrecursionlimit(previous_limit)

    previous_stack = threading.stack_size()
    threading.stack_size(_STACK_BYTES)
    try:
        worker = threading.Thread(target=run, name="code-tree-antlr-cache")
        worker.start()
    finally:
        threading.stack_size(previous_stack)
    worker.join()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result.get("value")
