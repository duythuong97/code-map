from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANGULAR_EXTRACTOR = ROOT / "extractors/angular-extractor/main.py"
ANGULAR_CONFIG = ROOT / "configs/angular-customer-web.json"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class AngularRequirementsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)
        config = json.loads(ANGULAR_CONFIG.read_text(encoding="utf-8"))
        config["output"] = str(cls.work / "customer-web-package")
        cls.config_path = cls.work / "customer-web.json"
        cls.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        cls.output = Path(config["output"])
        cls._run(cls.config_path)
        cls.nodes = _read_csv(cls.output / "nodes.csv")
        cls.edges = _read_csv(cls.output / "edges.csv")
        cls.issues = _read_csv(cls.output / "issues.csv")
        cls.manifest = json.loads((cls.output / "manifest.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    @staticmethod
    def _run(config_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(ANGULAR_EXTRACTOR), "--config", str(config_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(f"Angular extractor failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    def test_config_uses_required_minimal_shape_without_endpoint_inventory(self) -> None:
        config = json.loads(ANGULAR_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual("angular", config["type"])
        for key in ("source", "root", "folders", "appConfig", "output"):
            self.assertIn(key, config)
        self.assertEqual("src/assets/appconfig.json", config["appConfig"])
        self.assertNotIn("apiTargets", config)
        self.assertNotIn("knownApiOperations", config)

    def test_runtime_config_patterns_and_request_calls_become_api_call_references(self) -> None:
        node_ids = {row["node_id"] for row in self.nodes}
        expected = {
            "api-call:customer-web:POST:/order-api/api/orders",
            "api-call:customer-web:GET:/order-api/api/orders/{id}",
            "api-call:customer-web:DELETE:/order-api/api/orders/{id}",
            "api-call:customer-web:POST:/order-api/api/orders/retry",
            "api-call:customer-web:PATCH:/order-api/api/orders/{id}/status",
            "api-call:customer-web:POST:/order-api/api/shipments/{id}/confirm",
        }
        self.assertTrue(expected <= node_ids)
        self.assertIn("screen:customer-web:/orders/new", node_ids)
        self.assertIn("screen:customer-web:/orders/{id}", node_ids)

    def test_dynamic_config_key_is_reported_not_guessed(self) -> None:
        dynamic_issues = [row for row in self.issues if row["issue_type"] == "DYNAMIC_CONFIG_KEY"]
        self.assertTrue(dynamic_issues)
        self.assertIn("this.config[endpointName]", {row["raw_reference"] for row in dynamic_issues})
        node_ids = {row["node_id"] for row in self.nodes}
        self.assertNotIn("api-call:customer-web:POST:/endpointName", node_ids)

    def test_extraction_does_not_resolve_to_api_operation_from_config_inventory(self) -> None:
        self.assertNotIn("RESOLVES_TO", {row["edge_type"] for row in self.edges})
        self.assertNotIn("CALLS_API", {row["edge_type"] for row in self.edges})

    def test_only_fully_resolved_template_to_service_calls_create_ui_actions(self) -> None:
        action_ids = {row["node_id"] for row in self.nodes if row["node_type"] == "UI_ACTION"}
        self.assertIn("ui-action:customer-web:submitorder", action_ids)
        self.assertNotIn("ui-action:customer-web:load", action_ids)
        self.assertNotIn("ui-action:customer-web:cancelorder", action_ids)

    def test_scan_excludes_node_modules_dist_angular_spec_and_test_sources(self) -> None:
        root = self.work / "excluded-source" / "customer-web"
        app = root / "src" / "app"
        assets = root / "src" / "assets"
        assets.mkdir(parents=True)
        app.mkdir(parents=True)
        (assets / "appconfig.json").write_text(json.dumps({"live": "/live-api/api/live"}) + "\n", encoding="utf-8")
        (app / "live.ts").write_text(
            "import { HttpClient } from '@angular/common/http';\n"
            "export const routes = [{ path: 'live', component: 'LiveComponent' }];\n"
            "export class LiveService { constructor(private readonly http: HttpClient, private readonly config: any) {} "
            "ok() { return this.http.get(this.config.live); } }\n",
            encoding="utf-8",
        )
        ignored_source = (
            "import { HttpClient } from '@angular/common/http';\n"
            "export class IgnoredService { constructor(private readonly http: HttpClient) {} "
            "ignored() { return this.http.get('/ignored-api/api/ignored'); } }\n"
        )
        for relative in (
            "node_modules/ignored.ts",
            "dist/ignored.ts",
            ".angular/ignored.ts",
            "ignored.spec.ts",
            "ignored.test.ts",
        ):
            path = app / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ignored_source, encoding="utf-8")
        output = self.work / "excluded-package"
        config_path = self.work / "excluded.json"
        config_path.write_text(
            json.dumps(
                {
                    "type": "angular",
                    "source": "req-exclusions",
                    "root": str(root),
                    "folders": ["src/app"],
                    "appConfig": "src/assets/appconfig.json",
                    "output": str(output),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._run(config_path)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        node_ids = {row["node_id"] for row in _read_csv(output / "nodes.csv")}
        self.assertEqual("1.0", manifest["contractVersion"])
        self.assertEqual(1, manifest["statistics"]["filesScanned"])
        self.assertIn("api-call:req-exclusions:GET:/live-api/api/live", node_ids)
        self.assertNotIn("api-call:req-exclusions:GET:/ignored-api/api/ignored", node_ids)

    def test_lazy_loaded_routes_create_screens(self) -> None:
        root = self.work / "lazy-source" / "customer-web"
        app = root / "src" / "app"
        assets = root / "src" / "assets"
        assets.mkdir(parents=True)
        app.mkdir(parents=True)
        (assets / "appconfig.json").write_text("{}\n", encoding="utf-8")
        (app / "app.routes.ts").write_text(
            "export const routes = [\n"
            "  { path: 'reports/:id', loadComponent: () => import('./report.page').then(m => m.ReportPageComponent) },\n"
            "  { path: 'admin', loadChildren: () => import('./admin.routes').then(m => m.ADMIN_ROUTES) },\n"
            "];\n",
            encoding="utf-8",
        )
        (app / "report.page.ts").write_text(
            "export class ReportPageComponent { }\n",
            encoding="utf-8",
        )
        (app / "admin.routes.ts").write_text(
            "export const ADMIN_ROUTES = [];\n",
            encoding="utf-8",
        )
        output = self.work / "lazy-package"
        config_path = self.work / "lazy.json"
        config_path.write_text(
            json.dumps(
                {
                    "type": "angular",
                    "source": "req-lazy",
                    "root": str(root),
                    "folders": ["src/app"],
                    "appConfig": "src/assets/appconfig.json",
                    "output": str(output),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._run(config_path)
        nodes = _read_csv(output / "nodes.csv")
        node_ids = {row["node_id"] for row in nodes}
        screen_props = {row["node_id"]: json.loads(row["properties_json"]) for row in nodes if row["node_type"] == "SCREEN"}
        self.assertIn("screen:req-lazy:/reports/{id}", node_ids)
        self.assertIn("screen:req-lazy:/admin", node_ids)
        self.assertIn("angular-component:req-lazy:reportpage", node_ids)
        self.assertEqual("loadComponent", screen_props["screen:req-lazy:/reports/{id}"]["routeLoader"])
        self.assertEqual("loadChildren", screen_props["screen:req-lazy:/admin"]["routeLoader"])


if __name__ == "__main__":
    unittest.main()
