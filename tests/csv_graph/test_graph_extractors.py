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
from contract.graph_contract import api_operation_id, sql_file_id, stable_node_id, table_id
from extractors.package_support.package_writer import load_authoritative_ids

ROOT = Path(__file__).resolve().parents[2]
SOURCE_EXTRACTORS = {
    "angular": (ROOT / "extractors/angular-extractor/main.py", ROOT / "configs/angular-customer-web.json"),
    "dotnet_api": (ROOT / "extractors/dotnet-api-extractor/main.py", ROOT / "configs/dotnet-api-order-api.json"),
    "batch": (ROOT / "extractors/dotnet-batch-extractor/main.py", ROOT / "configs/dotnet-batch-order-fulfillment.json"),
    "plsql": (ROOT / "extractors/plsql-extractor/main.py", ROOT / "configs/plsql-order-db.json"),
    "sql_files": (ROOT / "extractors/sql-file-extractor/main.py", ROOT / "configs/sql-order-ops.json"),
}
CSV_NORMALIZER = (ROOT / "extractors/csv-normalizer/main.py", ROOT / "configs/csv-demo.json")
FORBIDDEN_SOURCE_TYPES = {"TABLE", "COLUMN", "JOB", "JOB_NETWORK"}


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


class ExtractorIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)
        cls.package_roots: dict[str, Path] = {}
        for name, (script, config_path) in SOURCE_EXTRACTORS.items():
            output = cls.work / "packages" / name
            cls._run_extractor(script, config_path, output)
            cls.package_roots[name] = output
        normalizer_output = cls.work / "authoritative"
        cls._run_extractor(CSV_NORMALIZER[0], CSV_NORMALIZER[1], normalizer_output)
        cls.normalizer_output = normalizer_output

        cls.allowed_ids = load_authoritative_ids(ROOT / "input-data")
        for package_root in cls.package_roots.values():
            cls.allowed_ids.update(row["node_id"] for row in _read_csv(package_root / "nodes.csv"))
        cls.packages = {name: validate_package(root, cls.allowed_ids) for name, root in cls.package_roots.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _run_extractor(cls, script: Path, config_path: Path, output: Path) -> None:
        config = _replace_workspace(json.loads(config_path.read_text(encoding="utf-8")))
        config["output"] = str(output)
        generated_config = cls.work / f"{script.parent.name}-{output.name}.json"
        generated_config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), "--config", str(generated_config)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(f"{script} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    def test_required_extractor_folder_structure_exists(self):
        for folder in (
            "angular-extractor",
            "dotnet-api-extractor",
            "dotnet-batch-extractor",
            "plsql-extractor",
            "sql-file-extractor",
            "csv-normalizer",
        ):
            self.assertTrue((ROOT / "extractors" / folder / "main.py").is_file(), folder)

    def test_all_five_source_extractors_emit_semantic_tree_v2(self):
        expected_types = {
            "angular": {"ANGULAR_SERVICE", "UI_ACTION"},
            "dotnet_api": {"API_OPERATION"},
            "batch": {"EXECUTABLE_ENTRY_POINT", "COMMAND_MODE"},
            "plsql": {"PROCEDURE", "FUNCTION"},
            "sql_files": {"SQL_FILE"},
        }
        for name, node_types in expected_types.items():
            trees = []
            for row in _read_csv(self.package_roots[name] / "nodes.csv"):
                tree = json.loads(row["properties_json"]).get("semantic_tree")
                if isinstance(tree, dict) and tree.get("version") == 2:
                    trees.append((row, tree))
            self.assertTrue(trees, name)
            self.assertTrue(node_types.issubset({row["node_type"] for row, _ in trees}), name)
            for row, tree in trees:
                self.assertEqual("operation", tree["type"], row["node_id"])
                for field in ("parameters", "steps", "outputs", "exceptions", "analysis_notes"):
                    self.assertIsInstance(tree[field], list, f"{row['node_id']}:{field}")
                for fact in tree["steps"]:
                    self.assertIn("path", fact["source"])
                    self.assertGreater(fact["source"]["line"], 0)

    def test_extractors_use_requirement_mandated_parser_stacks(self):
        angular_source = (ROOT / "extractors/angular-extractor/main.mjs").read_text(encoding="utf-8")
        self.assertIn("createSourceFile", angular_source)
        self.assertEqual("Node.js TypeScript Compiler API", self.packages["angular"]["manifest"]["metadata"]["technology"])

        roslyn_support = (ROOT / "extractors/_roslyn/CodeMapExtractorSupport.cs").read_text(encoding="utf-8")
        for folder, package_name, project_file in (
            ("dotnet-api-extractor", "dotnet_api", "DotNetApiExtractor.csproj"),
            ("dotnet-batch-extractor", "batch", "DotNetBatchExtractor.csproj"),
        ):
            self.assertTrue((ROOT / "extractors" / folder / project_file).is_file())
            program = (ROOT / "extractors" / folder / "Program.cs").read_text(encoding="utf-8")
            self.assertIn("SemanticModel", program + roslyn_support)
            self.assertIn("Microsoft.CodeAnalysis.CSharp", program + roslyn_support)
            self.assertEqual("C# Roslyn Workspace/SemanticModel", self.packages[package_name]["manifest"]["metadata"]["technology"])

        oracle_parser = (ROOT / "extractors/package_support/oracle_parser.py").read_text(encoding="utf-8")
        self.assertIn("from antlr4 import InputStream", oracle_parser)
        self.assertIn("class OracleSqlParser", oracle_parser)
        self.assertIn("class OraclePlsqlParser", oracle_parser)
        self.assertEqual("Python + ANTLR4 runtime Oracle PL/SQL parser", self.packages["plsql"]["manifest"]["metadata"]["technology"])
        self.assertEqual("Python + ANTLR4 runtime Oracle SQL parser", self.packages["sql_files"]["manifest"]["metadata"]["technology"])

    def test_source_packages_validate_and_exclude_authoritative_types(self):
        for name, package in self.packages.items():
            with self.subTest(name=name):
                self.assertEqual("1.0", package["manifest"]["contractVersion"])
                self.assertTrue(package["manifest"]["metadata"]["checksumsValidated"])
                self.assertGreater(package["manifest"]["counts"]["nodes"], 0)
                self.assertGreater(package["manifest"]["counts"]["edges"], 0)
                node_types = {row["node_type"] for row in package["nodes"]}
                self.assertFalse(FORBIDDEN_SOURCE_TYPES & node_types)
                self.assertFalse({row["edge_type"] for row in package["edges"]} & {"DATA_FLOW"})

    def test_angular_extractor_outputs_routes_actions_api_calls_and_issues(self):
        package = self.packages["angular"]
        node_types = {row["node_type"] for row in package["nodes"]}
        self.assertTrue({"ANGULAR_PROJECT", "SCREEN", "ANGULAR_COMPONENT", "ANGULAR_SERVICE", "UI_ACTION", "API_CALL_REFERENCE"} <= node_types)
        node_ids = {row["node_id"] for row in package["nodes"]}
        edge_types = {row["edge_type"] for row in package["edges"]}
        self.assertIn("screen:customer-web:/orders/new", node_ids)
        self.assertIn("api-call:customer-web:POST:/order-api/api/orders", node_ids)
        self.assertNotIn("RESOLVES_TO", edge_types)
        self.assertNotIn("CALLS_API", edge_types)
        self.assertFalse(any(row["target_node_id"].startswith("api-operation:") for row in package["edges"]))
        issue_types = {row["issue_type"] for row in package["issues"]}
        self.assertTrue({"DYNAMIC_CONFIG_KEY", "API_ROUTE_NOT_MATCHED", "API_ROUTE_AMBIGUOUS"} <= issue_types)

    def test_dotnet_api_extractor_outputs_api_controller_repository_and_data_edges(self):
        package = self.packages["dotnet_api"]
        node_ids = {row["node_id"] for row in package["nodes"]}
        edge_targets = {row["target_node_id"] for row in package["edges"]}
        unresolved = [row for row in package["nodes"] if row["node_type"] == "UNRESOLVED_REFERENCE"]
        self.assertIn(api_operation_id("order-api", "POST", "/api/orders"), node_ids)
        self.assertIn(table_id("ORDER_DB", "ORDER_HEADER"), edge_targets)
        self.assertTrue(any(json.loads(row["properties_json"]).get("raw_reference") == "PKG_ORDER.CREATE_ORDER" and row["node_id"] in edge_targets for row in unresolved))
        issue_types = {row["issue_type"] for row in package["issues"]}
        self.assertTrue({"TABLE_NOT_IMPORTED", "COLUMN_NOT_IMPORTED"} <= issue_types)

    def test_batch_extractor_outputs_executables_job_links_modes_and_data_edges(self):
        package = self.packages["batch"]
        node_ids = {row["node_id"] for row in package["nodes"]}
        edge_pairs = {(row["source_node_id"], row["edge_type"], row["target_node_id"]) for row in package["edges"]}
        edge_targets = {row["target_node_id"] for row in package["edges"]}
        unresolved = [row for row in package["nodes"] if row["node_type"] == "UNRESOLVED_REFERENCE"]
        self.assertIn("executable:batch-system:orderfulfillment.exe", node_ids)
        self.assertIn("command-mode:order-fulfillment:allocate", node_ids)
        self.assertIn((stable_node_id("job", "batch-system", "ORDER_DAILY", "ALLOCATE_ORDERS"), "STARTS", "executable:batch-system:orderfulfillment.exe"), edge_pairs)
        self.assertTrue(any(json.loads(row["properties_json"]).get("raw_reference") == "PKG_ORDER.ALLOCATE_ORDER" and row["node_id"] in edge_targets for row in unresolved))
        self.assertIn("EXECUTABLE_NOT_MAPPED", {row["issue_type"] for row in package["issues"]})

    def test_plsql_extractor_outputs_database_objects_remote_refs_and_dynamic_sql(self):
        package = self.packages["plsql"]
        node_types = {row["node_type"] for row in package["nodes"]}
        edge_types = {row["edge_type"] for row in package["edges"]}
        self.assertTrue({"DATABASE", "PLSQL_PACKAGE", "PROCEDURE", "FUNCTION", "LOCAL_ROUTINE", "TRIGGER", "SEQUENCE", "DATABASE_LINK", "EXTERNAL_DATABASE_OBJECT", "EXTERNAL_SYSTEM", "EXTERNAL_API_OPERATION"} <= node_types)
        self.assertTrue({"INSERTS", "UPDATES", "DELETES", "REMOTE_READS", "CALLS", "CALLS_API", "TRIGGERS", "USES"} <= edge_types)
        issue_types = {row["issue_type"] for row in package["issues"]}
        self.assertTrue({"DYNAMIC_SQL", "EXTERNAL_OBJECT"} <= issue_types)

    def test_sql_file_extractor_outputs_sql_file_crud_and_quality_issues(self):
        package = self.packages["sql_files"]
        self.assertIn(sql_file_id("order-ops", "demo-sources/order-ops/reconcile_orders.sql"), {row["node_id"] for row in package["nodes"]})
        self.assertIn(table_id("ORDER_DB", "ORDER_DAILY_SUMMARY"), {row["target_node_id"] for row in package["edges"]})
        self.assertTrue({"READS", "MERGES"} <= {row["edge_type"] for row in package["edges"]})
        self.assertTrue({"PARSE_ERROR", "INVALID_CONFIG"} <= {row["issue_type"] for row in package["issues"]})

    def test_csv_normalizer_outputs_authoritative_csvs_and_report(self):
        expected = {
            "tables.csv",
            "jobnet.csv",
            "executable-mappings.csv",
            "localized-metadata.csv",
            "normalization-report.json",
        }
        self.assertTrue(expected <= {path.name for path in self.normalizer_output.iterdir()})
        self.assertEqual(["database", "table_code", "table_name_ja", "table_name_en"], list(_read_csv(self.normalizer_output / "tables.csv")[0].keys()))
        self.assertEqual(["column_code", "column_name_ja", "column_name_en", "ordinal_position", "data_type", "nullable", "note", "relation_table"], list(_read_csv(self.normalizer_output / "tables/ORDER_HEADER.csv")[0].keys()))
        order_id = next(row for row in _read_csv(self.normalizer_output / "tables/ORDER_LINE.csv") if row["column_code"] == "ORDER_ID")
        self.assertEqual("ORDER_HEADER", order_id["relation_table"])
        report = json.loads((self.normalizer_output / "normalization-report.json").read_text(encoding="utf-8"))
        self.assertEqual([], report["issues"])
        self.assertGreater(report["files"]["tables.csv"]["rows"], 0)


if __name__ == "__main__":
    unittest.main()
