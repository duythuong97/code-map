from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from application.backend.importer.pipeline import initialize, materialize, resolve
from code_tree_exporter.contract.graph_contract import canonical_edge_id


class ResolverMaterializerRequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "graph.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        initialize(self.conn)
        self.conn.execute("INSERT INTO graph_sources VALUES('s','test-package','now')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _node(self, node_id: str, node_type: str, *, database: str = "", props: dict | None = None) -> None:
        technical_name = node_id.split(":")[-1]
        self.conn.execute(
            "INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                node_id,
                node_type,
                technical_name,
                node_id,
                technical_name,
                "",
                database,
                "",
                "MAIN",
                1.0,
                json.dumps(props or {}, sort_keys=True),
                "s",
            ),
        )

    def _edge(self, source: str, target: str, edge_type: str, *, raw_operation: str = "", layer: str = "TECHNICAL", props: dict | None = None, source_id: str = "s") -> str:
        edge_id = canonical_edge_id(source, edge_type, target, raw_operation, layer)
        self.conn.execute(
            "INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",
            (edge_id, source, target, edge_type, layer, raw_operation, 1.0, json.dumps(props or {}, sort_keys=True), source_id),
        )
        return edge_id

    def _resolver_edges(self, source: str, edge_type: str = "RESOLVES_TO") -> list[str]:
        rows = self.conn.execute(
            "SELECT target_node_id FROM graph_edges WHERE source_id='resolver:cross-source' AND source_node_id=? AND edge_type=? ORDER BY target_node_id",
            (source, edge_type),
        ).fetchall()
        return [row["target_node_id"] for row in rows]

    def _issue_types(self, source: str) -> set[str]:
        return {
            row["issue_type"]
            for row in self.conn.execute(
                "SELECT issue_type FROM graph_issues WHERE source_id='resolver:cross-source' AND source_node_id=?",
                (source,),
            )
        }

    def _api_operation(self, app: str, method: str, route: str) -> str:
        node_id = f"api-operation:{app}:{method}:{route}"
        self._node(node_id, "API_OPERATION", props={"method": method, "route": route})
        return node_id

    def _api_call(self, source: str, method: str, route: str, props: dict | None = None) -> str:
        node_id = f"api-call:{source}:{method}:{route}"
        merged = {"method": method, "route": route, **(props or {})}
        self._node(node_id, "API_CALL_REFERENCE", props=merged)
        return node_id

    def test_api_explicit_mapping_precedes_route_matching_and_links_ui_callers(self) -> None:
        preferred = self._api_operation("order-api", "POST", "/api/orders")
        self._api_operation("other-api", "POST", "/wrong")
        call = self._api_call("customer-web", "POST", "/wrong", {"apiOperationId": preferred})
        screen = "screen:customer-web:/orders/new"
        service = "angular-service:customer-web:OrderService"
        self._node(screen, "SCREEN")
        self._node(service, "ANGULAR_SERVICE")
        self._edge(screen, service, "CALLS")
        self._edge(service, call, "CALLS")

        with self.conn:
            resolve(self.conn)

        self.assertEqual([preferred], self._resolver_edges(call))
        self.assertEqual([preferred], self._resolver_edges(screen, "CALLS_API"))

    def test_api_matching_order_full_prefix_suffix_ambiguous_and_no_match(self) -> None:
        full = self._api_operation("order-api", "GET", "/api/orders")
        prefixed = self._api_operation("order-api", "POST", "/api/orders")
        suffix = self._api_operation("order-api", "DELETE", "/api/orders/{id}")
        self._api_operation("billing-api", "PATCH", "/api/orders")
        self._api_operation("shipping-api", "PATCH", "/api/orders")
        full_call = self._api_call("customer-web", "GET", "/api/orders")
        prefix_call = self._api_call("customer-web", "POST", "/order-api/api/orders")
        suffix_call = self._api_call("customer-web", "DELETE", "/gateway/order-api/api/orders/{id}")
        ambiguous_call = self._api_call("customer-web", "PATCH", "/gateway/api/orders")
        missing_call = self._api_call("customer-web", "PUT", "/api/missing")

        with self.conn:
            resolve(self.conn)

        self.assertEqual([full], self._resolver_edges(full_call))
        self.assertEqual([prefixed], self._resolver_edges(prefix_call))
        self.assertEqual([suffix], self._resolver_edges(suffix_call))
        self.assertFalse(self._resolver_edges(ambiguous_call))
        self.assertIn("API_ROUTE_AMBIGUOUS", self._issue_types(ambiguous_call))
        self.assertFalse(self._resolver_edges(missing_call))
        self.assertIn("API_ROUTE_NOT_MATCHED", self._issue_types(missing_call))

    def test_routine_resolver_exact_signature_and_overload_ambiguity(self) -> None:
        target = "procedure:ORDER_DB:PKG_ORDER:CREATE_ORDER:NUMBER_VARCHAR2"
        self._node(target, "PROCEDURE", database="ORDER_DB")
        exact_ref = "unresolved-reference:ORDER_DB:PKG_ORDER.CREATE_ORDER"
        self._node(
            exact_ref,
            "UNRESOLVED_REFERENCE",
            database="ORDER_DB",
            props={"package": "PKG_ORDER", "routine": "CREATE_ORDER", "signature": "NUMBER_VARCHAR2"},
        )
        overloaded_a = "procedure:ORDER_DB:PKG_ORDER:PROCESS_ORDER:NUMBER"
        overloaded_b = "procedure:ORDER_DB:PKG_ORDER:PROCESS_ORDER:VARCHAR2"
        self._node(overloaded_a, "PROCEDURE", database="ORDER_DB")
        self._node(overloaded_b, "PROCEDURE", database="ORDER_DB")
        ambiguous_ref = "unresolved-reference:ORDER_DB:PKG_ORDER.PROCESS_ORDER"
        self._node(
            ambiguous_ref,
            "UNRESOLVED_REFERENCE",
            database="ORDER_DB",
            props={"package": "PKG_ORDER", "routine": "PROCESS_ORDER"},
        )
        missing_ref = "unresolved-reference:ORDER_DB:PKG_ORDER.MISSING_PROC"
        self._node(missing_ref, "UNRESOLVED_REFERENCE", database="ORDER_DB")

        with self.conn:
            resolve(self.conn)

        self.assertEqual([target], self._resolver_edges(exact_ref))
        self.assertFalse(self._resolver_edges(ambiguous_ref))
        self.assertIn("AMBIGUOUS_SYMBOL", self._issue_types(ambiguous_ref))
        self.assertFalse(self._resolver_edges(missing_ref))
        self.assertIn("PROCEDURE_NOT_FOUND", self._issue_types(missing_ref))

    def test_materializer_traverses_only_allowed_edges_including_entry_in_and_keeps_terminal_operation(self) -> None:
        actor = "executable:batch-system:orderfulfillment.exe"
        entry = "executable-entry:orderfulfillment:main"
        repo = "repository:orderfulfillment:AllocationRepository"
        table = "table:ORDER_DB:FULFILLMENT_ALLOCATION"
        for node_id, node_type in (
            (actor, "EXECUTABLE"),
            (entry, "EXECUTABLE_ENTRY_POINT"),
            (repo, "REPOSITORY"),
            (table, "TABLE"),
        ):
            self._node(node_id, node_type, database="ORDER_DB")
        self._edge(actor, entry, "ENTRY_IN")
        self._edge(entry, repo, "CALLS")
        semantic = {"version": 1, "action": "WRITE", "operation": "INSERT", "fields": [{"name": "ORDER_ID"}]}
        terminal = self._edge(repo, table, "INSERTS", raw_operation="INSERT FULFILLMENT_ALLOCATION", props={"semantic": semantic})

        with self.conn:
            materialize(self.conn)

        path = self.conn.execute(
            "SELECT * FROM graph_paths WHERE actor_node_id=? AND table_node_id=? AND operation='W'",
            (actor, table),
        ).fetchone()
        self.assertIsNotNone(path)
        self.assertEqual(terminal, json.loads(path["edge_path_json"])[-1])
        data_edge = self.conn.execute(
            "SELECT raw_operation,properties_json FROM graph_edges WHERE graph_layer='DATA_FLOW' AND source_node_id=? AND target_node_id=?",
            (actor, table),
        ).fetchone()
        self.assertEqual("INSERT FULFILLMENT_ALLOCATION", data_edge["raw_operation"])
        props = json.loads(data_edge["properties_json"])
        self.assertEqual("INSERTS", props["terminal_edge_type"])
        self.assertEqual("INSERT FULFILLMENT_ALLOCATION", props["terminal_raw_operation"])
        self.assertEqual(semantic, props["semantic"])

    def test_materializer_preserves_multiple_paths_and_does_not_traverse_structural_shortcuts(self) -> None:
        actor = "screen:customer-web:/orders"
        service_a = "service:a"
        service_b = "service:b"
        repo = "repository:orders"
        shortcut = "service:shortcut"
        table = "table:ORDER_DB:ORDER_HEADER"
        for node_id, node_type in (
            (actor, "SCREEN"),
            (service_a, "SERVICE"),
            (service_b, "SERVICE"),
            (repo, "REPOSITORY"),
            (shortcut, "SERVICE"),
            (table, "TABLE"),
        ):
            self._node(node_id, node_type, database="ORDER_DB")
        self._edge(actor, service_a, "CALLS")
        self._edge(actor, service_b, "CALLS")
        self._edge(service_a, repo, "CALLS")
        self._edge(service_b, repo, "CALLS")
        self._edge(repo, table, "UPDATES", raw_operation="UPDATE ORDER_HEADER")
        self._edge(actor, shortcut, "CONTAINS", layer="STRUCTURAL")
        self._edge(shortcut, table, "DELETES", raw_operation="DELETE ORDER_HEADER")

        with self.conn:
            materialize(self.conn)

        rows = self.conn.execute(
            "SELECT edge_path_json FROM graph_paths WHERE actor_node_id=? AND table_node_id=? AND operation='W' ORDER BY edge_path_json",
            (actor, table),
        ).fetchall()
        paths = {tuple(json.loads(row["edge_path_json"])) for row in rows}
        self.assertEqual(2, len(paths))
        data_edges = self.conn.execute(
            "SELECT raw_operation FROM graph_edges WHERE graph_layer='DATA_FLOW' AND source_node_id=? AND target_node_id=? ORDER BY raw_operation",
            (actor, table),
        ).fetchall()
        self.assertEqual(["UPDATE ORDER_HEADER"], [row["raw_operation"] for row in data_edges])


if __name__ == "__main__":
    unittest.main()
