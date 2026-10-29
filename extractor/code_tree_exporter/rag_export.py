"""Write ``codemap-rag.sqlite``: the graph plus business data, shaped for Graph RAG.

The VS Code extension (``vscode-extension/``) answers questions by looking up
seed nodes in ``terms``/``concept_terms``, walking ``edges`` a few hops and
handing the facts (with file:line evidence) to the chat model. Everything it
needs is precomputed here so the extension only runs indexed lookups.

Business data comes from the ``inputData`` folder when present:

* ``semantic-dictionary.csv``  business terms, definitions and code aliases
* ``tables.csv`` / ``tables/<TABLE>.csv``  Japanese/English table and column names
* ``localized-metadata.csv``  extra localized names
* ``jobnet.csv`` / ``executable-mappings.csv``  batch job networks

``tokens`` must stay identical to ``tokens`` in
``vscode-extension/src/core/text.ts``; ``shared/fixtures/rag-token-vectors.json``
pins both to the same vectors.
"""
from __future__ import annotations

import csv
import io
import datetime as _dt
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

from .decoding import read_text_auto
from .fingerprints import node_fingerprints

from .serving_tables import add_business_edge, add_business_node, finish_serving_tables, write_serving_tables

RAG_SCHEMA_VERSION = "2"
DATABASE_NAME = "codemap-rag.sqlite"
WEB_DATABASE_NAME = "codemap-web.sqlite"
MAX_DOC_CHARS = 600
MAX_SNIPPET_CHARS = 240
MAX_DOC_TERMS = 40
MAX_LINKS_PER_ALIAS = 50

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE nodes(
  id INTEGER PRIMARY KEY, stable_id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, name TEXT NOT NULL,
  qualified_name TEXT NOT NULL, system TEXT, repository TEXT, file TEXT, line INTEGER,
  names TEXT NOT NULL DEFAULT '', doc TEXT NOT NULL DEFAULT '', database TEXT);
CREATE TABLE edges(
  id INTEGER PRIMARY KEY, src INTEGER NOT NULL, dst INTEGER NOT NULL, type TEXT NOT NULL,
  layer TEXT NOT NULL, operation TEXT NOT NULL, file TEXT, line INTEGER, snippet TEXT);
CREATE TABLE terms(term TEXT NOT NULL, node_id INTEGER NOT NULL, weight REAL NOT NULL);
CREATE TABLE concepts(
  id INTEGER PRIMARY KEY, semantic_id TEXT NOT NULL, locale TEXT NOT NULL, domain TEXT NOT NULL,
  label TEXT NOT NULL, definition TEXT NOT NULL, aliases TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE concept_terms(term TEXT NOT NULL, concept_id INTEGER NOT NULL, label_terms INTEGER NOT NULL);
CREATE TABLE concept_links(semantic_id TEXT NOT NULL, node_id INTEGER NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(semantic_id, node_id));
CREATE TABLE flows(id INTEGER PRIMARY KEY, type TEXT NOT NULL, start_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL, operation TEXT NOT NULL, path TEXT NOT NULL, complete INTEGER NOT NULL);
CREATE TABLE issues(id INTEGER PRIMARY KEY, node_id INTEGER, type TEXT NOT NULL, severity TEXT NOT NULL,
  message TEXT NOT NULL, file TEXT, line INTEGER);
"""
_INDEXES = """
CREATE INDEX edges_src ON edges(src);
CREATE INDEX edges_dst ON edges(dst);
CREATE INDEX terms_term ON terms(term);
CREATE INDEX terms_node ON terms(node_id);
CREATE INDEX nodes_type_name ON nodes(type, name);
CREATE INDEX nodes_file ON nodes(file);
CREATE INDEX concept_terms_term ON concept_terms(term);
CREATE INDEX concept_links_node ON concept_links(node_id);
CREATE INDEX flows_start ON flows(start_id);
CREATE INDEX flows_target ON flows(target_id);
CREATE INDEX issues_node ON issues(node_id);
"""
_STOPWORDS = frozenset(
    "a an and are as at be by do does for from how in is it of on or the this to what when where which who why with "
    "ai bi cac cho co cua da den duoc gi hay khi khong la ma mot nao nay nhu nhung o ra roi sao se thi tren trong tu va vao voi".split()
)
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT = re.compile(r"[\W_]+")
_HTTP_ALIAS = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)$", re.IGNORECASE)
_MODE_ARGUMENT = re.compile(r"--mode[=\s]+(\S+)")


def fold(text: str) -> str:
    """Lower-case and strip diacritics: ``Hủy đơn`` -> ``huy don``."""
    text = str(text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("M")).lower()


def tokens(text: str) -> list[str]:
    """Search tokens: camelCase/snake_case split, folded, CJK as character bigrams."""
    result: list[str] = []
    for word in _SPLIT.split(fold(_CAMEL.sub(" ", str(text or "")))):
        if not word:
            continue
        position = 0
        for match in _CJK.finditer(word):
            _plain(word[position:match.start()], result)
            run = match.group()
            result.extend([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
            position = match.end()
        _plain(word[position:], result)
    return result


def _plain(word: str, result: list[str]) -> None:
    if len(word) >= 2 and word not in _STOPWORDS:
        result.append(word)


def compact(text: str) -> str:
    return "".join(ch for ch in fold(text) if ch.isalnum())


def write_rag_database(
    graph_path: Path,
    output_path: Path,
    *,
    web_path: Path | None = None,
    input_root: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, int]:
    """Build ``output_path`` (VS Code) and ``web_path`` (web app, default
    ``codemap-web.sqlite`` next to it) from the engine ``graph.sqlite``.

    They are separate files because the web tables use long string ids and
    full evidence; folded into one file the extension would load ~6x more.
    """
    web_path = web_path or output_path.with_name(WEB_DATABASE_NAME)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    web_temporary = web_path.with_name(web_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    web_temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(f"{graph_path.resolve().as_uri()}?mode=ro", uri=True)) as graph, closing(
        sqlite3.connect(temporary)
    ) as rag, closing(sqlite3.connect(web_temporary)) as web:
        graph.row_factory = sqlite3.Row
        rag.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + _SCHEMA)
        web.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        with rag, web:
            ids = _copy_nodes(graph, rag)
            _copy_edges(graph, rag, ids)
            _copy_flows(graph, rag, ids)
            _copy_issues(graph, rag, ids)
            business = _BusinessData(input_root)
            write_serving_tables(graph, web, business.root)
            _names_from_localization(rag, web)
            _add_jobs(rag, web, business)
            _index_terms(rag)
            _add_concepts(rag, business)
            _concept_links_for_web(rag, web)
            _knowledge_inputs(graph, web, business)
            engine = _graph_metadata(graph)
            metadata = {
                "schema_version": RAG_SCHEMA_VERSION,
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "graph_generated_at": engine.get("generated_at", ""),
                "source_name": engine.get("source_name", ""),
                "source_root": str(source_root or engine.get("source_root") or ""),
                "input_data": str(input_root or ""),
                # {source: {commit, branch, dirty, ...}} as JSON: how old the graph is.
                "source_revisions": json.dumps(engine.get("source_revisions") or {}, ensure_ascii=False, sort_keys=True),
            }
            rag.executemany("INSERT INTO meta VALUES(?,?)", [(k, str(v)) for k, v in metadata.items()])
            web.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            web.executemany("INSERT INTO meta VALUES(?,?)", [(k, str(v)) for k, v in metadata.items()])
        finish_serving_tables(web)
        rag.executescript(_INDEXES + "ANALYZE;")
        counts = {
            table: rag.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("nodes", "edges", "terms", "concepts", "concept_links", "flows", "issues")
        }
    temporary.replace(output_path)
    web_temporary.replace(web_path)
    return counts


def _graph_metadata(graph: sqlite3.Connection) -> dict[str, object]:
    return {key: json.loads(value) for key, value in graph.execute("SELECT key, value_json FROM metadata")}


def _copy_nodes(graph: sqlite3.Connection, rag: sqlite3.Connection) -> dict[int, int]:
    """Copy nodes with small sequential ids (engine ids are 63-bit; JavaScript numbers are not)."""
    declarations: dict[int, tuple[str, int]] = {}
    for target, path, line in graph.execute(
        "SELECT target_id, source_path, min(start_line) FROM evidence WHERE target_type='NODE' "
        "AND evidence_kind <> 'CATALOG_ROW' GROUP BY target_id"
    ):
        declarations[target] = (path, line)
    docs: dict[int, list[str]] = {}
    for owner, text in graph.execute(
        "SELECT owner_node_id, normalized_text FROM comments WHERE owner_node_id IS NOT NULL ORDER BY start_line"
    ):
        docs.setdefault(owner, []).append(str(text or "").strip())
    ids: dict[int, int] = {}
    rows = []
    for row in graph.execute(
        "SELECT node_id, stable_id, node_type, technical_name, qualified_name, system_key, repository_key, properties_json, database_key FROM nodes"
    ):
        node_id = row["node_id"]
        ids[node_id] = len(ids) + 1
        path, line = declarations.get(node_id, (None, None))
        if path is None:
            properties = _json(row["properties_json"])
            path = properties.get("source_path") or properties.get("path") or None
        doc = " ".join(text for text in docs.get(node_id, []) if text)[:MAX_DOC_CHARS]
        rows.append((
            ids[node_id], row["stable_id"], row["node_type"], row["technical_name"] or "", row["qualified_name"] or "",
            row["system_key"], row["repository_key"], path, line, "", doc, row["database_key"],
        ))
    rag.executemany("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return ids


def _copy_edges(graph: sqlite3.Connection, rag: sqlite3.Connection, ids: dict[int, int]) -> None:
    evidence: dict[int, tuple[str, int, str]] = {}
    for target, path, line, snippet in graph.execute(
        "SELECT target_id, source_path, start_line, snippet FROM evidence WHERE target_type='EDGE' ORDER BY target_id, start_line"
    ):
        evidence.setdefault(target, (path, line, str(snippet or "")[:MAX_SNIPPET_CHARS]))
    rows = []
    for edge_id, src, dst, edge_type, layer, operation in graph.execute(
        "SELECT edge_id, source_node_id, target_node_id, edge_type, graph_layer, raw_operation FROM edges ORDER BY edge_id"
    ):
        if src in ids and dst in ids:
            path, line, snippet = evidence.get(edge_id, (None, None, None))
            rows.append((len(rows) + 1, ids[src], ids[dst], edge_type, layer or "", operation or "", path, line, snippet))
    rag.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?)", rows)


def _copy_flows(graph: sqlite3.Connection, rag: sqlite3.Connection, ids: dict[int, int]) -> None:
    steps: dict[int, list[tuple[int, int | None]]] = {}
    for flow_id, node_id, incoming in graph.execute(
        "SELECT flow_id, node_id, incoming_edge_id FROM flow_steps ORDER BY flow_id, step_index"
    ):
        steps.setdefault(flow_id, []).append((node_id, incoming))
    operations = {
        edge_id: (operation or edge_type)
        for edge_id, edge_type, operation in graph.execute("SELECT edge_id, edge_type, raw_operation FROM edges")
    }
    rows = []
    for flow_id, flow_type, start, target, complete in graph.execute(
        "SELECT flow_id, flow_type, start_node_id, target_node_id, complete FROM flows ORDER BY flow_id"
    ):
        path = steps.get(flow_id, [])
        if start not in ids or target not in ids or not path or any(node not in ids for node, _ in path):
            continue
        last_edge = path[-1][1]
        operation = str(operations.get(last_edge, "") or "")
        rows.append((
            len(rows) + 1, flow_type, ids[start], ids[target], operation,
            json.dumps([ids[node] for node, _ in path]), int(complete or 0),
        ))
    rag.executemany("INSERT INTO flows VALUES(?,?,?,?,?,?,?)", rows)


def _copy_issues(graph: sqlite3.Connection, rag: sqlite3.Connection, ids: dict[int, int]) -> None:
    rag.executemany(
        "INSERT INTO issues(node_id,type,severity,message,file,line) VALUES(?,?,?,?,?,?)",
        [
            (ids.get(node), issue_type, severity, message or "", path, line)
            for node, issue_type, severity, message, path, line in graph.execute(
                "SELECT source_node_id, issue_type, severity, message, source_path, start_line FROM issues ORDER BY issue_id"
            )
        ],
    )


class _BusinessData:
    def __init__(self, root: Path | None) -> None:
        self.root = root if root is not None and root.is_dir() else None

    def rows(self, relative: str) -> list[dict[str, str]]:
        if self.root is None or not (self.root / relative).is_file():
            return []
        text = read_text_auto(self.root / relative)
        return [{key: (value or "").strip() for key, value in row.items() if key} for row in csv.DictReader(io.StringIO(text, newline=""))]


def _names_from_localization(rag: sqlite3.Connection, web: sqlite3.Connection) -> None:
    """nodes.names ("ja:注文; en:Order Header") from graph_localization, for the search index."""
    order = {"ja": 0, "en": 1, "vi": 2}
    names: dict[str, list[tuple[int, str]]] = {}
    for target, locale, value in web.execute(
        "SELECT target_id, locale, value FROM graph_localization WHERE field_name='name' AND lower(review_status) <> 'rejected'"
    ):
        names.setdefault(target, []).append((order.get(locale, 9), f"{locale}:{value}"))
    rag.executemany(
        "UPDATE nodes SET names=? WHERE stable_id=?",
        [("; ".join(value for _, value in sorted(values)), target) for target, values in names.items()],
    )


def _add_jobs(rag: sqlite3.Connection, web: sqlite3.Connection, business: _BusinessData) -> None:
    """Add JOB_NETWORK/JOB nodes from jobnet.csv and link jobs to the code they start."""
    jobs = business.rows("jobnet.csv")
    if not jobs:
        return
    aliases: dict[str, set[str]] = {}
    for row in business.rows("executable-mappings.csv"):
        names = {compact(Path(row.get(key, "")).stem) for key in ("executable_name", "canonical_executable_name", "alias")}
        for name in names:
            aliases.setdefault(name, set()).update(names - {""})
    executables: dict[str, list[int]] = {}
    for node_id, name, stable_id in rag.execute("SELECT id, name, stable_id FROM nodes WHERE type='EXECUTABLE'"):
        for key in {compact(Path(name).stem), compact(stable_id.rsplit(":", 1)[-1].rsplit(".", 1)[0])}:
            executables.setdefault(key, []).append(node_id)
    modes: dict[tuple[str, str], int] = {}
    for node_id, name, stable_id in rag.execute("SELECT id, name, stable_id FROM nodes WHERE type='COMMAND_MODE'"):
        parts = stable_id.split(":")
        if len(parts) >= 3:
            modes[(compact(parts[1]), compact(name))] = node_id
    next_id = (rag.execute("SELECT max(id) FROM nodes").fetchone()[0] or 0) + 1
    next_edge = (rag.execute("SELECT max(id) FROM edges").fetchone()[0] or 0) + 1
    next_issue = (rag.execute("SELECT max(id) FROM issues").fetchone()[0] or 0) + 1
    created: dict[str, int] = {}

    stable = {node_id: stable_id for node_id, stable_id in rag.execute("SELECT id, stable_id FROM nodes")}

    def node(stable_id: str, node_type: str, name: str, doc: str = "", properties: dict | None = None) -> int:
        nonlocal next_id
        if stable_id not in created:
            created[stable_id] = next_id
            stable[next_id] = stable_id
            rag.execute(
                "INSERT INTO nodes(id,stable_id,type,name,qualified_name,file,line,doc) VALUES(?,?,?,?,?,?,?,?)",
                (next_id, stable_id, node_type, name, stable_id, "jobnet.csv", None, doc),
            )
            add_business_node(web, stable_id, node_type, name, properties or {})
            next_id += 1
        return created[stable_id]

    def edge(src: int, dst: int, edge_type: str, operation: str = "") -> None:
        nonlocal next_edge
        rag.execute(
            "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?)",
            (next_edge, src, dst, edge_type, "BUSINESS", operation, "jobnet.csv", None, None),
        )
        # The web vocabulary has no INVOKES; a job running a command mode is a CALLS there.
        add_business_edge(web, stable[src], stable[dst], "CALLS" if edge_type == "INVOKES" else edge_type, operation)
        next_edge += 1

    job_ids: dict[tuple[str, str], int] = {}
    for row in jobs:
        if not (row.get("jobnet_id") or "").strip() or not (row.get("job_id") or "").strip():
            # A row without ids (or a file with other headers) is reported, not allowed to abort the extract.
            rag.execute(
                "INSERT INTO issues VALUES(?,?,?,?,?,?,?)",
                (next_issue, None, "INVALID_INPUT_ROW", "WARNING", "jobnet.csv row without jobnet_id/job_id was skipped", "jobnet.csv", None),
            )
            next_issue += 1
            continue
        network = node(f"job-network:{row['jobnet_id']}", "JOB_NETWORK", row.get("jobnet_name") or row["jobnet_id"])
        doc = f"{row.get('executable_name', '')} {row.get('arguments', '')}".strip()
        job = node(f"job:{row['jobnet_id']}:{row['job_id']}", "JOB", row.get("job_name") or row["job_id"], doc, row)
        job_ids[(row["jobnet_id"], row["job_id"])] = job
        edge(network, job, "CONTAINS")
        executable = compact(Path(row.get("executable_name", "")).stem)
        candidates = sorted({target for key in aliases.get(executable, {executable}) for target in executables.get(key, [])})
        mode_match = _MODE_ARGUMENT.search(row.get("arguments", ""))
        mode = next(
            (
                modes[(key, compact(mode_match.group(1)))]
                for key in sorted(aliases.get(executable, {executable}))
                if mode_match and (key, compact(mode_match.group(1))) in modes
            ),
            None,
        )
        for target in candidates:
            edge(job, target, "STARTS", row.get("arguments", ""))
        if mode is not None:
            edge(job, mode, "INVOKES", row.get("arguments", ""))
        if not candidates:
            message = f"Job {row['job_id']} runs {row.get('executable_name')} but no scanned executable matches it"
            rag.execute(
                "INSERT INTO issues VALUES(?,?,?,?,?,?,?)",
                (next_issue, job, "JOB_EXECUTABLE_NOT_FOUND", "WARNING", message, "jobnet.csv", None),
            )
            web.execute(
                "INSERT OR IGNORE INTO graph_issues VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"issue:job-executable:{row['jobnet_id']}:{row['job_id']}", "JOB_EXECUTABLE_NOT_FOUND", "WARNING",
                 stable[job], row.get("executable_name"), None, "jobnet.csv", None, message, "{}", "code-tree:business"),
            )
            next_issue += 1
    for row in jobs:
        current = job_ids.get((row.get("jobnet_id"), row.get("job_id")))
        predecessor = job_ids.get((row.get("jobnet_id"), row.get("predecessor_job_id", "")))
        if current is not None and predecessor is not None:
            edge(current, predecessor, "DEPENDS_ON")


def _index_terms(rag: sqlite3.Connection) -> None:
    rows = []
    for node_id, node_type, name, qualified, names, doc in rag.execute(
        "SELECT id, type, name, qualified_name, names, doc FROM nodes"
    ):
        weights: dict[str, float] = {}
        for text, weight in ((name, 1.0), (names, 1.0), (_qualified_tail(qualified), 0.5), (node_type, 0.4)):
            for token in tokens(text):
                weights[token] = max(weights.get(token, 0.0), weight)
        for token in dict.fromkeys(tokens(doc)).keys():
            if len(weights) >= MAX_DOC_TERMS + 20:
                break
            weights.setdefault(token, 0.3)
        whole = compact(name)
        if len(whole) >= 3:
            weights[whole] = max(weights.get(whole, 0.0), 1.5)
        rows.extend((term, node_id, weight) for term, weight in weights.items())
    rag.executemany("INSERT INTO terms VALUES(?,?,?)", rows)


def _qualified_tail(qualified: str) -> str:
    parts = [part for part in re.split(r"[./\\:]", qualified) if part]
    return " ".join(parts[-3:-1])


def _add_concepts(rag: sqlite3.Connection, business: _BusinessData) -> None:
    rows = business.rows("semantic-dictionary.csv")
    if not rows:
        return
    by_compact: dict[str, list[int]] = {}
    apis: list[tuple[int, str, list[str]]] = []
    for node_id, node_type, name in rag.execute("SELECT id, type, name FROM nodes"):
        by_compact.setdefault(compact(name), []).append(node_id)
        if node_type == "API_OPERATION" and " " in name:
            method, route = name.split(" ", 1)
            segments = [compact(part) for part in route.split("/") if part and not part.startswith("{")]
            apis.append((node_id, method.upper(), segments))
    links: dict[tuple[str, int], str] = {}
    for concept_id, row in enumerate(rows, start=1):
        aliases = [alias.strip() for alias in row.get("aliases", "").split("|") if alias.strip()]
        rag.execute(
            "INSERT INTO concepts VALUES(?,?,?,?,?,?,?,?)",
            (concept_id, row.get("semantic_id", ""), row.get("locale", ""), row.get("domain", ""), row.get("label", ""),
             row.get("definition", ""), json.dumps(aliases, ensure_ascii=False), row.get("status", "approved")),
        )
        # Labels match word by word; aliases only as a whole ("cancelOrder" == "cancel order").
        label_terms = list(dict.fromkeys(tokens(row.get("label", ""))))
        searchable = set(label_terms)
        searchable.update(compact(alias) for alias in aliases if len(compact(alias)) >= 3)
        rag.executemany(
            "INSERT INTO concept_terms VALUES(?,?,?)",
            [(term, concept_id, 1 if term in label_terms else 0) for term in sorted(searchable)],
        )
        semantic_id = row.get("semantic_id", "")
        for alias in aliases:
            http = _HTTP_ALIAS.match(alias)
            if http:
                word = compact(http.group(2))
                matches = [
                    node_id for node_id, method, segments in apis
                    if method == http.group(1).upper() and segments and segments[-1] in {word, word + "s", word.rstrip("s")}
                ]
                reason = f"api alias '{alias}'"
            else:
                matches = by_compact.get(compact(alias), [])
                reason = f"alias '{alias}'"
            for node_id in matches[:MAX_LINKS_PER_ALIAS]:
                links.setdefault((semantic_id, node_id), reason)
    rag.executemany(
        "INSERT INTO concept_links VALUES(?,?,?)",
        [(semantic_id, node_id, reason) for (semantic_id, node_id), reason in sorted(links.items())],
    )


def _concept_links_for_web(rag: sqlite3.Connection, web: sqlite3.Connection) -> None:
    """Dictionary alias links (business term -> code) with the web's stable ids."""
    web.execute(
        "CREATE TABLE graph_business_semantic_links(target_id TEXT NOT NULL, semantic_id TEXT NOT NULL, reason TEXT NOT NULL, "
        "PRIMARY KEY(target_id, semantic_id))"
    )
    web.executemany(
        "INSERT OR IGNORE INTO graph_business_semantic_links VALUES(?,?,?)",
        rag.execute("SELECT n.stable_id, l.semantic_id, l.reason FROM concept_links l JOIN nodes n ON n.id=l.node_id"),
    )


def _knowledge_inputs(graph: sqlite3.Connection, web: sqlite3.Connection, business: _BusinessData) -> None:
    """Node fingerprints (to send approved rules back to review when code changes), system owners
    (``owners.csv``) and operational facts (``ops-facts.csv``: batch windows, volumes, SLAs)."""
    web.executescript(
        """
        CREATE TABLE graph_fingerprints(node_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        CREATE TABLE graph_owners(system TEXT PRIMARY KEY, team TEXT NOT NULL, owner TEXT NOT NULL,
          contact TEXT NOT NULL, notes TEXT NOT NULL);
        CREATE TABLE graph_ops_facts(fact_id INTEGER PRIMARY KEY, target TEXT NOT NULL, node_id TEXT, kind TEXT NOT NULL,
          value TEXT NOT NULL, note TEXT NOT NULL);
        CREATE INDEX graph_ops_facts_node ON graph_ops_facts(node_id);
        """
    )
    web.executemany("INSERT INTO graph_fingerprints VALUES(?,?)", sorted(node_fingerprints(graph).items()))
    owners = {}
    for row in business.rows("owners.csv"):
        system = row.get("system", "")
        if system:
            owners[system] = (system, row.get("team", ""), row.get("owner", ""), row.get("contact", ""), row.get("notes", ""))
    web.executemany("INSERT INTO graph_owners VALUES(?,?,?,?,?)", sorted(owners.values()))
    by_stable = {node_id for (node_id,) in web.execute("SELECT node_id FROM graph_nodes")}
    by_name: dict[str, list[str]] = {}
    for node_id, name in web.execute("SELECT node_id, technical_name FROM graph_nodes WHERE node_type <> 'COLUMN'"):
        by_name.setdefault(compact(name), []).append(node_id)
    facts = []
    for row in business.rows("ops-facts.csv"):
        target = row.get("target", "")
        if not target or not row.get("kind"):
            continue
        matches = [target] if target in by_stable else by_name.get(compact(target), [])
        facts.append((target, matches[0] if len(matches) == 1 else None, row["kind"], row.get("value", ""), row.get("note", "")))
    web.executemany("INSERT INTO graph_ops_facts(target,node_id,kind,value,note) VALUES(?,?,?,?,?)", facts)


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
