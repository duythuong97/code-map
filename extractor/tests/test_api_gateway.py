"""CloudFormation API Gateway routes, and the links client -> gateway route -> backend operation."""
from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from code_tree_exporter.contract.graph_contract import api_operation_id
from code_tree_exporter.extractors.api_gateway.runner import extract, load_template
from code_tree_exporter.graph_package import GraphPackage
from code_tree_exporter.linker import link_api, link_gateway

# Resources split over two templates, the root imported from another stack.
RESOURCES = textwrap.dedent(
    """\
    Resources:
      StoreResource:
        Type: AWS::ApiGateway::Resource
        Properties:
          RestApiId: !Sub ${GatewayStack.ApiId}
          ParentId: !Sub ${GatewayStack.RootResourceId}
          PathPart: "store"
      ItemsResource:
        Type: AWS::ApiGateway::Resource
        Properties:
          RestApiId: !Sub ${GatewayStack.ApiId}
          ParentId: !Ref StoreResource
          PathPart: "items"
      ItemResource:
        Type: AWS::ApiGateway::Resource
        Properties:
          ParentId: !Ref ItemsResource
          PathPart: "{itemId}"
      ReserveResource:
        Type: AWS::ApiGateway::Resource
        Properties:
          ParentId: !Ref ItemResource
          PathPart: "reserve"
    """
)
METHODS = textwrap.dedent(
    """\
    Resources:
      ReservePost:
        Type: AWS::ApiGateway::Method
        DependsOn:
          - ReserveResource
        Properties:
          RestApiId: !Sub ${GatewayStack.ApiId}
          ResourceId: !Ref ReserveResource
          HttpMethod: "POST"
          AuthorizationType: "COGNITO_USER_POOLS"
          Integration:
            ConnectionType: "VPC_LINK"
            IntegrationHttpMethod: "POST"
            Type: "HTTP_PROXY"
            Uri: !Sub "http://${Network.BackendHost}:${Network.BackendPort}/inventory-svc/v1/items/{itemId}/reserve"
      ReserveOptions:
        Type: AWS::ApiGateway::Method
        Properties:
          ResourceId: !Ref ReserveResource
          HttpMethod: "OPTIONS"
          Integration:
            Type: "MOCK"
      OrphanGet:
        Type: AWS::ApiGateway::Method
        Properties:
          ResourceId: !Ref NotDeclaredHere
          HttpMethod: "GET"
    """
)
# The long form of the same intrinsics, in JSON.
JSON_TEMPLATE = {
    "Resources": {
        "StockResource": {
            "Type": "AWS::ApiGateway::Resource",
            "Properties": {"ParentId": {"Fn::GetAtt": ["Api", "RootResourceId"]}, "PathPart": "stock"},
        },
        "StockGet": {
            "Type": "AWS::ApiGateway::Method",
            "Properties": {
                "ResourceId": {"Ref": "StockResource"},
                "HttpMethod": "GET",
                "Integration": {
                    "Type": "HTTP_PROXY",
                    "Uri": {"Fn::Join": ["", ["http://", {"Ref": "BackendHost"}, "/inventory-svc/v1/stock"]]},
                },
            },
        },
    }
}


def _operation(node_id: str, method: str, route: str, **properties: object) -> dict[str, str]:
    return {
        "node_id": node_id,
        "node_type": "API_OPERATION",
        "technical_name": f"{method} {route}",
        "qualified_name": f"{method} {route}",
        "properties_json": json.dumps({"method": method, "route": route, **properties}),
    }


class GatewayTemplateTests(unittest.TestCase):
    def extract(self) -> tuple[dict[str, dict], dict[str, dict]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gateway").mkdir()
            (root / "gateway" / "resources.yml").write_text(RESOURCES, encoding="utf-8")
            (root / "gateway" / "methods.yaml").write_text(METHODS, encoding="utf-8")
            (root / "gateway" / "stock.json").write_text(json.dumps(JSON_TEMPLATE), encoding="utf-8")
            (root / "gateway" / "notes.yml").write_text("not: [a, template", encoding="utf-8")
            output = root / "out"
            extract({"type": "api-gateway", "source": "edge", "application": "edge-gateway",
                     "root": str(root), "output": str(output), "folders": ["gateway"]})
            graph = GraphPackage()
            graph.merge_directory(output)
        return graph.nodes, graph.issues

    def test_short_form_tags_become_intrinsics(self) -> None:
        template = load_template(METHODS)
        properties = template["Resources"]["ReservePost"]["Properties"]
        self.assertEqual(properties["ResourceId"], {"Ref": "ReserveResource"})
        self.assertIn("Fn::Sub", properties["Integration"]["Uri"])

    def test_public_path_and_backend_integration(self) -> None:
        nodes, _ = self.extract()
        reserve = nodes[api_operation_id("edge-gateway", "POST", "/store/items/{id}/reserve")]
        properties = json.loads(reserve["properties_json"])
        self.assertTrue(properties["gateway"])
        self.assertEqual(properties["integration"]["method"], "POST")
        self.assertEqual(properties["integration"]["path"], "/inventory-svc/v1/items/{id}/reserve")
        stock = json.loads(nodes[api_operation_id("edge-gateway", "GET", "/stock")]["properties_json"])
        self.assertEqual(stock["integration"]["path"], "/inventory-svc/v1/stock")

    def test_preflight_and_unknown_resources_are_not_routes(self) -> None:
        nodes, issues = self.extract()
        routes = {node["technical_name"] for node in nodes.values() if node["node_type"] == "API_OPERATION"}
        self.assertEqual(routes, {"POST /store/items/{id}/reserve", "GET /stock"})
        self.assertIn("GATEWAY_RESOURCE_NOT_FOUND", {issue["issue_type"] for issue in issues.values()})


class GatewayLinkTests(unittest.TestCase):
    def graph(self) -> GraphPackage:
        graph = GraphPackage()
        gateway = _operation("api-operation:edge:POST:/store/items/{id}/reserve", "POST", "/store/items/{id}/reserve",
                             gateway=True, integration={"method": "POST", "path": "/inventory-svc/v1/items/{id}/reserve"})
        backend = _operation("api-operation:inventory:POST:/v1/items/{id}/reserve", "POST", "/v1/items/{id}/reserve")
        # Another API with the same route name under another prefix.
        other = _operation("api-operation:pricing:POST:/pricing/items/{id}/reserve", "POST", "/pricing/items/{id}/reserve")
        call = {
            "node_id": "api-call:web:POST:/store/items/{id}/reserve",
            "node_type": "API_CALL_REFERENCE",
            "technical_name": "POST /store/items/{id}/reserve",
            "qualified_name": "POST https://gateway.example/prod/store/items/{id}/reserve",
            "properties_json": json.dumps({"method": "POST", "route": "/prod/store/items/{id}/reserve"}),
        }
        for node in (gateway, backend, other, call):
            graph.nodes[node["node_id"]] = node
        return graph

    def edges(self, graph: GraphPackage) -> set[tuple[str, str, str]]:
        return {(edge["source_node_id"], edge["edge_type"], edge["target_node_id"]) for edge in graph.edges.values()}

    def test_gateway_route_forwards_to_the_backend_operation(self) -> None:
        graph = self.graph()
        link_gateway(graph)
        self.assertEqual(self.edges(graph), {(
            "api-operation:edge:POST:/store/items/{id}/reserve", "ROUTES_TO",
            "api-operation:inventory:POST:/v1/items/{id}/reserve",
        )})

    def test_client_call_links_to_the_longest_matching_route(self) -> None:
        graph = self.graph()
        link_api(graph)
        self.assertEqual(self.edges(graph), {(
            "api-call:web:POST:/store/items/{id}/reserve", "CALLS_API",
            "api-operation:edge:POST:/store/items/{id}/reserve",
        )})
        self.assertEqual(graph.issues, {})

    def test_backend_not_extracted_is_an_issue(self) -> None:
        graph = self.graph()
        del graph.nodes["api-operation:inventory:POST:/v1/items/{id}/reserve"]
        link_gateway(graph)
        self.assertEqual(self.edges(graph), set())
        self.assertEqual({issue["issue_type"] for issue in graph.issues.values()}, {"GATEWAY_BACKEND_NOT_FOUND"})


if __name__ == "__main__":
    unittest.main()
