from __future__ import annotations

import csv
import hashlib
import json
import unittest
from pathlib import Path

from application.backend.importer.package_validator import ISSUE_TYPES, validate_package
from contract.graph_contract import EDGE_TYPES, NODE_TYPES, column_id, stable_node_id, table_id

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOTS = (
    ROOT / "output/angular/customer-web",
    ROOT / "output/dotnet-api/order-api",
    ROOT / "output/batch/order-fulfillment",
    ROOT / "output/plsql/order-db",
    ROOT / "output/sql-files/order-ops",
)
FORBIDDEN = {"TABLE", "COLUMN", "JOB", "JOB_NETWORK"}


def authoritative_nodes() -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    types: set[str] = set()
    with (ROOT / "input-data/tables.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ids.add(table_id(row["database"], row["table_code"]))
            with (ROOT / "input-data/tables" / f"{row['table_code']}.csv").open(newline="", encoding="utf-8") as columns:
                ids.update(column_id(row["database"], row["table_code"], column["column_code"]) for column in csv.DictReader(columns))
            types.update(("TABLE", "COLUMN"))
    with (ROOT / "input-data/jobnet.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ids.add(stable_node_id("job-network", "batch-system", row["jobnet_id"]))
            ids.add(stable_node_id("job", "batch-system", row["jobnet_id"], row["job_id"]))
            types.update(("JOB_NETWORK", "JOB"))
    return ids, types


class DemoPackagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.authoritative_ids, cls.authoritative_types = authoritative_nodes()
        package_node_ids = set()
        for root in PACKAGE_ROOTS:
            with (root / "nodes.csv").open(newline="", encoding="utf-8") as handle:
                package_node_ids.update(row["node_id"] for row in csv.DictReader(handle))
        cls.allowed_ids = cls.authoritative_ids | package_node_ids
        cls.packages = [validate_package(root, cls.allowed_ids) for root in PACKAGE_ROOTS]

    def test_exact_five_packages_and_no_combined_package(self):
        manifests = sorted((ROOT / "output").glob("*/*/manifest.json"))
        self.assertEqual(sorted(PACKAGE_ROOTS), [path.parent for path in manifests])
        self.assertFalse((ROOT / "output/complete-order-flow").exists())

    def test_manifest_counts_and_sha256_are_exact(self):
        for root, package in zip(PACKAGE_ROOTS, self.packages):
            manifest = package["manifest"]
            self.assertEqual("1.0", manifest["contractVersion"])
            self.assertTrue(manifest["metadata"]["checksumsValidated"])
            for name in ("nodes", "edges", "evidence", "issues"):
                path = root / f"{name}.csv"
                self.assertEqual(len(package[name]), manifest["counts"][name])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["files"][path.name]["sha256"])
                self.assertEqual(path.stat().st_size, manifest["files"][path.name]["bytes"])

    def test_source_packages_exclude_authoritative_types_and_refs_resolve(self):
        for package in self.packages:
            self.assertFalse(FORBIDDEN & {row["node_type"] for row in package["nodes"]})
            for edge in package["edges"]:
                self.assertIn(edge["source_node_id"], self.allowed_ids)
                self.assertIn(edge["target_node_id"], self.allowed_ids)

    def test_complete_contract_coverage(self):
        node_types = self.authoritative_types | {row["node_type"] for package in self.packages for row in package["nodes"]}
        edge_types = {row["edge_type"] for package in self.packages for row in package["edges"]}
        issue_types = {row["issue_type"] for package in self.packages for row in package["issues"]}
        self.assertEqual(NODE_TYPES, node_types)
        self.assertEqual(EDGE_TYPES, edge_types)
        self.assertEqual(ISSUE_TYPES, issue_types)

    def test_authoritative_inputs_and_demo_sources_exist(self):
        for name in ("tables.csv", "localized-metadata.csv", "jobnet.csv", "executable-mappings.csv"):
            self.assertGreater((ROOT / "input-data" / name).stat().st_size, 0)
        for package in self.packages:
            for row in package["evidence"]:
                self.assertTrue((ROOT / row["source_path"]).is_file(), row["source_path"])


if __name__ == "__main__":
    unittest.main()
