"""The generated corpus (tools/corpus-gen) through the real extractors: every pattern that static
extraction is expected to see must be in the graph, and every blind spot must be flagged by an issue.

This is the regression guard for lineage: before it existed, a package whose second procedure
updated a table the first one inserted into lost that dependency (table references were deduplicated
per file instead of per statement). The .NET patterns are scored in CI where the SDK is installed.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(REPOSITORY / "tools" / "corpus-gen"))

from corpus_gen.generator import generate  # noqa: E402
from corpus_gen.score import graph_impact, load_yaml, score_extract, score_impact  # noqa: E402

from code_tree_exporter.pipeline import _typescript_path, run_pipeline  # noqa: E402
from code_tree_exporter.verify import sample_edges, score_sample, verify_oracle, write_sample  # noqa: E402

PORTABLE = {"web", "db", "ops"}


def _node_available() -> bool:
    if not shutil.which(os.environ.get("CODE_TREE_NODE") or "node"):
        return False
    return Path(_typescript_path(REPOSITORY, ["web"]), "package.json").is_file()


@unittest.skipUnless(_node_available(), "Node.js and the angular-extractor TypeScript are required")
class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.corpus = Path(cls.temporary.name) / "corpus"
        generate(cls.corpus, "s", 1)
        config = json.loads((cls.corpus / "config.json").read_text(encoding="utf-8"))
        with_dotnet = bool(shutil.which(os.environ.get("CODE_TREE_DOTNET") or "dotnet"))
        cls.sources = None if with_dotnet else PORTABLE
        if not with_dotnet:
            config["sources"] = [source for source in config["sources"] if source["name"] in PORTABLE]
        config.update({"incremental": False, "lowPriority": False})
        path = cls.corpus / "config-test.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        cls.output = run_pipeline(path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_every_detectable_pattern_is_extracted(self) -> None:
        report = score_extract(self.corpus, self.output, only_sources=self.sources)
        self.assertGreater(report["total"], 50)
        self.assertEqual(report["regressions"], [], "\n".join(report["regressions"]))

    def test_blind_spots_are_flagged_not_silent(self) -> None:
        report = score_extract(self.corpus, self.output, only_sources=self.sources)
        self.assertEqual(report["silent_blind_spots"], [])
        self.assertGreater(report["by_pattern"]["plsql_dynamic_concat"]["flagged"], 0)

    def test_plsql_dependencies_match_what_oracle_would_record(self) -> None:
        report = verify_oracle(self.output / "graph.sqlite", self.corpus / "truth" / "all_dependencies.csv")
        self.assertEqual(report["recall"], 1.0, report["missed_by_object"])
        self.assertEqual(report["precision"], 1.0, report["graph_only"])

    def test_graph_only_impact_reaches_the_database_side(self) -> None:
        scenario = load_yaml(self.corpus / "scenarios" / "invoice-cancel-rule.yaml")
        result = score_impact(scenario, graph_impact(scenario, self.output), self.output)
        found = {miss.split(" (")[0] for miss in result["missed"]}
        for name in ("table INVOICE_HEADER", "table INVOICE_LINE", "table INVOICE_HISTORY",
                     "procedure PKG_INVOICE.CANCEL_INVOICE"):
            self.assertNotIn(name, found)

    def test_edge_sample_round_trip(self) -> None:
        rows = sample_edges(self.output / "graph.sqlite", 10, seed=3)
        self.assertEqual(len(rows), 10)
        self.assertGreater(len({row["edge_type"] for row in rows}), 3)  # spread over edge types
        for index, row in enumerate(rows):
            row["verdict"] = "wrong" if index == 0 else "correct"
        path = Path(self.temporary.name) / "sample.csv"
        write_sample(rows, path)
        score = score_sample(path)
        self.assertEqual((score["judged"], score["precision"]), (10, 0.9))
        self.assertLess(score["interval95"][0], 0.9)


if __name__ == "__main__":
    unittest.main()
