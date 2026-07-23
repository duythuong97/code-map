from __future__ import annotations

import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from application.backend.importer.package_validator import CSV_HEADERS, PACKAGE_CSV_HEADERS, validate_package
from contract.graph_contract import canonical_edge_id


def _write_csv(path: Path, name: str, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PACKAGE_CSV_HEADERS[name], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class ContractValidatorRequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.package = self.root / "package"
        (self.root / "test-web" / "src").mkdir(parents=True)
        (self.root / "test-web" / "src/app.ts").write_text("submit();\nconsole.log('issue');\nreturn;\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rows(self) -> dict[str, list[dict[str, str]]]:
        source = "screen:test-web:/orders"
        target = "service:test-web:OrderService"
        edge_id = canonical_edge_id(source, "CALLS", target, "", "TECHNICAL")
        return {
            "nodes": [
                {
                    "node_id": source,
                    "node_type": "SCREEN",
                    "technical_name": "/orders",
                    "qualified_name": "test-web:/orders",
                    "default_display_name": "Orders",
                    "system_key": "order-system",
                    "database_key": "",
                    "repository_key": "test-web",
                    "graph_role": "MAIN",
                    "confidence": "1.0",
                    "properties_json": "{}",
                },
                {
                    "node_id": target,
                    "node_type": "SERVICE",
                    "technical_name": "OrderService",
                    "qualified_name": "test-web.OrderService",
                    "default_display_name": "Order Service",
                    "system_key": "order-system",
                    "database_key": "",
                    "repository_key": "test-web",
                    "graph_role": "TECHNICAL",
                    "confidence": "0.9",
                    "properties_json": "{}",
                },
            ],
            "edges": [
                {
                    "edge_id": edge_id,
                    "source_node_id": source,
                    "target_node_id": target,
                    "edge_type": "CALLS",
                    "graph_layer": "TECHNICAL",
                    "raw_operation": "",
                    "confidence": "0.8",
                    "properties_json": "{}",
                }
            ],
            "evidence": [
                {
                    "evidence_id": "ev:call-submit",
                    "target_type": "EDGE",
                    "target_id": edge_id,
                    "source_path": "src/app.ts",
                    "start_line": "1",
                    "end_line": "1",
                    "start_column": "1",
                    "end_column": "8",
                    "evidence_kind": "CALL_SITE",
                    "extractor_name": "test-extractor",
                    "confidence": "1.0",
                    "snippet": "submit();",
                    "properties_json": "{}",
                }
            ],
            "issues": [
                {
                    "issue_id": "issue:invalid-config",
                    "issue_type": "INVALID_CONFIG",
                    "severity": "WARNING",
                    "source_node_id": source,
                    "raw_reference": "missing endpoint",
                    "database_key": "",
                    "source_path": "src/app.ts",
                    "start_line": "2",
                    "message": "Missing endpoint configuration",
                    "properties_json": "{}",
                }
            ],
        }

    def _write_package(self, rows: dict[str, list[dict[str, str]]], *, version: str = "1.0", checksums: bool = False) -> Path:
        self.package.mkdir(parents=True, exist_ok=True)
        files: dict[str, dict[str, object]] = {}
        for name, group in rows.items():
            path = self.package / f"{name}.csv"
            _write_csv(path, name, group)
            files[path.name] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        if version == "1.0":
            manifest_files = {name: f"{name}.csv" for name in CSV_HEADERS}
            if "localized_texts" in rows:
                manifest_files["localized_texts"] = "localized_texts.csv"
            manifest = {
                "contractVersion": "1.0",
                "extractor": {"name": "test-extractor", "version": "1.0.0"},
                "source": {"sourceKey": "test-source", "repositoryKey": "test-web", "revision": "abc123"},
                "generatedAt": "2026-07-20T09:00:00+07:00",
                "files": manifest_files,
                "statistics": {"filesScanned": 1, **{name: len(group) for name, group in rows.items()}},
            }
            if checksums:
                manifest["checksums"] = files
        else:
            manifest = {
                "contractVersion": "2.0.0",
                "packageId": "test-package",
                "sourceId": "test-source",
                "createdAt": "2026-07-20T09:00:00+07:00",
                "filesScanned": 1,
                "files": files,
                "counts": {name: len(group) for name, group in rows.items()},
                "metadata": {},
            }
        (self.package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return self.package

    def test_requirement_manifest_shape_is_supported_and_normalized(self) -> None:
        root = self._write_package(self._rows(), version="1.0")
        package = validate_package(root, workspace_root=self.root)
        self.assertEqual("1.0", package["manifest"]["contractVersion"])
        self.assertEqual("test-extractor:test-source", package["manifest"]["packageId"])
        self.assertEqual("test-source", package["manifest"]["sourceId"])
        self.assertEqual(2, package["manifest"]["counts"]["nodes"])
        self.assertFalse(package["manifest"]["metadata"]["checksumsValidated"])

    def test_requirement_manifest_with_checksums_is_production_validated(self) -> None:
        root = self._write_package(self._rows(), version="1.0", checksums=True)
        package = validate_package(root, workspace_root=self.root)
        self.assertEqual("1.0", package["manifest"]["contractVersion"])
        self.assertTrue(package["manifest"]["metadata"]["checksumsValidated"])

    def test_optional_localized_texts_csv_is_supported(self) -> None:
        rows = self._rows()
        rows["localized_texts"] = [
            {
                "target_type": "SCREEN",
                "target_id": rows["nodes"][0]["node_id"],
                "field_name": "name",
                "locale": "ja",
                "value": "注文一覧",
                "source_kind": "EXTRACTED",
                "review_status": "PENDING",
                "author_name": "test-extractor",
                "created_at": "2026-07-20T09:00:00+07:00",
                "updated_at": "2026-07-20T09:00:00+07:00",
            }
        ]
        root = self._write_package(rows, version="1.0", checksums=True)
        package = validate_package(root, workspace_root=self.root)
        self.assertEqual(rows["localized_texts"], package["localized_texts"])
        self.assertEqual(1, package["manifest"]["counts"]["localized_texts"])
        self.assertIn("localized_texts.csv", package["manifest"]["files"])

    def test_rejects_legacy_additive_manifest_shape(self) -> None:
        root = self._write_package(self._rows(), version="2.0.0")
        with self.assertRaisesRegex(ValueError, "unsupported contract version"):
            validate_package(root, workspace_root=self.root)

    def test_rejects_unstable_node_id_and_bad_json_or_confidence(self) -> None:
        cases = []
        rows = self._rows()
        rows["nodes"][0]["node_id"] = "service:test-web:/orders"
        rows["edges"][0]["source_node_id"] = rows["nodes"][0]["node_id"]
        rows["edges"][0]["edge_id"] = canonical_edge_id(rows["edges"][0]["source_node_id"], "CALLS", rows["edges"][0]["target_node_id"], "", "TECHNICAL")
        rows["evidence"][0]["target_id"] = rows["edges"][0]["edge_id"]
        rows["issues"][0]["source_node_id"] = rows["nodes"][0]["node_id"]
        cases.append(("wrong node prefix", rows))

        rows = self._rows()
        rows["nodes"][0]["confidence"] = "1.01"
        cases.append(("node confidence", rows))

        rows = self._rows()
        rows["edges"][0]["properties_json"] = "[]"
        cases.append(("edge properties", rows))

        for label, candidate in cases:
            with self.subTest(label=label):
                root = self._write_package(candidate)
                with self.assertRaises(ValueError):
                    validate_package(root, workspace_root=self.root)

    def test_accepts_local_routine_with_signature_preserving_routine_id(self) -> None:
        rows = self._rows()
        rows["nodes"].append(
            {
                "node_id": "procedure:ORDER_DB:PKG_ORDER:VALIDATE_LINES:NUMBER",
                "node_type": "LOCAL_ROUTINE",
                "technical_name": "VALIDATE_LINES",
                "qualified_name": "ORDER_DB.PKG_ORDER.VALIDATE_LINES",
                "default_display_name": "VALIDATE_LINES",
                "system_key": "order-db",
                "database_key": "ORDER_DB",
                "repository_key": "order-db",
                "graph_role": "TECHNICAL",
                "confidence": "1.0",
                "properties_json": "{}",
            }
        )
        root = self._write_package(rows)
        package = validate_package(root, workspace_root=self.root)
        self.assertIn(rows["nodes"][-1], package["nodes"])

    def test_rejects_noncanonical_edge_id(self) -> None:
        rows = self._rows()
        rows["edges"][0]["edge_id"] = "edge:" + "0" * 64
        rows["evidence"][0]["target_id"] = rows["edges"][0]["edge_id"]
        root = self._write_package(rows)
        with self.assertRaisesRegex(ValueError, "non-canonical edge_id"):
            validate_package(root, workspace_root=self.root)

    def test_rejects_bad_evidence_paths_ranges_confidence_and_properties(self) -> None:
        cases = {
            "absolute path": {"source_path": "/tmp/secret.ts"},
            "path traversal": {"source_path": "../secret.ts"},
            "line out of bounds": {"start_line": "9", "end_line": "9"},
            "reversed line range": {"start_line": "2", "end_line": "1"},
            "reversed column range": {"start_column": "8", "end_column": "1"},
            "bad confidence": {"confidence": "false"},
            "bad properties": {"properties_json": "[]"},
        }
        for label, patch in cases.items():
            rows = self._rows()
            rows["evidence"][0].update(patch)
            with self.subTest(label=label):
                root = self._write_package(rows)
                with self.assertRaises(ValueError):
                    validate_package(root, workspace_root=self.root)

    def test_rejects_duplicate_evidence_canonical_key(self) -> None:
        rows = self._rows()
        duplicate = copy.deepcopy(rows["evidence"][0])
        duplicate["evidence_id"] = "ev:duplicate"
        duplicate["snippet"] = "different snippet does not change evidence key"
        rows["evidence"].append(duplicate)
        root = self._write_package(rows)
        with self.assertRaisesRegex(ValueError, "duplicate evidence canonical key"):
            validate_package(root, workspace_root=self.root)


if __name__ == "__main__":
    unittest.main()
