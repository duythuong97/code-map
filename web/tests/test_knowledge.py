"""Knowledge layer: rules, decisions and questions people own, and how a code change sends them back to review."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.rag_export import write_rag_database  # noqa: E402
from shared.fixtures.engine_graph import BIG, business_inputs, engine_graph  # noqa: E402
from web.backend.codemap_mcp.server import McpServer  # noqa: E402
from web.backend.codemap_mcp.workspace import Workspace  # noqa: E402

API = "/api/graph/business"
CANCEL = "method:a:Orders.CancelOrder"
TABLE = "table:DB1:ORDER_HEADER"
AN = {"X-Code-Map-User": "an"}


class KnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.output = base / "output"
        self.output.mkdir()
        self.inputs = base / "input-data"
        engine_graph(self.output / "graph.sqlite")
        business_inputs(self.inputs)
        (self.inputs / "owners.csv").write_text(
            "system,team,owner,contact,notes\nrepo,Team Order,Binh,#order,\nDB1,Team DB,Chi,#db,\n", encoding="utf-8"
        )
        (self.inputs / "ops-facts.csv").write_text(
            "target,kind,value,note\nORDER_HEADER,volume,~200M rows,partitioned\nNOPE,sla,x,\n", encoding="utf-8"
        )
        self.extract()
        import web.backend.api.app as serving

        self.serving = serving
        self.previous = (serving.GRAPH_OUTPUT, serving.KNOWLEDGE_DB)
        serving.GRAPH_OUTPUT, serving.KNOWLEDGE_DB = self.output, base / "knowledge.sqlite"
        self.client = serving.app.test_client()
        self.mcp = McpServer(Workspace(output=self.output, knowledge=base / "knowledge.sqlite"))

    def tearDown(self) -> None:
        self.serving.GRAPH_OUTPUT, self.serving.KNOWLEDGE_DB = self.previous
        self.temporary.cleanup()

    def extract(self) -> None:
        write_rag_database(self.output / "graph.sqlite", self.output / "codemap-rag.sqlite", input_root=self.inputs)

    def change_cancel_order(self) -> None:
        """The next extract: CancelOrder now deletes instead of updating."""
        with closing(sqlite3.connect(self.output / "graph.sqlite")) as graph, graph:
            graph.execute("UPDATE edges SET raw_operation='DELETE' WHERE edge_id=?", (BIG + 11,))
        self.extract()

    def send(self, method: str, path: str, body: dict | None = None, headers: dict | None = None, status: int = 200):
        response = self.client.open(API + path, method=method, json=body, headers=headers or {})
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def tool(self, name: str, arguments: dict) -> str:
        reply = self.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return reply["result"]["content"][0]["text"]

    def test_rule_lifecycle_and_revalidation_after_a_code_change(self) -> None:
        draft = self.send("POST", "/rules", {
            "domain": "ord", "title": "Đơn đã giao không được hủy", "text": "CancelOrder chỉ đổi STATUS khi chưa giao.",
            "evidence": "a/Orders.cs:42", "nodes": ["Orders.CancelOrder", TABLE],
        }, AN, 201)
        self.assertEqual(draft["rule_id"], "RULE-ORD-001")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["node_ids"], [CANCEL, TABLE])
        queue = self.send("GET", "/review-queue")
        self.assertEqual(queue["counts"]["rules_draft"], 1)

        # Approving records who approved; without a name it is refused.
        self.send("PATCH", "/rules/RULE-ORD-001", {"status": "approved"}, status=400)
        approved = self.send("PATCH", "/rules/RULE-ORD-001", {"status": "approved"}, AN)
        self.assertEqual((approved["status"], approved["approved_by"]), ("approved", "an"))
        self.assertTrue(approved["fingerprint"])
        text = self.tool("codemap_current_behavior", {"topic": "CancelOrder"})
        self.assertIn("[R1] RULE-ORD-001 (approved by an) Đơn đã giao không được hủy", text)

        # The same extract again changes nothing.
        self.extract()
        self.assertEqual(self.send("GET", "/rules/RULE-ORD-001")["status"], "approved")

        self.change_cancel_order()
        rule = self.send("GET", "/rules/RULE-ORD-001")
        self.assertEqual(rule["status"], "needs_review")
        self.assertIn("METHOD CancelOrder", rule["review_reason"])
        self.assertEqual(self.send("GET", "/review-queue")["counts"]["rules_needs_review"], 1)
        text = self.tool("codemap_current_behavior", {"topic": "CancelOrder"})
        self.assertIn("NEEDS REVIEW: Code changed since approval", text)
        self.assertIn("Approved rules whose code changed since approval (do not rely on them): RULE-ORD-001", text)

        # Re-approving after review stores the new fingerprint.
        self.assertEqual(self.send("PATCH", "/rules/RULE-ORD-001", {"status": "approved"}, AN)["status"], "approved")
        self.send("POST", "/revalidate")
        self.assertEqual(self.send("GET", "/rules/RULE-ORD-001")["status"], "approved")

    def test_editing_an_approved_rule_needs_a_new_approval(self) -> None:
        self.send("POST", "/rules", {"title": "x", "nodes": [TABLE], "status": "approved"}, AN, 201)
        edited = self.send("PATCH", "/rules/RULE-GEN-001", {"text": "new meaning"}, AN)
        self.assertEqual(edited["status"], "draft")

    def test_unknown_nodes_are_refused(self) -> None:
        response = self.send("POST", "/rules", {"title": "x", "nodes": ["NO_SUCH_TABLE"]}, AN, 400)
        self.assertEqual(response["error"]["code"], "unknown_nodes")

    def test_decisions_and_questions(self) -> None:
        decision = self.send("POST", "/decisions", {
            "title": "Hủy đơn đã phân bổ", "context": "Batch đang hủy không hoàn tiền",
            "options": [{"title": "Không cho hủy"}, {"title": "Hủy và hoàn tiền", "consequence": "đổi PAYMENT"}],
            "nodes": [CANCEL],
        }, AN, 201)
        self.assertEqual((decision["decision_id"], decision["status"]), ("DEC-0001", "proposed"))
        self.send("PATCH", "/decisions/DEC-0001", {"status": "accepted"}, AN, 400)  # no decision text yet
        accepted = self.send("PATCH", "/decisions/DEC-0001", {"status": "accepted", "decision": "Hủy và hoàn tiền"}, AN)
        self.assertEqual(accepted["decided_by"], "an")

        question = self.send("POST", "/questions", {"text": "Hoàn tiền trong bao lâu?", "nodes": [CANCEL],
                                                    "options": ["Ngay", "Batch đêm"]}, AN, 201)
        self.assertEqual(question["question_id"], "Q-0001")
        self.assertIn("Q-0001", self.tool("codemap_search", {"question": "CancelOrder"}))
        self.send("POST", "/questions/Q-0001/answer", {"answer": "Ngay"}, status=400)
        answered = self.send("POST", "/questions/Q-0001/answer", {"answer": "Ngay"}, AN)
        self.assertEqual((answered["status"], answered["answered_by"]), ("answered", "an"))
        text = self.tool("codemap_current_behavior", {"topic": "CancelOrder"})
        self.assertIn("DEC-0001 (accepted by an) Hủy đơn đã phân bổ: Hủy và hoàn tiền", text)
        self.assertIn("Q-0001 answered by an: Hoàn tiền trong bao lâu? → Ngay", text)

    def test_owners_and_ops_facts_reach_impact_answers(self) -> None:
        owners = self.send("GET", "/owners")
        self.assertEqual([owner["system"] for owner in owners], ["DB1", "repo"])
        facts = self.send("GET", "/ops-facts")
        self.assertEqual([(fact["target"], fact["node_id"]) for fact in facts], [("NOPE", None), ("ORDER_HEADER", TABLE)])
        text = self.tool("codemap_impact", {"target": "ORDER_HEADER"})
        self.assertIn("- DB1 (owner: Team DB, Chi, #db)", text)
        self.assertIn("ORDER_HEADER volume: ~200M rows (partitioned)", text)

    def test_approved_flow_part_goes_back_to_review_when_its_code_changes(self) -> None:
        tree = self.send("GET", "/tree")
        flow = next(item for menu in tree["menus"] for feature in menu["children"] for item in feature.get("children", [])
                    if item["kind"] == "flow" and "ALLOCATE" in item["id"])
        doc = self.send("GET", f"/flows/{flow['id']}")["flow"]
        step = next(step for step in doc["steps"] if "command-mode:order-fulfillment:allocate" in step["nodes"])
        self.send("PATCH", f"/flows/{flow['id']}/parts/step:{step['id']}", {"text": "Phân bổ đơn", "status": "approved"}, AN)
        self.assertEqual(self.send("GET", "/review-queue")["counts"]["flow_parts_changed"], 0)
        with closing(sqlite3.connect(self.output / "graph.sqlite")) as graph, graph:
            graph.execute(
                "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?)",
                (BIG + 13, "e4", BIG + 6, BIG + 2, "CALLS", "TECHNICAL", "", 1.0, "{}", "sources/a"),
            )
        self.extract()
        queue = self.send("GET", "/review-queue")
        self.assertEqual([(item["flow_id"], item["part"]) for item in queue["flow_parts_changed"]],
                         [(flow["id"], f"step:{step['id']}")])

    def test_kb_status(self) -> None:
        self.tool("codemap_search", {"question": "zzqqxx"})
        status = self.send("GET", "/kb-status")
        self.assertIn("KB built", status["summary"])
        self.assertEqual(status["usage"]["unanswered"][0]["tool"], "codemap_search")
        self.assertIn("rules_draft", status["review"])


if __name__ == "__main__":
    unittest.main()
