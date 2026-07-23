import json
import tempfile
import unittest
from pathlib import Path

from extractors.package_support.package_writer import configured_files, load_config


class ConfigPortabilityTest(unittest.TestCase):
    def test_relative_config_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "scan.json"
            config_path.write_text(json.dumps({"root": "external-source"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root must be an absolute path"):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
