"""Business layer: menu/feature tree from the graph, flow documents and the AI export/import round trip."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.rag_export import write_rag_database  # noqa: E402
from shared.fixtures.engine_graph import BIG, ENGINE_SCHEMA, business_inputs  # noqa: E402

API = "/api/graph/business"
SCREEN = "screen:web:orders/new"
ACTION = "ui-action:web:create-order:submit-order"
CREATE_API = "api-operation:a:POST:/api/orders"
HANDLER = "method:a:OrderController.Create"
CREATE_PROC = "procedure:DB1:PKG_ORDER.CREATE_ORDER"
VALIDATE = "procedure:DB1:PKG_ORDER.VALIDATE_LINES"
TABLE = "table:DB1:ORDER_HEADER"
PING = "api-operation:a:GET:/api/ping"
SHIPMENT_SCREEN = "screen:web:shipments/:id"
FEATURE = f"feature:{SCREEN}"
FLOW = f"flow:{ACTION}"

NODES = [
    (1, SCREEN, "SCREEN", "CreateOrderComponent", "web.orders/new", "orders new", {"route": "orders/new"}),
    (2, ACTION, "UI_ACTION", "submitOrder", "web.CreateOrderComponent.submitOrder", "Submit order", {}),
    (3, CREATE_API, "API_OPERATION", "POST /api/orders", "a/OrderFlow.cs.POST", "POST /api/orders", {}),
    (4, HANDLER, "METHOD", "Create", "a/OrderFlow.cs.OrderController.Create", "Create", {}),
    (5, CREATE_PROC, "PROCEDURE", "PKG_ORDER.CREATE_ORDER", "DB1.PKG_ORDER.CREATE_ORDER", "CREATE_ORDER", {}),
    (6, VALIDATE, "PROCEDURE", "PKG_ORDER.VALIDATE_LINES", "DB1.PKG_ORDER.VALIDATE_LINES", "VALIDATE_LINES",
     {"semantic_tree": {"version": 3, "steps": [{"type": "branch", "condition": "v_qty > 0", "line": 64}]}}),
    (7, TABLE, "TABLE", "ORDER_HEADER", "DB1.APP.ORDER_HEADER", "ORDER_HEADER", {}),
    (8, PING, "API_OPERATION", "GET /api/ping", "a/OrderFlow.cs.GET", "GET /api/ping", {}),
    (9, SHIPMENT_SCREEN, "SCREEN", "ShipmentPageComponent", "web.shipments/:id", "shipments id", {"route": "shipments/:id"}),
    (10, "executable:batch:order-fulfillment.exe", "EXECUTABLE", "order-fulfillment.exe", "order-fulfillment.exe", "order-fulfillment.exe", {}),
]
EDGES = [
    (1, 2, "CONTAINS", "STRUCTURAL", ""), (2, 3, "CALLS", "TECHNICAL", ""), (3, 4, "HANDLES_API", "TECHNICAL", ""),
    (4, 5, "CALLS", "TECHNICAL", ""), (5, 6, "CALLS", "TECHNICAL", ""), (5, 7, "WRITES_TO", "TECHNICAL", "INSERT"),
    (10, 5, "CALLS", "TECHNICAL", ""),
]


def business_graph(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ENGINE_SCHEMA)
        db.execute("INSERT INTO metadata VALUES('source_name', ?)", (json.dumps("demo"),))
        for number, stable, node_type, name, qualified, display, properties in NODES:
            db.execute(
                "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (BIG + number, stable, node_type, name, qualified, display, "sys", "DB1", "repo", "MAIN", 1.0,
                 json.dumps(properties), "sources/a"),
            )
        for position, (source, target, edge_type, layer, operation) in enumerate(EDGES):
            db.execute(
                "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?)",
                (BIG + 100 + position, f"e{position}", BIG + source, BIG + target, edge_type, layer, operation, 1.0, "{}", "sources/a"),
            )
        db.executemany("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (1, "ev1", "NODE", BIG + 3, "order-api/OrderFlow.cs", 26, 30, 1, 1, "DECLARATION", "dotnet", 1.0,
             "[HttpPost] public IActionResult Create(...)", "{}", "a"),
            (2, "ev2", "NODE", BIG + 6, "order-db/order_flow.sql", 62, 65, 1, 1, "DECLARATION", "plsql", 1.0,
             "PROCEDURE VALIDATE_LINES IS BEGIN NULL; END;", "{}", "a"),
        ])
        db.execute(
            "INSERT INTO comments VALUES(1,'c1','order-api/OrderFlow.cs',?,'LINE',25,25,1,1,'// x',"
            "'Idempotency: existing request key returns prior order.','en','utf-8','{}','a')", (BIG + 3,),
        )


class BusinessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.output = base / "output"
        self.output.mkdir()
        inputs = base / "input-data"
        business_graph(self.output / "graph.sqlite")
        business_inputs(inputs)
        write_rag_database(self.output / "graph.sqlite", self.output / "codemap-rag.sqlite", input_root=inputs)
        import web.backend.api.app as serving

        self.serving = serving
        self.previous = (serving.GRAPH_OUTPUT, serving.KNOWLEDGE_DB)
        serving.GRAPH_OUTPUT = self.output
        serving.KNOWLEDGE_DB = base / "knowledge.sqlite"
        self.client = serving.app.test_client()

    def tearDown(self) -> None:
        self.serving.GRAPH_OUTPUT, self.serving.KNOWLEDGE_DB = self.previous
        self.temporary.cleanup()

    def call(self, method: str, path: str, status: int = 200, **kwargs):
        response = getattr(self.client, method)(API + path, **kwargs)
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def tree_item(self, tree, item_id):
        stack = list(tree["menus"])
        while stack:
            item = stack.pop()
            if item["id"] == item_id:
                return item
            stack.extend(item.get("children", []))
        return None

    def export(self, **flags):
        query = "&".join(f"{key}={value}" for key, value in flags.items())
        return self.call("post", f"/features/{FEATURE}/ai-export" + (f"?{query}" if query else ""))

    def fill(self, pack_text: str, *, step_text: str, s1_text: str | None = None) -> str:
        pack = yaml.safe_load(pack_text)
        flow = next(flow for flow in pack["flows"] if flow["id"] == FLOW)
        flow["name"] = "Tạo đơn hàng"
        flow["summary"]["text"] = "Nhân viên lập đơn cho khách."
        for step in flow["steps"]:
            if step.get("locked"):
                continue
            step["name"] = f"Bước {step['id']}"
            step["text"] = s1_text if step["id"] == "s1" and s1_text else step_text
            step["rules"] = [{"title": "Dòng đơn phải hợp lệ", "at": "PKG_ORDER.VALIDATE_LINES",
                              "evidence": "order-db/order_flow.sql:62", "status": "bad", "text": "Thân procedure chỉ có NULL."}]
        flow["questions"] = ["Quy tắc kiểm tra dòng đơn gồm những gì?"]
        return yaml.safe_dump(pack, allow_unicode=True, sort_keys=False)

    def test_tree_groups_screens_by_route_and_lists_unassigned_apis(self) -> None:
        tree = self.call("get", "/tree")
        names = [menu["id"] for menu in tree["menus"]]
        self.assertEqual(names, ["menu:orders", "menu:shipments", "menu:batch", "menu:unsorted"])
        feature = self.tree_item(tree, FEATURE)
        self.assertEqual(feature["route"], "orders/new")
        self.assertEqual([(flow["id"], flow["name"], flow["has_doc"]) for flow in feature["children"]], [(FLOW, "Submit order", False)])
        jobnet = self.tree_item(tree, "menu:batch")["children"][0]
        self.assertEqual(jobnet["entry_type"], "JOB_NETWORK")
        self.assertEqual(len(jobnet["children"]), 2)  # one proposed flow per job
        unsorted = self.tree_item(tree, "menu:unsorted")
        self.assertEqual([item["node_id"] for item in unsorted["children"]], [PING])

    def test_flow_skeleton_has_one_step_per_system(self) -> None:
        flow = self.call("get", f"/flows/{FLOW}")
        self.assertFalse(flow["has_doc"])
        steps = flow["flow"]["steps"]
        self.assertEqual([step["system"] for step in steps], ["repo", "DB1"])
        self.assertEqual(steps[0]["nodes"], [ACTION, CREATE_API, HANDLER])
        self.assertEqual(steps[1]["nodes"][0], CREATE_PROC)
        self.assertEqual(set(steps[1]["nodes"]), {CREATE_PROC, VALIDATE, TABLE})
        names = {node["node_id"]: node["name"] for node in flow["nodes"]}
        systems = {node["node_id"]: node["system"] for node in flow["nodes"]}
        self.assertEqual((systems[ACTION], systems[HANDLER], systems[CREATE_PROC], systems[TABLE]), ("repo", "repo", "DB1", "DB1"))
        self.assertEqual(names[HANDLER], "OrderController.Create")
        self.assertEqual(names[ACTION], "CreateOrderComponent.submitOrder")
        self.assertIn({"source": CREATE_PROC, "target": TABLE, "edge_type": "INSERTS"},
                      [{key: edge[key] for key in ("source", "target", "edge_type")} for edge in flow["edges"]])
        self.assertEqual(flow["path"][0]["name"], "Orders")

    def test_export_pack_carries_graph_context_and_an_empty_template(self) -> None:
        result = self.export()
        self.assertTrue(result["filename"].endswith(".codemap-ai.yaml"))
        pack = yaml.safe_load(result["text"])
        refs = [node["ref"] for node in pack["context"]["nodes"]]
        self.assertIn("api: POST /api/orders", refs)
        self.assertIn("method: OrderController.Create", refs)
        self.assertIn(["PKG_ORDER.CREATE_ORDER", "INSERTS", "ORDER_HEADER"], pack["context"]["edges"])
        self.assertEqual(pack["context"]["evidence"]["PKG_ORDER.VALIDATE_LINES"]["at"], "order-db/order_flow.sql:62")
        validate = next(node for node in pack["context"]["nodes"] if node["ref"] == "procedure: PKG_ORDER.VALIDATE_LINES")
        self.assertEqual(validate["conditions"], [{"kind": "branch", "text": "v_qty > 0", "line": 64}])
        api = next(node for node in pack["context"]["nodes"] if node["ref"] == "api: POST /api/orders")
        self.assertEqual(api["comments"], ["Idempotency: existing request key returns prior order."])
        flow = pack["flows"][0]
        self.assertEqual(flow["id"], FLOW)
        self.assertEqual([step["text"] for step in flow["steps"]], ["", ""])
        self.assertEqual(result["stats"]["locked"], 0)
        self.assertIn("Quy tắc bắt buộc", pack["instructions"])
        bare = yaml.safe_load(self.export(code=0, instructions=0)["text"])
        self.assertNotIn("evidence", bare["context"])
        self.assertNotIn("instructions", bare)

    def test_import_checks_references_and_saves_the_document(self) -> None:
        text = self.fill(self.export()["text"], step_text="[[OrderController.Create]] gọi [[PKG_ORDER.CREATE_ORDER]]. Đọc [[ORDER_DISCOUNT]].")
        check = self.call("post", f"/features/{FEATURE}/ai-import/check", json={"text": text})
        self.assertTrue(check["ok"])
        errors = [line["message"] for line in check["lines"] if line["level"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("ORDER_DISCOUNT", errors[0])
        applied = self.call("post", f"/features/{FEATURE}/ai-import", json={"text": text})
        self.assertEqual(applied["applied"]["flows"], [FLOW])
        flow = self.call("get", f"/flows/{FLOW}")
        self.assertTrue(flow["has_doc"])
        self.assertEqual(flow["flow"]["name"], "Tạo đơn hàng")
        step = flow["flow"]["steps"][1]
        self.assertEqual((step["status"], step["rules"][0]["status"], step["rules"][0]["at_node_id"]), ("ai", "bad", VALIDATE))
        self.assertEqual(flow["flow"]["questions"][0]["text"], "Quy tắc kiểm tra dòng đơn gồm những gì?")
        tree_flow = self.tree_item(applied["tree"], FLOW)
        self.assertEqual((tree_flow["has_doc"], tree_flow["questions"]), (True, 1))

    def test_reviewed_parts_are_locked_in_the_next_export_and_survive_import(self) -> None:
        first = self.fill(self.export()["text"], step_text="AI viết lần một.")
        self.call("post", f"/features/{FEATURE}/ai-import", json={"text": first})
        edited = self.call("patch", f"/flows/{FLOW}/parts/step:s1", json={"text": "Người dùng sửa."},
                           headers={"X-Code-Map-User": "lan"})
        self.assertEqual((edited["flow"]["steps"][0]["status"], edited["flow"]["steps"][0]["reviewed_by"]), ("edited", "lan"))
        approved = self.call("patch", f"/flows/{FLOW}/parts/summary", json={"status": "approved"})
        self.assertEqual(approved["flow"]["summary"]["status"], "approved")
        second = self.export()
        self.assertEqual(second["stats"]["locked"], 2)
        pack = yaml.safe_load(second["text"])
        s1 = pack["flows"][0]["steps"][0]
        self.assertEqual((s1["locked"], s1["text"]), (True, "Người dùng sửa."))
        s1["locked"] = False  # an AI that ignores the lock
        s1["text"] = "AI ghi đè."
        pack["flows"][0]["steps"][1]["text"] = "AI viết lần hai."
        self.call("post", f"/features/{FEATURE}/ai-import", json={"text": yaml.safe_dump(pack, allow_unicode=True)})
        flow = self.call("get", f"/flows/{FLOW}")["flow"]
        self.assertEqual(flow["steps"][0]["text"], "Người dùng sửa.")
        self.assertEqual(flow["steps"][1]["text"], "AI viết lần hai.")
        self.assertEqual(flow["summary"]["status"], "approved")

    def test_import_rejects_a_pack_from_another_export(self) -> None:
        text = self.fill(self.export()["text"], step_text="x")
        pack = yaml.safe_load(text)
        pack["export"]["id"] = "exp-9999"
        check = self.call("post", f"/features/{FEATURE}/ai-import/check", json={"text": yaml.safe_dump(pack)})
        self.assertFalse(check["ok"])
        self.call("post", f"/features/{FEATURE}/ai-import", json={"text": yaml.safe_dump(pack)}, status=400)
        broken = self.call("post", f"/features/{FEATURE}/ai-import/check", json={"text": "flows: ["}, status=400)
        self.assertEqual(broken["error"]["code"], "invalid_import")

    def test_answers_renames_and_assigning_an_entry_point(self) -> None:
        self.call("post", f"/features/{FEATURE}/ai-import", json={"text": self.fill(self.export()["text"], step_text="x")})
        answered = self.call("post", f"/flows/{FLOW}/questions/q1/answer", json={"answer": "Số lượng > 0."})
        self.assertEqual(answered["flow"]["questions"][0]["answer"], "Số lượng > 0.")
        self.assertIn({"q": "Quy tắc kiểm tra dòng đơn gồm những gì?", "a": "Số lượng > 0."},
                      yaml.safe_load(self.export()["text"])["context"]["answered"])
        renamed = self.call("patch", "/items/menu:orders", json={"name": "Đơn hàng", "description": "Màn hình bán hàng"})
        self.assertEqual((self.tree_item(renamed["tree"], "menu:orders")["name"]), "Đơn hàng")
        assigned = self.call("patch", f"/items/entry:{PING}", json={"parent_id": FEATURE})
        self.assertEqual(self.tree_item(assigned["tree"], "menu:unsorted")["children"], [])
        self.assertIn(f"flow:{PING}", [flow["id"] for flow in self.tree_item(assigned["tree"], FEATURE)["children"]])
        self.call("patch", "/items/menu:orders", json={"status": "rejected"}, status=400)

    def test_feature_detail_and_search(self) -> None:
        detail = self.call("get", f"/items/{FEATURE}")
        self.assertEqual(detail["apis"], ["POST /api/orders"])
        self.assertEqual(detail["writes"], ["ORDER_HEADER"])
        self.assertIsNone(detail["last_export"])
        self.export()
        self.assertTrue(self.call("get", f"/items/{FEATURE}")["last_export"]["export_id"].startswith("exp-"))
        self.call("post", f"/features/{FEATURE}/ai-import", json={"text": self.fill(self.export()["text"], step_text="Ghi ORDER_HEADER")})
        hits = self.call("get", "/search?q=ghi%20order_header")["business"]
        self.assertIn(("step", FLOW), [(hit["kind"], hit["id"]) for hit in hits])
        self.assertIn(("feature", FEATURE), [(hit["kind"], hit["id"]) for hit in self.call("get", "/search?q=orders/new")["business"]])
        node = next(hit for hit in self.call("get", "/search?q=VALIDATE_LINES")["nodes"] if hit["id"] == VALIDATE)
        self.assertEqual((node["feature_id"], node["flow_id"], node["label"]), (FEATURE, FLOW, "PKG_ORDER.VALIDATE_LINES"))

    def test_people_edits_survive_the_next_extract(self) -> None:
        self.call("patch", f"/items/{FEATURE}", json={"name": "Tạo đơn"})
        write_rag_database(self.output / "graph.sqlite", self.output / "codemap-rag.sqlite", input_root=None)
        self.assertEqual(self.tree_item(self.call("get", "/tree"), FEATURE)["name"], "Tạo đơn")


if __name__ == "__main__":
    unittest.main()
