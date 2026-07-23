import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[2] / "extractors" / "plsql-extractor" / "main.py"
SPEC = importlib.util.spec_from_file_location("canonical_plsql_extractor", MODULE_PATH)
plsql = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plsql
SPEC.loader.exec_module(plsql)


class PlSqlSemanticTreeTest(unittest.TestCase):
    def test_tree_contains_source_ordered_v2_facts(self):
        builder = plsql.PackageBuilder("test", "source", "test")
        owner = "function:db:pkg:run:number"
        builder.add_node(owner, "FUNCTION", "RUN", "DB.PKG.RUN(NUMBER)")
        table = "table:db:orders"
        callee = "procedure:db:pkg:save:void"
        builder.add_node(table, "TABLE", "ORDERS", "DB.ORDERS")
        builder.add_node(callee, "PROCEDURE", "SAVE", "DB.PKG.SAVE()")
        builder.add_edge(owner, table, "READS", raw_operation="SELECT")
        builder.add_edge(owner, callee, "CALLS")
        builder.add_issue("DYNAMIC_SQL", "WARNING", "Runtime SQL target cannot be resolved", source_node_id=owner)
        source = "FUNCTION RUN(p_id IN NUMBER) RETURN NUMBER IS BEGIN SELECT * FROM ORDERS; SAVE(); RETURN p_id; END;"

        plsql._attach_semantic_tree(builder, owner, "FUNCTION", "RUN", "NUMBER", parameter_block="(p_id IN NUMBER)", text=source, source_path="run.sql")

        tree = json.loads(builder.nodes[owner]["properties_json"])["semantic_tree"]
        self.assertEqual(2, tree["version"])
        self.assertEqual({"name": "p_id", "type": "NUMBER", "direction": "IN"}, tree["parameters"][0])
        self.assertEqual(["data_effect", "call", "return"], [step["type"] for step in tree["steps"]])
        self.assertEqual(table, tree["steps"][0]["ref_node_id"])
        self.assertEqual(callee, tree["steps"][1]["ref_node_id"])
        self.assertEqual("run.sql", tree["steps"][0]["source"]["path"])
        self.assertEqual("Return p_id", tree["outputs"][0]["label"])
        self.assertEqual("DYNAMIC_SQL", tree["analysis_notes"][0]["code"])
        self.assertEqual([], tree["exceptions"])


    def test_write_edge_semantic_lists_target_fields_expressions_and_sources(self):
        builder = plsql.PackageBuilder("test", "source", "test")
        owner = "procedure:ORDER_DB:PKG:WRITE:void"
        table = "table:ORDER_DB:TARGET_TABLE"
        target_id = "column:ORDER_DB:TARGET_TABLE:ID"
        source_id = "column:ORDER_DB:SOURCE_TABLE:ID"
        builder.add_node(owner, "PROCEDURE", "WRITE", "ORDER_DB.PKG.WRITE()")
        builder.add_node(table, "TABLE", "TARGET_TABLE", "ORDER_DB.TARGET_TABLE")
        builder.add_edge(owner, table, "INSERTS", raw_operation="INSERT", properties={"existing": True})
        properties = {"operation": "INSERT_SELECT", "line": 12, "expression": "s.ID"}
        lineage = [
            SimpleNamespace(rel_type="WRITES_COLUMN", from_key_value="owner", to_key_value="target", properties=properties),
            SimpleNamespace(rel_type="DERIVES_FROM", from_key_value="source", to_key_value="target", properties=properties),
        ]

        plsql._attach_edge_semantics(
            builder,
            lineage,
            {"owner": owner},
            {"target": target_id, "source": source_id},
            {"target": ("TARGET_TABLE", "ID"), "source": ("SOURCE_TABLE", "ID")},
            "write.sql",
            "ORDER_DB",
        )

        edge = next(row for row in builder.edges.values() if row["edge_type"] == "INSERTS")
        edge_properties = json.loads(edge["properties_json"])
        self.assertTrue(edge_properties["existing"])
        semantic = edge_properties["semantic"]
        self.assertEqual("WRITE", semantic["action"])
        self.assertEqual("TARGET_TABLE", semantic["target"]["name"])
        self.assertEqual({"name": "ID", "target_node_id": target_id, "expression": "s.ID", "sources": [source_id]}, semantic["fields"][0])
        self.assertEqual({"path": "write.sql", "line": 12}, semantic["statements"][0]["source"])

if __name__ == "__main__":
    unittest.main()
