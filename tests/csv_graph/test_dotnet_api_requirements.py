from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from application.backend.importer.package_validator import validate_package
from contract.graph_contract import api_operation_id, stable_node_id, table_id
from extractors.package_support.package_writer import load_authoritative_ids

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "extractors/dotnet-api-extractor/main.py"
CONFIG = ROOT / "configs/dotnet-api-order-api.json"


def _replace_workspace(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _replace_workspace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_workspace(item) for item in value]
    if isinstance(value, str):
        return value.replace("${WORKSPACE_ROOT}", str(ROOT))
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class DotNetApiRequirementsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)
        cls.package_root = cls.work / "dotnet-api"
        config = _replace_workspace(json.loads(CONFIG.read_text(encoding="utf-8")))
        config["output"] = str(cls.package_root)
        generated_config = cls.work / "order-api.json"
        generated_config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(generated_config)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(f"dotnet-api extractor failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

        allowed_ids = load_authoritative_ids(ROOT / "input-data")
        allowed_ids.update(row["node_id"] for row in _read_csv(cls.package_root / "nodes.csv"))
        cls.package = validate_package(cls.package_root, allowed_ids, workspace_root=ROOT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_controller_route_constants_are_resolved(self) -> None:
        nodes = {row["node_id"]: row for row in self.package["nodes"]}
        for method, route in (
            ("POST", "/api/orders"),
            ("POST", "/api/orders/retry"),
            ("GET", "/api/orders/{id}"),
            ("GET", "/api/Tokenized/Ping"),
        ):
            with self.subTest(method=method, route=route):
                node = nodes[api_operation_id("order-api", method, route)]
                props = json.loads(node["properties_json"])
                self.assertEqual("controller", props["endpointStyle"])
                self.assertEqual(route, props["route"])

    def test_minimal_api_map_methods_are_extracted(self) -> None:
        nodes = {row["node_id"]: row for row in self.package["nodes"]}
        expected = {
            ("GET", "/api/minimal/orders/{id}"),
            ("POST", "/api/minimal/orders"),
            ("PUT", "/api/minimal/orders/{id}"),
            ("PATCH", "/api/minimal/orders/{id}/status"),
            ("DELETE", "/api/minimal/orders/{id}"),
            ("PATCH", "/api/minimal/orders/{id}/state"),
            ("DELETE", "/api/minimal/orders/{id}/state"),
        }
        for method, route in expected:
            with self.subTest(method=method, route=route):
                node = nodes[api_operation_id("order-api", method, route)]
                props = json.loads(node["properties_json"])
                self.assertEqual("minimal", props["endpointStyle"])
                self.assertEqual(route, props["route"])

    def test_semantic_invocation_reachability_replaces_suffix_cartesian_edges(self) -> None:
        node_ids = {row["node_id"] for row in self.package["nodes"]}
        self.assertIn(stable_node_id("service", "order-api", "OrderService"), node_ids)
        self.assertIn(stable_node_id("repository", "order-api", "OrderRepository"), node_ids)
        self.assertNotIn(stable_node_id("service", "order-api", "UnusedService"), node_ids)
        self.assertNotIn(stable_node_id("repository", "order-api", "UnusedRepository"), node_ids)

        call_edges = {
            (row["source_node_id"], row["target_node_id"], row["raw_operation"])
            for row in self.package["edges"]
            if row["edge_type"] == "CALLS"
        }
        self.assertIn(
            (
                stable_node_id("controller", "order-api", "OrderController"),
                stable_node_id("service", "order-api", "OrderService"),
                "CreateOrder",
            ),
            call_edges,
        )
        self.assertIn(
            (
                stable_node_id("service", "order-api", "OrderService"),
                stable_node_id("repository", "order-api", "OrderRepository"),
                "ReadOrder",
            ),
            call_edges,
        )
        self.assertFalse(any("Unused" in source or "Unused" in target for source, target, _ in call_edges))

    def test_only_source_backed_create_endpoints_call_create_order(self) -> None:
        nodes = {row["node_id"]: row for row in self.package["nodes"]}
        target = next(
            node_id
            for node_id, row in nodes.items()
            if row["node_type"] == "UNRESOLVED_REFERENCE"
            and json.loads(row["properties_json"]).get("raw_reference") == "PKG_ORDER.CREATE_ORDER"
        )
        endpoint_sources = {
            row["source_node_id"]
            for row in self.package["edges"]
            if row["edge_type"] == "CALLS"
            and row["target_node_id"] == target
            and row["source_node_id"].startswith("api-operation:")
        }
        self.assertEqual(
            {
                api_operation_id("order-api", "POST", "/api/orders"),
                api_operation_id("order-api", "POST", "/api/orders/retry"),
                api_operation_id("order-api", "POST", "/api/minimal/orders"),
            },
            endpoint_sources,
        )

    def test_dynamic_sql_issue_is_emitted_without_fake_table_target(self) -> None:
        dynamic_issues = [row for row in self.package["issues"] if row["issue_type"] == "DYNAMIC_SQL"]
        self.assertTrue(dynamic_issues)
        self.assertTrue(any("tableName" in row["raw_reference"] for row in dynamic_issues))

        edge_targets = {row["target_node_id"] for row in self.package["edges"]}
        self.assertNotIn(table_id("ORDER_DB", "ORDER_APP"), edge_targets)
        self.assertFalse(any("tableName" in target for target in edge_targets))
        self.assertFalse(any(row["raw_reference"] == "ORDER_APP" for row in self.package["issues"]))

    def test_table_targets_are_authoritative_only(self) -> None:
        authoritative_tables = {
            table_id("ORDER_DB", "ORDER_HEADER"),
            table_id("ORDER_DB", "ORDER_LINE"),
            table_id("ORDER_DB", "PAYMENT_TRANSACTION"),
        }
        table_targets = {
            row["target_node_id"]
            for row in self.package["edges"]
            if row["target_node_id"].startswith("table:")
        }
        self.assertTrue(table_targets <= authoritative_tables)
        self.assertIn(table_id("ORDER_DB", "ORDER_HEADER"), table_targets)
        self.assertNotIn(table_id("ORDER_DB", "ORDER_ARCHIVE_TMP"), table_targets)

    def test_workspace_discovers_csproj_and_project_references(self) -> None:
        root = self.work / "workspace-source"
        api = root / "Api"
        data = root / "Data"
        (api / "Controllers").mkdir(parents=True)
        (api / "Services").mkdir(parents=True)
        (data / "Repositories").mkdir(parents=True)
        (root / "Workspace.sln").write_text(
            "Microsoft Visual Studio Solution File, Format Version 12.00\n"
            "Project(\"{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}\") = \"Api\", \"Api\\Api.csproj\", \"{11111111-1111-1111-1111-111111111111}\"\n"
            "EndProject\n"
            "Project(\"{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}\") = \"Data\", \"Data\\Data.csproj\", \"{22222222-2222-2222-2222-222222222222}\"\n"
            "EndProject\n",
            encoding="utf-8",
        )
        (api / "Api.csproj").write_text(
            "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
            "  <ItemGroup>\n"
            "    <ProjectReference Include=\"../Data/Data.csproj\" />\n"
            "  </ItemGroup>\n"
            "</Project>\n",
            encoding="utf-8",
        )
        (data / "Data.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\" />\n", encoding="utf-8")
        (api / "Controllers" / "OrdersController.cs").write_text(
            "using Api.Services;\n"
            "using Data.Repositories;\n"
            "namespace Api.Controllers;\n"
            "public sealed class RouteAttribute : System.Attribute { public RouteAttribute(string value) {} }\n"
            "public sealed class HttpGetAttribute : System.Attribute { public HttpGetAttribute(string value) {} }\n"
            "[Route(\"api/orders\")]\n"
            "public class OrdersController\n"
            "{\n"
            "    private readonly OrderService _service = new(new OrderRepository());\n"
            "    [HttpGet(\"{id}\")]\n"
            "    public string Get(long id) => _service.ReadOrder(id);\n"
            "}\n",
            encoding="utf-8",
        )
        (api / "Services" / "OrderService.cs").write_text(
            "using Data.Repositories;\n"
            "namespace Api.Services;\n"
            "public class OrderService\n"
            "{\n"
            "    private readonly OrderRepository _repository;\n"
            "    public OrderService(OrderRepository repository) => _repository = repository;\n"
            "    public string ReadOrder(long id) => _repository.ReadOrder(id);\n"
            "}\n",
            encoding="utf-8",
        )
        (data / "Repositories" / "OrderRepository.cs").write_text(
            "namespace Data.Repositories;\n"
            "public class OrderRepository\n"
            "{\n"
            "    public string ReadOrder(long id)\n"
            "    {\n"
            "        const string sql = \"SELECT ORDER_ID FROM ORDER_HEADER WHERE ORDER_ID = :id\";\n"
            "        return sql;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        output = self.work / "workspace-package"
        config_path = self.work / "workspace-dotnet-api.json"
        config_path.write_text(
            json.dumps(
                {
                    "type": "dotnet-api",
                    "source": "workspace-api",
                    "application": "workspace-api",
                    "repository": "workspace-repo",
                    "database": "ORDER_DB",
                    "root": str(root),
                    "folders": ["."],
                    "inputData": str(ROOT / "input-data"),
                    "output": str(output),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(config_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
        allowed_ids = load_authoritative_ids(ROOT / "input-data")
        allowed_ids.update(row["node_id"] for row in _read_csv(output / "nodes.csv"))
        package = validate_package(output, allowed_ids)
        node_ids = {row["node_id"] for row in package["nodes"]}
        edge_pairs = {(row["source_node_id"], row["edge_type"], row["target_node_id"]) for row in package["edges"]}
        api_project = stable_node_id("dotnet-project", "workspace-repo", "Api/Api.csproj")
        data_project = stable_node_id("dotnet-project", "workspace-repo", "Data/Data.csproj")
        self.assertIn(api_project, node_ids)
        self.assertIn(data_project, node_ids)
        self.assertNotIn(stable_node_id("dotnet-project", "workspace-repo", "src/workspace-api/workspace-api.csproj"), node_ids)
        self.assertIn((api_project, "PROJECT_REFERENCE", data_project), edge_pairs)
        self.assertIn((api_project, "CONTAINS", stable_node_id("controller", "workspace-api", "OrdersController")), edge_pairs)
        self.assertIn((data_project, "CONTAINS", stable_node_id("repository", "workspace-api", "OrderRepository")), edge_pairs)
        self.assertIn(table_id("ORDER_DB", "ORDER_HEADER"), {row["target_node_id"] for row in package["edges"]})


if __name__ == "__main__":
    unittest.main()
