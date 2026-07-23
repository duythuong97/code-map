from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from application.backend.importer import pipeline
from application.backend.importer.package_validator import validate_package
from contract.graph_contract import stable_node_id, table_id
from extractors.package_support.package_writer import load_authoritative_ids

ROOT = Path(__file__).resolve().parents[2]
BATCH_EXTRACTOR = ROOT / "extractors/dotnet-batch-extractor/main.py"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


class BatchExtractorRequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.work = Path(self.tmp.name)
        self.source_root = self.work / "src"
        self.input_root = self.work / "input-data"
        self.output_root = self.work / "package"
        self._write_fixture()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fixture(self) -> None:
        _write(
            self.source_root / "BatchFolderA/Nested/FolderAWorker.csproj",
            """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <TargetFramework>net9.0</TargetFramework>
                <OutputType>Exe</OutputType>
                <AssemblyName>FolderA.Assembly</AssemblyName>
                <TargetName>FolderAWorker</TargetName>
              </PropertyGroup>
                            <ItemGroup>
                                <ProjectReference Include="../../DataAccess/SharedDataAccess.csproj" />
                            </ItemGroup>
            </Project>
            """,
        )
        _write(
            self.source_root / "BatchFolderA/Nested/ProgramA.cs",
            """
            public class EntryA
            {
                public static void Main(string[] args)
                {
                    var mode = args.Length > 0 ? args[0] : "";
                    if (mode == "allocate")
                    {
                        new AllocationHandler().Handle();
                    }
                    switch (mode)
                    {
                        case "cross":
                            new CrossFolderHandler().Handle();
                            break;
                    }
                }
            }

            public class AllocationHandler
            {
                public void Handle()
                {
                    new OrderRepository().Allocate();
                }
            }

            public class OrderRepository
            {
                public void Allocate()
                {
                    var procedure = "PKG_A.REACHABLE_PROC";
                    var sql = "insert into TABLE_A (ID) values (1)";
                }
            }

            public class CrossFolderHandler
            {
                public void Handle()
                {
                    new CrossFolderRepository().WriteAudit();
                }
            }

            public class UnusedRepository
            {
                public void Write()
                {
                    var sql = "insert into UNREACHABLE_A (ID) values (1)";
                }
            }
            """,
        )
        _write(
            self.source_root / "DataAccess/SharedDataAccess.csproj",
            """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <TargetFramework>net9.0</TargetFramework>
                <OutputType>Library</OutputType>
                <AssemblyName>Shared.DataAccess</AssemblyName>
              </PropertyGroup>
            </Project>
            """,
        )
        _write(
            self.source_root / "DataAccess/CrossFolderRepository.cs",
            """
            public class CrossFolderRepository
            {
                public void WriteAudit()
                {
                    var sql = "insert into CROSS_AUDIT (ID) values (1)";
                }
            }
            """,
        )
        _write(
            self.source_root / "BatchFolderB/Main/FolderBMain.csproj",
            """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <TargetFramework>net9.0</TargetFramework>
                <AssemblyName>FolderB.Assembly</AssemblyName>
                <TargetName>FolderBMain</TargetName>
              </PropertyGroup>
            </Project>
            """,
        )
        _write(
            self.source_root / "BatchFolderB/Main/ProgramB.cs",
            """
            public class EntryB
            {
                public static void Main(string[] args)
                {
                    new BillingRepository().Update();
                }
            }

            public class BillingRepository
            {
                public void Update()
                {
                    var sql = "update TABLE_B set ID = 1";
                }
            }
            """,
        )
        _write(
            self.source_root / "BatchFolderB/TopLevel/FolderBTop.csproj",
            """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <TargetFramework>net9.0</TargetFramework>
                <AssemblyName>FolderB.TopLevel</AssemblyName>
                <TargetName>FolderBTop</TargetName>
              </PropertyGroup>
            </Project>
            """,
        )
        _write(self.source_root / "BatchFolderB/TopLevel/Program.cs", "System.Console.WriteLine(\"top level\");")
        _write(
            self.source_root / "BatchFolderA/bin/Ghost/Ghost.csproj",
            """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup><OutputType>Exe</OutputType><TargetName>Ghost</TargetName></PropertyGroup>
            </Project>
            """,
        )
        _write(
            self.source_root / "BatchFolderA/bin/Ghost/Ghost.cs",
            """
            public class GhostEntry { public static void Main() { var sql = "insert into BIN_TABLE (ID) values (1)"; } }
            """,
        )
        _write(
            self.source_root / "BatchFolderA/Nested/EntryATests.cs",
            """
            public class EntryATests { public static void Main() { var sql = "insert into TEST_TABLE (ID) values (1)"; } }
            """,
        )
        _write(
            self.input_root / "tables.csv",
            """
            database,table_code,table_name_ja,table_name_en
            DB_A,TABLE_A,,TABLE_A
            DB_A,UNREACHABLE_A,,UNREACHABLE_A
            DB_A,BIN_TABLE,,BIN_TABLE
            DB_A,TEST_TABLE,,TEST_TABLE
            DB_B,TABLE_B,,TABLE_B
            DB_ACCESS,CROSS_AUDIT,,CROSS_AUDIT
            """,
        )
        for table in ("TABLE_A", "UNREACHABLE_A", "BIN_TABLE", "TEST_TABLE", "TABLE_B", "CROSS_AUDIT"):
            _write(
                self.input_root / "tables" / f"{table}.csv",
                """
                column_code,column_name_ja,column_name_en,ordinal_position,data_type,nullable,note
                ID,,ID,1,NUMBER,false,
                """,
            )
        _write(
            self.input_root / "jobnet.csv",
            """
            jobnet_id,jobnet_name,job_id,job_name,executable_name,arguments,predecessor_job_id
            SA08,SA08,JOB_EXPLICIT,Explicit configured executable,ConfiguredA.exe,--mode allocate,
            SA08,SA08,JOB_FILENAME,Filename executable,FolderBMain.exe,,JOB_EXPLICIT
            SA08,SA08,JOB_ASSEMBLY,Assembly executable,FolderB.Assembly,,JOB_FILENAME
            SA08,SA08,JOB_ALIAS,Alias executable,folder-a-worker,,JOB_ASSEMBLY
            SA08,SA08,JOB_NOFUZZY,No fuzzy executable,FolderAWork,,JOB_ALIAS
            """,
        )
        _write(
            self.input_root / "executable-mappings.csv",
            """
            job_system,executable_name,executable_scope,canonical_executable_name,alias
            batch-system,ConfiguredA.exe,batch-system,FolderAWorker.exe,folder-a-worker
            """,
        )

    def _run_extractor(self) -> dict[str, Any]:
        config = {
            "type": "dotnet-batch",
            "source": "sa08-batch",
            "repository": "sa08-batch",
            "system": "batch-system",
            "executableScope": "batch-system",
            "root": str(self.source_root),
            "inputData": str(self.input_root),
            "output": str(self.output_root),
            "folders": [
                {"path": "BatchFolderA", "database": "DB_A"},
                {"path": "BatchFolderB", "database": "DB_B"},
                {"path": "DataAccess", "database": "DB_ACCESS"},
            ],
            "executables": ["FolderAWorker.exe"],
            "commandModes": ["allocate", "cross"],
        }
        config_path = self.work / "batch-config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(BATCH_EXTRACTOR), "--config", str(config_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(f"batch extractor failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
        allowed_ids = load_authoritative_ids(self.input_root)
        for row in _read_csv(self.output_root / "nodes.csv"):
            allowed_ids.add(row["node_id"])
        return validate_package(self.output_root, allowed_ids, workspace_root=self.work)

    def test_batch_extractor_discovers_projects_executables_entries_modes_and_reachable_data(self) -> None:
        package = self._run_extractor()
        nodes = package["nodes"]
        edges = package["edges"]
        issues = package["issues"]
        node_ids = {row["node_id"] for row in nodes}
        edge_pairs = {(row["source_node_id"], row["edge_type"], row["target_node_id"]) for row in edges}
        edge_targets = {row["target_node_id"] for row in edges}

        exe_a = stable_node_id("executable", "batch-system", "folderaworker.exe")
        exe_b = stable_node_id("executable", "batch-system", "folderbmain.exe")
        exe_top = stable_node_id("executable", "batch-system", "folderbtop.exe")
        self.assertIn(exe_a, node_ids)
        self.assertIn(exe_b, node_ids)
        self.assertIn(exe_top, node_ids)
        self.assertNotIn(stable_node_id("executable", "batch-system", "ghost.exe"), node_ids)

        self.assertIn((exe_a, "ENTRY_IN", stable_node_id("executable-entry", "folder-a-worker", "main")), edge_pairs)
        self.assertIn((exe_b, "ENTRY_IN", stable_node_id("executable-entry", "folderbmain", "main")), edge_pairs)
        self.assertIn((exe_top, "ENTRY_IN", stable_node_id("executable-entry", "folderbtop", "main")), edge_pairs)

        mode_id = stable_node_id("command-mode", "folder-a-worker", "allocate")
        handler_id = stable_node_id("local-routine", "sa08-batch", "AllocationHandler.Handle")
        self.assertIn(mode_id, node_ids)
        self.assertIn((mode_id, "CALLS", handler_id), edge_pairs)
        cross_mode_id = stable_node_id("command-mode", "folder-a-worker", "cross")
        cross_handler_id = stable_node_id("local-routine", "sa08-batch", "CrossFolderHandler.Handle")
        self.assertIn(cross_mode_id, node_ids)
        self.assertIn((cross_mode_id, "CALLS", cross_handler_id), edge_pairs)

        self.assertIn(table_id("DB_A", "TABLE_A"), edge_targets)
        procedure_refs = [row for row in nodes if row["node_type"] == "UNRESOLVED_REFERENCE"]
        self.assertTrue(any(json.loads(row["properties_json"]).get("raw_reference") == "PKG_A.REACHABLE_PROC" and row["node_id"] in edge_targets for row in procedure_refs))
        self.assertIn(table_id("DB_B", "TABLE_B"), edge_targets)
        self.assertIn(table_id("DB_ACCESS", "CROSS_AUDIT"), edge_targets)
        self.assertNotIn(table_id("DB_A", "UNREACHABLE_A"), edge_targets)
        self.assertNotIn(table_id("DB_A", "BIN_TABLE"), edge_targets)
        self.assertNotIn(table_id("DB_A", "TEST_TABLE"), edge_targets)

        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_EXPLICIT"), "STARTS", exe_a), edge_pairs)
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_FILENAME"), "STARTS", exe_b), edge_pairs)
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_ASSEMBLY"), "STARTS", exe_b), edge_pairs)
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_ALIAS"), "STARTS", exe_a), edge_pairs)
        self.assertEqual(
            ["EXECUTABLE_NOT_MAPPED"],
            [row["issue_type"] for row in issues if row["raw_reference"] == "FolderAWork"],
        )


class PipelineExecutableResolverTest(unittest.TestCase):
    def test_resolver_uses_exact_explicit_filename_assembly_alias_without_fuzzy(self) -> None:
        db = sqlite3.connect(":memory:")
        db.executescript(pipeline.SCHEMA)
        db.execute("INSERT INTO graph_sources VALUES(?,?,datetime('now'))", ("test", "test"))
        exe_a = stable_node_id("executable", "batch-system", "folderaworker.exe")
        exe_b = stable_node_id("executable", "batch-system", "folderbmain.exe")
        self._insert_node(db, exe_a, "EXECUTABLE", "folderaworker.exe", {"assemblyNames": ["FolderA.Assembly"], "aliases": ["folder-a-worker"]})
        self._insert_node(db, exe_b, "EXECUTABLE", "folderbmain.exe", {"assemblyNames": ["FolderB.Assembly"]})
        jobs = {
            "JOB_EXPLICIT": "ConfiguredA.exe",
            "JOB_FILENAME": "FolderBMain.exe",
            "JOB_ASSEMBLY": "FolderB.Assembly",
            "JOB_ALIAS": "folder-a-worker",
            "JOB_NOFUZZY": "FolderAWork",
        }
        for job_id, executable_name in jobs.items():
            self._insert_node(
                db,
                stable_node_id("job", "batch-system", "SA08", job_id),
                "JOB",
                job_id,
                {"job_system": "batch-system", "executable_scope": "batch-system", "executable_name": executable_name},
            )
        db.execute(
            "INSERT INTO graph_executable_mappings VALUES(?,?,?,?,?)",
            ("batch-system", "ConfiguredA.exe", "batch-system", "FolderAWorker.exe", "folder-a-worker"),
        )

        pipeline.resolve(db)
        starts = {
            (row[0], row[2])
            for row in db.execute("SELECT source_node_id,edge_type,target_node_id FROM graph_edges WHERE edge_type='STARTS'")
        }
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_EXPLICIT"), exe_a), starts)
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_FILENAME"), exe_b), starts)
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_ASSEMBLY"), exe_b), starts)
        self.assertIn((stable_node_id("job", "batch-system", "SA08", "JOB_ALIAS"), exe_a), starts)
        self.assertNotIn((stable_node_id("job", "batch-system", "SA08", "JOB_NOFUZZY"), exe_a), starts)
        issue = db.execute("SELECT issue_type,raw_reference FROM resolution_issues WHERE raw_reference='FolderAWork'").fetchone()
        self.assertEqual(("EXECUTABLE_NOT_MAPPED", "FolderAWork"), issue)

    @staticmethod
    def _insert_node(db: sqlite3.Connection, node_id: str, node_type: str, technical_name: str, props: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                node_id,
                node_type,
                technical_name,
                technical_name,
                technical_name,
                "batch-system",
                "",
                "sa08-batch",
                "MAIN",
                1.0,
                json.dumps(props),
                "test",
            ),
        )


if __name__ == "__main__":
    unittest.main()
