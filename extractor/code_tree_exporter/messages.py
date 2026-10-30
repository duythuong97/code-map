"""Message topics and queues (Kafka, ...) declared in ``input-data/message-flows.csv``.

Producers and consumers sit in different sources and the broker hides the
link from the code, so it is declared once per relation::

    topic,kind,producer,consumer,repository,note
    order-placed,kafka,OrderService.PlaceOrder,,,
    order-placed,kafka,,OrderPlacedConsumer.Handle,order-worker,

``producer`` and ``consumer`` name what the graph already has: a method
(``Class.Method`` or ``Namespace.Class.Method``), an API operation
(``POST /api/orders``) or an executable (``OrderWorker.exe``). ``repository``
narrows the lookup when the same name exists in several repositories. A row
may give the producer, the consumer or both.

The result is a ``MESSAGE_TOPIC`` node with ``producer -PUBLISHES-> topic`` and
``topic -CONSUMED_BY-> consumer``. Both are flow steps (``contract.vocabulary``),
so the web code-flow, the MCP trace and the business documents continue from
the code that publishes to the code that consumes and the tables it writes.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .contract.graph_contract import canonical_edge_id, normalize_http_route, stable_node_id
from .graph_package import GraphPackage

FILE_NAME = "message-flows.csv"
_API_REFERENCE = re.compile(r"^\s*([A-Za-z]+)\s+(/\S*)\s*$")
_METHOD_TYPES = frozenset({"METHOD", "LOCAL_ROUTINE"})


def link_messages(graph: GraphPackage, input_root: Path | None) -> None:
    path = input_root / FILE_NAME if input_root else None
    if path is None or not path.is_file():
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    resolver = _Resolver(graph)
    for line, row in enumerate(rows, start=2):
        row = {str(key or "").strip().lower(): str(value or "").strip() for key, value in row.items()}
        topic = row.get("topic", "")
        if not topic or not (row.get("producer") or row.get("consumer")):
            graph.add_issue(
                "INVALID_INPUT_ROW",
                f"{FILE_NAME} line {line}: a row needs a topic and a producer or a consumer",
                source_path=f"input-data/{FILE_NAME}",
                severity="WARNING",
            )
            continue
        kind = (row.get("kind") or "kafka").lower()
        topic_id = stable_node_id("message-topic", kind, topic)
        properties = {"kind": kind, "topic": topic, "source": FILE_NAME}
        if row.get("note"):
            properties["note"] = row["note"]
        graph.nodes.setdefault(topic_id, {
            "node_id": topic_id,
            "node_type": "MESSAGE_TOPIC",
            "technical_name": topic,
            "qualified_name": f"{kind}:{topic}",
            "default_display_name": topic,
            "system_key": "",
            "database_key": "",
            "repository_key": "",
            "graph_role": "MAIN",
            "confidence": "1.0",
            "properties_json": json.dumps(properties, ensure_ascii=False, sort_keys=True),
        })
        for column, edge_type in (("producer", "PUBLISHES"), ("consumer", "CONSUMED_BY")):
            reference = row.get(column, "")
            if not reference:
                continue
            candidates = resolver.find(reference, row.get("repository", ""))
            if len(candidates) != 1:
                graph.add_issue(
                    "AMBIGUOUS_SYMBOL" if candidates else "MESSAGE_ENDPOINT_NOT_FOUND",
                    f"{FILE_NAME} line {line}: {column} {reference!r} of topic {topic} "
                    + (f"matches {len(candidates)} nodes; add the repository column" if candidates
                       else "matches no method, API or executable in the graph"),
                    source_path=f"input-data/{FILE_NAME}",
                    severity="WARNING",
                    properties={"topic_node_id": topic_id, "candidate_node_ids": candidates[:20]},
                )
                continue
            source, target = (candidates[0], topic_id) if edge_type == "PUBLISHES" else (topic_id, candidates[0])
            _add_edge(graph, source, edge_type, target, {"source": FILE_NAME, "line": line})


class _Resolver:
    def __init__(self, graph: GraphPackage) -> None:
        self.graph = graph
        self.methods: dict[str, list[str]] = {}
        self.executables: dict[str, list[str]] = {}
        self.operations: dict[tuple[str, str], list[str]] = {}
        for node_id, node in graph.nodes.items():
            node_type = node.get("node_type")
            name = str(node.get("technical_name") or "")
            if node_type in _METHOD_TYPES:
                self.methods.setdefault(name.casefold(), []).append(node_id)
            elif node_type == "EXECUTABLE":
                properties = _properties(node)
                names = {name, *(str(value) for key in ("names", "assemblyNames", "aliases")
                                 for value in properties.get(key) or [])}
                for key in {_executable(value) for value in names if value}:
                    self.executables.setdefault(key, []).append(node_id)
            elif node_type == "API_OPERATION":
                signature = _signature(node)
                if signature:
                    self.operations.setdefault(signature, []).append(node_id)

    def find(self, reference: str, repository: str) -> list[str]:
        api = _API_REFERENCE.match(reference)
        if api:
            try:
                method, route = normalize_http_route(api.group(1), api.group(2))
            except ValueError:
                return []
            candidates = self.operations.get((method, route.casefold()), [])
        elif reference.lower().endswith(".exe") or ("." not in reference and _executable(reference) in self.executables):
            candidates = self.executables.get(_executable(reference), [])
        else:
            candidates = self._methods(reference)
        if repository:
            candidates = [node_id for node_id in candidates
                          if str(self.graph.nodes[node_id].get("repository_key") or "").casefold() == repository.casefold()]
        return sorted(set(candidates))

    def _methods(self, reference: str) -> list[str]:
        parts = [part for part in reference.split(".") if part]
        if not parts:
            return []
        candidates = self.methods.get(parts[-1].casefold(), [])
        if len(parts) == 1:
            return candidates
        # The qualified name holds "Class.Method" after a separator: "M:Ns.Class.Method(...)", "...:Class.Method".
        pattern = re.compile(r"(?:^|[.:|])" + re.escape(".".join(parts)) + r"(?:$|[(|])", re.IGNORECASE)
        return [node_id for node_id in candidates if pattern.search(str(self.graph.nodes[node_id].get("qualified_name") or ""))]


def _properties(node: dict[str, str]) -> dict:
    try:
        value = json.loads(node.get("properties_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _signature(node: dict[str, str]) -> tuple[str, str] | None:
    properties = _properties(node)
    method = str(properties.get("method") or "")
    route = str(properties.get("route") or "")
    if not method or not route:
        method, _, route = str(node.get("technical_name") or "").partition(" ")
    try:
        method, route = normalize_http_route(method, route)
    except ValueError:
        return None
    return method, route.casefold()


def _executable(name: str) -> str:
    """``OrderFulfillment.exe``, ``order-fulfillment`` and ``order_fulfillment.exe`` are one executable."""
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
    name = name[:-4] if name.endswith(".exe") else name
    return re.sub(r"[-_. ]", "", name)


def _add_edge(graph: GraphPackage, source: str, edge_type: str, target: str, properties: dict) -> None:
    # TECHNICAL, not DATA_FLOW: flow walks leave the derived DATA_FLOW shortcuts out.
    edge_id = canonical_edge_id(source, edge_type, target, "", "TECHNICAL")
    graph.edges.setdefault(edge_id, {
        "edge_id": edge_id,
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": edge_type,
        "graph_layer": "TECHNICAL",
        "raw_operation": "",
        "confidence": "1.0",
        "properties_json": json.dumps(properties, ensure_ascii=False, sort_keys=True),
    })
