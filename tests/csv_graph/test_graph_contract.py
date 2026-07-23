import unittest

from contract.graph_contract import (
    api_operation_id, canonical_edge_id, column_id, normalize_http_route,
    normalize_oracle_identifier, normalize_repository_path, routine_id, sql_file_id,
    table_id, validate_edge_fields, validate_node_fields, validate_properties_json,
)


class GraphContractTest(unittest.TestCase):
    def test_repository_path_is_canonical_and_safe(self):
        self.assertEqual("src/sql/orders.sql", normalize_repository_path("./src\\sql//orders.sql"))
        for invalid in ("", "/etc/passwd", "C:\\secret.txt", "src/../secret", ".."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_repository_path(invalid)
        self.assertEqual("sql-file:orders:sql/create.sql", sql_file_id("orders", "sql/create.sql"))

    def test_http_route_discards_environment_and_normalizes_parameters(self):
        cases = (
            (" get ", "https://dev.example/api//orders/123/?trace=1", ("GET", "/api/orders/123")),
            ("post", "/orders/{orderId}/items/:itemId/", ("POST", "/orders/{id}/items/{id}")),
            ("DELETE", "orders/<uuid:order_id>", ("DELETE", "/orders/{id}")),
            ("GET", "/", ("GET", "/")),
        )
        for method, route, expected in cases:
            with self.subTest(route=route):
                self.assertEqual(expected, normalize_http_route(method, route))
        first = api_operation_id("orders", "post", "https://a.example/orders/{orderId}?x=1")
        second = api_operation_id("orders", "POST", "http://b.example//orders/:id/")
        self.assertEqual(first, second)

    def test_oracle_quoted_and_unquoted_identifiers(self):
        self.assertEqual("HR.ORDERS", normalize_oracle_identifier("hr.orders"))
        self.assertEqual("MixedCase.LINE", normalize_oracle_identifier('"MixedCase".line'))
        self.assertEqual('A"B', normalize_oracle_identifier('"A""B"'))
        with self.assertRaises(ValueError):
            normalize_oracle_identifier('"unfinished')
        self.assertEqual("table:ORDER_DB:ORDER_HEADER", table_id("order_db", "order_header"))
        self.assertEqual("column:ORDER_DB:ORDER_HEADER:STATUS", column_id("order_db", "order_header", "status"))

    def test_routine_overloads_have_stable_distinct_identity(self):
        no_args = routine_id("procedure", "db", "pkg", "run")
        number = routine_id("procedure", "db", "pkg", "run", ("number",))
        varchar = routine_id("procedure", "db", "pkg", "run", ("varchar2",))
        self.assertEqual("procedure:DB:PKG:RUN:void", no_args)
        self.assertEqual(3, len({no_args, number, varchar}))

    def test_edge_identity_uses_only_canonical_tuple(self):
        arguments = ("screen:app:/orders", "CALLS", "procedure:DB:PKG:RUN:void", "EXECUTE", "TECHNICAL")
        edge_id = canonical_edge_id(*arguments)
        self.assertRegex(edge_id, r"^edge:[0-9a-f]{64}$")
        self.assertEqual(edge_id, canonical_edge_id(*arguments))
        changed = list(arguments)
        changed[3] = "CALL"
        self.assertNotEqual(edge_id, canonical_edge_id(*changed))
        with self.assertRaises(ValueError):
            canonical_edge_id(arguments[0], "UNKNOWN", *arguments[2:])

    def test_contract_field_validation(self):
        validate_node_fields("TABLE", "MAIN", 1, "{}")
        validate_edge_fields("READS", "TECHNICAL", "0.75", {"source": "parser"})
        for confidence in (-0.01, 1.01, True, "unknown"):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                validate_node_fields("TABLE", "MAIN", confidence, "{}")
        for properties in ("[]", "null", "not-json"):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                validate_properties_json(properties)
        with self.assertRaises(ValueError):
            validate_node_fields("NOT_A_NODE", "MAIN", 1, "{}")
        with self.assertRaises(ValueError):
            validate_edge_fields("READS", "INVALID", 1, "{}")


if __name__ == "__main__":
    unittest.main()
