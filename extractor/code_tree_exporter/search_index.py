"""The ``search_*`` tables of ``codemap.sqlite``: an index over its ``graph_*`` tables.

The MCP server answers questions by looking up seed nodes in ``search_terms``
/ ``search_concept_terms``, walking ``search_edges`` a few hops and handing the
facts (with file:line evidence) to the agent. Every row here is derived from a
``graph_*`` row of the same file (same node ids, same serving edge names), so
the web app and the MCP server can never disagree about what the graph holds;
the index only adds small integer ids, search terms and declaration lines.

``testdata/search-token-vectors.json`` pins what ``tokens`` returns.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Iterable

from .contract.vocabulary import serving_edge_type

MAX_DOC_CHARS = 600
MAX_SNIPPET_CHARS = 240
MAX_DOC_TERMS = 40
MAX_LINKS_PER_ALIAS = 50
BUSINESS_SOURCE = "code-tree:business"

SCHEMA = """
CREATE TABLE search_nodes(
  id INTEGER PRIMARY KEY, stable_id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, name TEXT NOT NULL,
  qualified_name TEXT NOT NULL, system TEXT, repository TEXT, file TEXT, line INTEGER,
  names TEXT NOT NULL DEFAULT '', doc TEXT NOT NULL DEFAULT '', database TEXT);
CREATE TABLE search_edges(
  id INTEGER PRIMARY KEY, edge_id TEXT NOT NULL, src INTEGER NOT NULL, dst INTEGER NOT NULL, type TEXT NOT NULL,
  layer TEXT NOT NULL, operation TEXT NOT NULL, file TEXT, line INTEGER, snippet TEXT);
CREATE TABLE search_terms(term TEXT NOT NULL, node_id INTEGER NOT NULL, weight REAL NOT NULL);
CREATE TABLE search_concepts(
  id INTEGER PRIMARY KEY, semantic_id TEXT NOT NULL, locale TEXT NOT NULL, domain TEXT NOT NULL,
  label TEXT NOT NULL, definition TEXT NOT NULL, aliases TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE search_concept_terms(term TEXT NOT NULL, concept_id INTEGER NOT NULL, label_terms INTEGER NOT NULL);
CREATE TABLE search_concept_links(semantic_id TEXT NOT NULL, node_id INTEGER NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(semantic_id, node_id));
CREATE TABLE search_flows(id INTEGER PRIMARY KEY, type TEXT NOT NULL, start_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL, operation TEXT NOT NULL, path TEXT NOT NULL, complete INTEGER NOT NULL);
CREATE TABLE search_issues(id INTEGER PRIMARY KEY, node_id INTEGER, type TEXT NOT NULL, severity TEXT NOT NULL,
  message TEXT NOT NULL, file TEXT, line INTEGER);
"""
INDEXES = """
CREATE INDEX search_edges_src ON search_edges(src);
CREATE INDEX search_edges_dst ON search_edges(dst);
CREATE INDEX search_terms_term ON search_terms(term);
CREATE INDEX search_terms_node ON search_terms(node_id);
CREATE INDEX search_nodes_type_name ON search_nodes(type, name);
CREATE INDEX search_nodes_file ON search_nodes(file);
CREATE INDEX search_concept_terms_term ON search_concept_terms(term);
CREATE INDEX search_concept_links_node ON search_concept_links(node_id);
CREATE INDEX search_flows_start ON search_flows(start_id);
CREATE INDEX search_flows_target ON search_flows(target_id);
CREATE INDEX search_issues_node ON search_issues(node_id);
"""
_STOPWORDS = frozenset(
    "a an and are as at be by do does for from how in is it of on or the this to what when where which who why with "
    "ai bi cac cho co cua da den duoc gi hay khi khong la ma mot nao nay nhu nhung o ra roi sao se thi tren trong tu va vao voi".split()
)
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT = re.compile(r"[\W_]+")
_HTTP_ALIAS = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)$", re.IGNORECASE)


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


def write_search_tables(
    graph: sqlite3.Connection,
    db: sqlite3.Connection,
    node_ids: dict[int, str],
    business_nodes: dict[str, tuple[str, str]],
) -> None:
    """Index the ``graph_*`` tables already in ``db``.

    ``graph`` (graph.sqlite) is only read for what the serving tables leave
    out: source comments and the node path of each engine flow. ``node_ids``
    maps engine node ids to stable ids; ``business_nodes`` gives the file and
    doc of the nodes added from inputData (jobnet.csv).
    """
    db.executescript(SCHEMA)
    ids = _nodes(graph, db, node_ids, business_nodes)
    _edges(db, ids)
    _flows(graph, db, node_ids, ids)
    db.execute(
        "INSERT INTO search_issues(node_id,type,severity,message,file,line) "
        "SELECT n.id, i.issue_type, i.severity, i.message, i.source_path, i.start_line FROM graph_issues i "
        "LEFT JOIN search_nodes n ON n.stable_id=i.source_node_id ORDER BY i.issue_id"
    )
    _names_from_localization(db)
    _index_terms(db)
    _concepts(db)


def finish_search_tables(db: sqlite3.Connection) -> None:
    db.executescript(INDEXES)


def _nodes(
    graph: sqlite3.Connection, db: sqlite3.Connection, node_ids: dict[int, str], business: dict[str, tuple[str, str]]
) -> dict[str, int]:
    """One row per graph_nodes row, with small sequential ids (JavaScript numbers stay exact)."""
    declarations: dict[str, tuple[str, int]] = {}
    for target, path, line in db.execute(
        "SELECT target_id, source_path, min(start_line) FROM graph_evidence WHERE target_type='NODE' "
        "AND evidence_kind <> 'CATALOG_ROW' GROUP BY target_id"
    ):
        declarations[target] = (path, line)
    docs: dict[str, list[str]] = {}
    for owner, text in graph.execute(
        "SELECT owner_node_id, normalized_text FROM comments WHERE owner_node_id IS NOT NULL ORDER BY start_line"
    ):
        if owner in node_ids:
            docs.setdefault(node_ids[owner], []).append(str(text or "").strip())
    ids: dict[str, int] = {}
    rows = []
    for stable_id, node_type, name, qualified, system, repository, database, properties in db.execute(
        "SELECT node_id, node_type, technical_name, qualified_name, system_key, repository_key, database_key, "
        "properties_json FROM graph_nodes ORDER BY rowid"
    ):
        ids[stable_id] = len(ids) + 1
        path, line = declarations.get(stable_id, (None, None))
        doc = " ".join(text for text in docs.get(stable_id, []) if text)
        if stable_id in business:
            path, doc = business[stable_id]
        elif path is None:
            parsed = _json(properties)
            path = parsed.get("source_path") or parsed.get("path") or None
        rows.append((ids[stable_id], stable_id, node_type, name or "", qualified or "", system, repository, path, line,
                     "", doc[:MAX_DOC_CHARS], database))
    db.executemany("INSERT INTO search_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return ids


def _edges(db: sqlite3.Connection, ids: dict[str, int]) -> None:
    """graph_edges without the derived actor -> table shortcuts (those are search_flows)."""
    evidence: dict[str, tuple[str, int, str]] = {}
    for target, path, line, snippet in db.execute(
        "SELECT target_id, source_path, start_line, snippet FROM graph_evidence WHERE target_type='EDGE' "
        "ORDER BY target_id, start_line"
    ):
        evidence.setdefault(target, (path, line, str(snippet or "")[:MAX_SNIPPET_CHARS]))
    rows = []
    for edge_id, src, dst, edge_type, layer, operation, source in db.execute(
        "SELECT edge_id, source_node_id, target_node_id, edge_type, graph_layer, raw_operation, source_id "
        "FROM graph_edges WHERE graph_layer <> 'DATA_FLOW' ORDER BY rowid"
    ):
        if src in ids and dst in ids:
            path, line, snippet = evidence.get(edge_id, (None, None, None))
            rows.append((len(rows) + 1, edge_id, ids[src], ids[dst], edge_type,
                         "BUSINESS" if source == BUSINESS_SOURCE else layer or "", operation or "", path, line, snippet))
    db.executemany("INSERT INTO search_edges VALUES(?,?,?,?,?,?,?,?,?,?)", rows)


def _flows(graph: sqlite3.Connection, db: sqlite3.Connection, node_ids: dict[int, str], ids: dict[str, int]) -> None:
    """Every engine flow (complete or not) as a node path; graph_paths holds the complete read/write ones."""
    steps: dict[int, list[tuple[int, int | None]]] = {}
    for flow_id, node_id, incoming in graph.execute(
        "SELECT flow_id, node_id, incoming_edge_id FROM flow_steps ORDER BY flow_id, step_index"
    ):
        steps.setdefault(flow_id, []).append((node_id, incoming))
    types = dict(graph.execute("SELECT node_id, node_type FROM nodes"))
    operations = {
        edge_id: operation or serving_edge_type(edge_type, operation or "", types.get(target, ""))
        for edge_id, edge_type, operation, target in graph.execute(
            "SELECT edge_id, edge_type, raw_operation, target_node_id FROM edges"
        )
    }

    def search_id(engine_id: int) -> int | None:
        return ids.get(node_ids.get(engine_id, ""))

    rows = []
    for flow_id, flow_type, start, target, complete in graph.execute(
        "SELECT flow_id, flow_type, start_node_id, target_node_id, complete FROM flows ORDER BY flow_id"
    ):
        path = steps.get(flow_id, [])
        nodes = [search_id(node) for node, _ in path]
        if search_id(start) is None or search_id(target) is None or not nodes or None in nodes:
            continue
        rows.append((len(rows) + 1, flow_type, search_id(start), search_id(target),
                     str(operations.get(path[-1][1], "") or ""), json.dumps(nodes), int(complete or 0)))
    db.executemany("INSERT INTO search_flows VALUES(?,?,?,?,?,?,?)", rows)


def _names_from_localization(db: sqlite3.Connection) -> None:
    """search_nodes.names ("ja:注文; en:Order Header") from graph_localization."""
    order = {"ja": 0, "en": 1, "vi": 2}
    names: dict[str, list[tuple[int, str]]] = {}
    for target, locale, value in db.execute(
        "SELECT target_id, locale, value FROM graph_localization WHERE field_name='name' AND lower(review_status) <> 'rejected'"
    ):
        names.setdefault(target, []).append((order.get(locale, 9), f"{locale}:{value}"))
    db.executemany(
        "UPDATE search_nodes SET names=? WHERE stable_id=?",
        [("; ".join(value for _, value in sorted(values)), target) for target, values in names.items()],
    )


def _index_terms(db: sqlite3.Connection) -> None:
    rows = []
    for node_id, node_type, name, qualified, names, doc in db.execute(
        "SELECT id, type, name, qualified_name, names, doc FROM search_nodes"
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
    db.executemany("INSERT INTO search_terms VALUES(?,?,?)", rows)


def _qualified_tail(qualified: str) -> str:
    parts = [part for part in re.split(r"[./\\:]", qualified) if part]
    return " ".join(parts[-3:-1])


def _concepts(db: sqlite3.Connection) -> None:
    """Business dictionary (graph_business_semantics) and its alias links to code.

    The links are written twice: by search id for the MCP server and as
    ``graph_business_semantic_links`` (stable ids) for the web app.
    """
    by_compact: dict[str, list[int]] = {}
    apis: list[tuple[int, str, list[str]]] = []
    for node_id, node_type, name in db.execute("SELECT id, type, name FROM search_nodes"):
        by_compact.setdefault(compact(name), []).append(node_id)
        if node_type == "API_OPERATION" and " " in name:
            method, route = name.split(" ", 1)
            segments = [compact(part) for part in route.split("/") if part and not part.startswith("{")]
            apis.append((node_id, method.upper(), segments))
    links: dict[tuple[str, int], str] = {}
    rows = db.execute(
        "SELECT semantic_id, locale, domain, label, definition, aliases_json, status FROM graph_business_semantics "
        "ORDER BY rowid"
    ).fetchall()
    for concept_id, (semantic_id, locale, domain, label, definition, aliases_json, status) in enumerate(rows, start=1):
        aliases = [str(alias) for alias in json.loads(aliases_json or "[]")]
        db.execute(
            "INSERT INTO search_concepts VALUES(?,?,?,?,?,?,?,?)",
            (concept_id, semantic_id, locale, domain, label, definition, json.dumps(aliases, ensure_ascii=False), status),
        )
        # Labels match word by word; aliases only as a whole ("cancelOrder" == "cancel order").
        label_terms = list(dict.fromkeys(tokens(label)))
        searchable = set(label_terms)
        searchable.update(compact(alias) for alias in aliases if len(compact(alias)) >= 3)
        db.executemany(
            "INSERT INTO search_concept_terms VALUES(?,?,?)",
            [(term, concept_id, 1 if term in label_terms else 0) for term in sorted(searchable)],
        )
        for alias in aliases:
            http = _HTTP_ALIAS.match(alias)
            if http:
                word = compact(http.group(2))
                matches: Iterable[int] = [
                    node_id for node_id, method, segments in apis
                    if method == http.group(1).upper() and segments and segments[-1] in {word, word + "s", word.rstrip("s")}
                ]
                reason = f"api alias '{alias}'"
            else:
                matches = by_compact.get(compact(alias), [])
                reason = f"alias '{alias}'"
            for node_id in list(matches)[:MAX_LINKS_PER_ALIAS]:
                links.setdefault((semantic_id, node_id), reason)
    db.executemany(
        "INSERT INTO search_concept_links VALUES(?,?,?)",
        [(semantic_id, node_id, reason) for (semantic_id, node_id), reason in sorted(links.items())],
    )
    db.execute(
        "CREATE TABLE graph_business_semantic_links(target_id TEXT NOT NULL, semantic_id TEXT NOT NULL, reason TEXT NOT NULL, "
        "PRIMARY KEY(target_id, semantic_id))"
    )
    db.execute(
        "INSERT OR IGNORE INTO graph_business_semantic_links "
        "SELECT n.stable_id, l.semantic_id, l.reason FROM search_concept_links l JOIN search_nodes n ON n.id=l.node_id"
    )


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
