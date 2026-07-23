import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from application.runtime_env import project_path
from extractors.package_support.package_writer import configured_files, load_config

class ExplicitConfigTest(unittest.TestCase):
    def test_project_path_joins_explicit_absolute_root(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODE_MAP_PROJECT_ROOT": directory}):
            self.assertEqual(Path(directory).resolve() / "output" / "demo", project_path("output", "demo"))

    def test_source_container_selects_repository_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory) / "sources"
            repository = source_root / "customer-admin-web"
            repository.mkdir(parents=True)
            config_path = Path(directory) / "angular.json"
            config_path.write_text(json.dumps({"root": "${CODE_MAP_SOURCE_ROOT}/customer-admin-web"}), encoding="utf-8")
            with patch.dict(os.environ, {"CODE_MAP_SOURCE_ROOT": str(source_root)}):
                self.assertEqual(str(repository.resolve()), load_config(config_path)["root"])

    def test_absolute_env_paths_and_external_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "external-source"
            source.mkdir()
            (source / "nested.sql").write_text("select 1;\n", encoding="utf-8")
            config_path = base / "scan.json"
            config_path.write_text(json.dumps({
                "root": "${TEST_SOURCE_ROOT}",
                "output": str(base / "packages/result"),
                "inputData": str(base / "catalog"),
            }), encoding="utf-8")
            with patch.dict(os.environ, {"TEST_SOURCE_ROOT": str(source)}):
                config = load_config(config_path)
            self.assertEqual(["nested.sql"], [file.relative for file in configured_files(config, [".sql"])])

    def test_relative_runtime_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "scan.json"
            config_path.write_text(json.dumps({"root": "relative/source"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root must be an absolute path"):
                load_config(config_path)

    def test_missing_environment_variable_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "scan.json"
            config_path.write_text(json.dumps({"root": "${MISSING_TEST_ROOT}"}), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "Unresolved environment variable"):
                load_config(config_path)

if __name__ == "__main__":
    unittest.main()
