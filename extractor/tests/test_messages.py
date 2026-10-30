"""message-flows.csv: topics between the code that publishes and the code that consumes."""
from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from code_tree_exporter.contract.vocabulary import follows
from code_tree_exporter.graph_package import GraphPackage
from code_tree_exporter.messages import link_messages

TOPIC = "message-topic:kafka:stock-reserved"


def _node(node_id: str, node_type: str, name: str, qualified: str, repository: str = "", **properties: object) -> dict:
    return {
        "node_id": node_id, "node_type": node_type, "technical_name": name, "qualified_name": qualified,
        "default_display_name": name, "system_key": "", "database_key": "", "repository_key": repository,
        "graph_role": "TECHNICAL", "confidence": "1.0", "properties_json": json.dumps(properties),
    }


class MessageFlowTests(unittest.TestCase):
    def link(self, csv_text: str) -> GraphPackage:
        graph = GraphPackage()
        for node in (
            # API extractor: documentation comment id in the qualified name.
            _node("method:shop:reserve", "METHOD", "Reserve",
                  "shop.Shop.Api/Shop.Api.csproj|global::Shop.Api.StockService|M:Shop.Api.StockService.Reserve(System.Int32)",
                  "shop-api"),
            # Batch extractor: file:offset:Class.Method.
            _node("method:worker:handle", "METHOD", "Handle", "worker.src/Consumer.cs:120:StockReservedConsumer.Handle", "worker"),
            _node("method:audit:handle", "METHOD", "Handle", "audit.src/Consumer.cs:80:AuditConsumer.Handle", "audit"),
            _node("executable:batch:mailer.exe", "EXECUTABLE", "mailer.exe", "batch.mailer.exe", "mailer"),
            _node("api-operation:shop:POST:/api/stock/{id}/reserve", "API_OPERATION", "POST /api/stock/{id}/reserve",
                  "shop.POST./api/stock/{id}/reserve", "shop-api", method="POST", route="/api/stock/{id}/reserve"),
        ):
            graph.nodes[node["node_id"]] = node
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "message-flows.csv").write_text(textwrap.dedent(csv_text), encoding="utf-8")
            link_messages(graph, Path(directory))
        return graph

    def edges(self, graph: GraphPackage) -> set[tuple[str, str, str]]:
        return {(edge["source_node_id"], edge["edge_type"], edge["target_node_id"]) for edge in graph.edges.values()}

    def issues(self, graph: GraphPackage) -> list[str]:
        return sorted(issue["issue_type"] for issue in graph.issues.values())

    def test_producers_and_consumers_by_method_api_and_executable(self) -> None:
        graph = self.link("""\
            topic,kind,producer,consumer,repository,note
            stock-reserved,kafka,StockService.Reserve,StockReservedConsumer.Handle,,after the stock row is locked
            stock-reserved,,POST /api/stock/{itemId}/reserve,,,
            stock-reserved,kafka,,mailer.exe,,
            """)
        self.assertEqual(self.edges(graph), {
            ("method:shop:reserve", "PUBLISHES", TOPIC),
            ("api-operation:shop:POST:/api/stock/{id}/reserve", "PUBLISHES", TOPIC),
            (TOPIC, "CONSUMED_BY", "method:worker:handle"),
            (TOPIC, "CONSUMED_BY", "executable:batch:mailer.exe"),
        })
        topic = graph.nodes[TOPIC]
        self.assertEqual(topic["node_type"], "MESSAGE_TOPIC")
        self.assertEqual(json.loads(topic["properties_json"])["note"], "after the stock row is locked")
        self.assertEqual({edge["graph_layer"] for edge in graph.edges.values()}, {"TECHNICAL"})
        self.assertEqual(self.issues(graph), [])

    def test_same_method_name_needs_the_class_or_the_repository(self) -> None:
        graph = self.link("""\
            topic,producer,consumer,repository
            stock-reserved,,Handle,
            stock-reserved,,Handle,audit
            stock-reserved,Missing.Method,,
            ,StockService.Reserve,,
            """)
        self.assertEqual(self.edges(graph), {(TOPIC, "CONSUMED_BY", "method:audit:handle")})
        self.assertEqual(self.issues(graph), ["AMBIGUOUS_SYMBOL", "INVALID_INPUT_ROW", "MESSAGE_ENDPOINT_NOT_FOUND"])

    def test_no_file_changes_nothing(self) -> None:
        graph = GraphPackage()
        link_messages(graph, None)
        self.assertEqual((graph.nodes, graph.edges), ({}, {}))

    def test_a_flow_goes_through_the_topic(self) -> None:
        self.assertTrue(follows("PUBLISHES", "MESSAGE_TOPIC"))
        self.assertTrue(follows("CONSUMED_BY", "METHOD"))


if __name__ == "__main__":
    unittest.main()
