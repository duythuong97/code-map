"""init: one source per kind of code found in each repository under a folder."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_tree_exporter.init_config import init_config  # noqa: E402

WEB_PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>'
EXE_PROJECT = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType></PropertyGroup></Project>'
LIBRARY = '<Project Sdk="Microsoft.NET.Sdk"></Project>'
TESTS = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" /></ItemGroup></Project>'


class InitConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "sources"
        self.config = self.base / "map" / "code-map.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str = "") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def sources(self) -> dict[str, dict]:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        return {source["name"]: source for source in config["sources"]}

    def test_detects_each_kind_of_source(self) -> None:
        self.write("portal/.git/HEAD")
        self.write("portal/angular.json", json.dumps({"projects": {
            "app": {"projectType": "application", "sourceRoot": "projects/app/src"},
            "ui": {"projectType": "library", "sourceRoot": "projects/ui/src"},
        }}))
        self.write("orders/.git/HEAD")
        self.write("orders/src/Orders.Api/Orders.Api.csproj", WEB_PROJECT)
        self.write("orders/src/Orders.Jobs/Orders.Jobs.csproj", EXE_PROJECT)
        self.write("orders/src/Orders.Core/Orders.Core.csproj", LIBRARY)
        self.write("orders/tests/Orders.Api.Tests/Orders.Api.Tests.csproj", TESTS)
        self.write("orders/src/Orders.Api/bin/Debug/Copy.csproj", WEB_PROJECT)
        self.write("docs/readme.md", "nothing to scan")

        report = init_config(self.root, self.config, database="DB1", schema="APP")

        sources = self.sources()
        self.assertEqual(sources["portal"]["type"], "angular")
        self.assertEqual(sources["portal"]["folders"], ["portal/projects/app/src"])
        self.assertNotIn("database", sources["portal"])
        self.assertEqual(sources["orders-api"]["folders"], ["orders/src/Orders.Api/Orders.Api.csproj"])
        self.assertEqual(sources["orders-batch"]["folders"], ["orders/src/Orders.Jobs/Orders.Jobs.csproj"])
        self.assertEqual((sources["orders-api"]["database"], sources["orders-api"]["schema"]), ("DB1", "APP"))
        self.assertEqual(report["skipped"], ["docs"])

    def test_gateway_templates_are_a_source(self) -> None:
        self.write("pipelines/.git/HEAD")
        self.write("pipelines/gateway/public/routes.yml", "Resources:\n  M:\n    Type: AWS::ApiGateway::Method\n")
        self.write("pipelines/gateway/network.yml", "Resources:\n  V:\n    Type: AWS::EC2::VPC\n")
        self.write("pipelines/package.json", "{}")
        init_config(self.root, self.config, database="DB1")
        [source] = self.sources().values()
        self.assertEqual(source["type"], "api-gateway")
        self.assertEqual(source["folders"], ["pipelines/gateway/public/routes.yml"])
        self.assertNotIn("database", source)

    def test_angular_4_and_svn_working_copies(self) -> None:
        self.write("legacy-web/.svn/wc.db")
        self.write("legacy-web/.angular-cli.json", json.dumps({"apps": [{"root": "src"}, {"root": "admin/src"}]}))
        self.write("team-b/billing/.svn/wc.db")
        self.write("team-b/billing/load.ctl", "LOAD DATA")
        self.write("team-b/reports/.git/HEAD")
        self.write("team-b/reports/r.ctl", "LOAD DATA")

        init_config(self.root, self.config)

        sources = self.sources()
        self.assertEqual(sources["legacy-web"]["folders"], ["legacy-web/admin/src", "legacy-web/src"])
        self.assertEqual(sources["billing"]["folders"], ["team-b/billing"])
        self.assertEqual(sources["reports"]["folders"], ["team-b/reports"])

    def test_sql_folders_never_overlap(self) -> None:
        self.write("db/packages/orders.pkb", "CREATE OR REPLACE PACKAGE BODY orders AS END;")
        self.write("db/procs/p.sql", "create or replace procedure p is begin null; end;")
        self.write("db/scripts/seed.sql", "INSERT INTO t VALUES (1);")
        self.write("db/mixed/fix.sql", "UPDATE t SET a = 1;")
        self.write("db/mixed/loader.ctl", "LOAD DATA INFILE 'x' INTO TABLE t")
        self.write("db/mixed/f.fnc", "CREATE FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END;")

        init_config(self.root, self.config)

        sources = self.sources()
        self.assertEqual(sources["db-db"]["type"], "oracle-plsql")
        self.assertEqual(sources["db-db"]["folders"], ["db/mixed/f.fnc", "db/packages", "db/procs"])
        self.assertEqual(sources["db-sql"]["folders"], ["db/mixed/fix.sql", "db/mixed/loader.ctl", "db/scripts"])

    def test_grouping_folders_are_entered_and_reruns_keep_edits(self) -> None:
        self.write("team-a/one/.git/HEAD")
        self.write("team-a/one/x.ctl", "LOAD DATA")
        self.write("team-a/two/.git/HEAD")
        self.write("team-a/two/y.ctl", "LOAD DATA")
        init_config(self.root, self.config, name="billing")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(sorted(self.sources()), ["one", "two"])
        self.assertEqual(self.sources()["one"]["folders"], ["team-a/one"])
        self.assertEqual(config["root"], "../sources")
        config["sources"][0]["database"] = "EDITED"
        config["sources"].pop()  # "two" is not wanted
        config["ignoreRepositories"].append("two")
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.write("team-a/three/.git/HEAD")
        self.write("team-a/three/z.ctl", "LOAD DATA")

        report = init_config(None, self.config)

        self.assertEqual([source["name"] for source in report["added"]], ["three"])
        self.assertEqual(self.sources()["one"]["database"], "EDITED")
        self.assertEqual(self.sources()["three"]["system"], "billing")
        self.assertNotIn("two", self.sources())


if __name__ == "__main__":
    unittest.main()
