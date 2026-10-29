"""MCP server: the tools agents call, over JSON-RPC (stdio and HTTP)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from code_tree_exporter.rag_export import write_rag_database  # noqa: E402
from shared.fixtures.engine_graph import business_inputs, engine_graph  # noqa: E402
from web.backend.codemap_mcp.server import CallContext, McpServer  # noqa: E402
from web.backend.codemap_mcp.workspace import Workspace  # noqa: E402

CANCEL = "method:a:Orders.CancelOrder"
TABLE = "table:DB1:ORDER_HEADER"


def rpc(method: str, params: dict | None = None, request_id: int | None = 1) -> dict:
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        message["id"] = request_id
    return message


class McpFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.output = base / "output"
        self.output.mkdir()
        inputs = base / "input-data"
        engine_graph(self.output / "graph.sqlite")
        business_inputs(inputs)
        write_rag_database(self.output / "graph.sqlite", self.output / "codemap-rag.sqlite", input_root=inputs)
        self.knowledge = base / "knowledge.sqlite"
        self.workspace = Workspace(output=self.output, knowledge=self.knowledge)
        self.server = McpServer(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, tool: str, arguments: dict, context: CallContext | None = None) -> dict:
        reply = self.server.handle(rpc("tools/call", {"name": tool, "arguments": arguments}), context)
        self.assertIn("result", reply, reply)
        return reply["result"]

    def text(self, tool: str, arguments: dict) -> str:
        result = self.call(tool, arguments)
        self.assertFalse(result["isError"], result)
        return result["content"][0]["text"]


class McpTests(McpFixture):
    def test_initialize_and_list_read_only_tools(self) -> None:
        init = self.server.handle(rpc("initialize", {"protocolVersion": "2025-03-26", "clientInfo": {"name": "t"}}))
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("instructions", init["result"])
        self.assertIsNone(self.server.handle(rpc("notifications/initialized", request_id=None)))
        tools = self.server.handle(rpc("tools/list"))["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertIn("codemap_impact", names)
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in tools))
        self.assertTrue(all(not name.startswith("codemap_propose") for name in names))
        unknown = self.server.handle(rpc("nope"))
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_search_cites_business_terms_and_ends_with_uncertainty(self) -> None:
        text = self.text("codemap_search", {"question": "hủy đơn hàng ghi bảng nào?"})
        self.assertTrue(text.startswith("> KB built"))
        self.assertIn("[C1] Hủy đơn hàng", text)
        self.assertIn("ORDER_HEADER", text)
        self.assertIn("## Not certain", text)
        self.assertIn("DYNAMIC_SQL", text)

    def test_impact_of_a_table_lists_the_writer_grouped_by_system(self) -> None:
        text = self.text("codemap_impact", {"target": "ORDER_HEADER"})
        self.assertIn("-WRITES_TO (UPDATE)->", text)
        self.assertIn("## Impact by system", text)
        self.assertIn("CancelOrder", text)
        self.assertIn("DELETE /api/orders/{id}", text)
        # The table's columns are structure, not dependants.
        self.assertNotIn("COLUMN STATUS", text)

    def test_impact_of_changed_files(self) -> None:
        text = self.text("codemap_impact", {"files": ["a/Orders.cs"], "detail": "brief"})
        self.assertIn("CancelOrder", text)
        self.assertIn("ORDER_HEADER", text)

    def test_bad_arguments_are_tool_errors(self) -> None:
        result = self.call("codemap_impact", {})
        self.assertTrue(result["isError"])
        self.assertIn("target", result["content"][0]["text"])
        result = self.call("codemap_search", {"question": "x", "detail": "huge"})
        self.assertTrue(result["isError"])

    def test_current_behavior_shows_documents_with_review_status(self) -> None:
        doc = {
            "id": f"flow:{CANCEL}", "name": "Hủy đơn hàng",
            "summary": {"text": "Khách hủy đơn chưa giao.", "status": "approved", "reviewed_by": "an"},
            "steps": [{"id": "s1", "system": "repo", "name": "Ghi trạng thái hủy", "text": "Cập nhật ORDER_HEADER.",
                       "status": "ai", "nodes": [CANCEL, TABLE],
                       "rules": [{"title": "Đơn đã giao không hủy được", "text": "", "at": "CancelOrder", "evidence": "", "status": "warn"}]}],
            "edge_cases": {"items": [], "status": "ai"},
            "questions": [{"id": "q1", "text": "Hoàn tiền tự động?", "answer": "Có", "answered_by": "binh"}],
        }
        with self.workspace.knowledge_db() as db:
            db.execute("INSERT INTO kn.biz_flows(flow_id,feature_id,doc_json) VALUES(?,?,?)",
                       (f"flow:{CANCEL}", "feature:x", json.dumps(doc, ensure_ascii=False)))
            db.commit()
        text = self.text("codemap_current_behavior", {"topic": "CancelOrder"})
        self.assertIn('[D1] Flow "Hủy đơn hàng"', text)
        self.assertIn("[approved by an]", text)
        self.assertIn("AI draft, NOT reviewed", text)
        self.assertIn("NO EVIDENCE", text)
        self.assertIn("answered: Hoàn tiền tự động? → Có (by binh)", text)

    def test_check_terms_flags_unknown_names_and_vague_words(self) -> None:
        text = self.text(
            "codemap_check_terms",
            {"text": "Khi hủy đơn hàng, cập nhật `ORDER_HEADER` và gọi OrderService.RefundAll rồi xử lý dữ liệu."},
        )
        self.assertIn("Hủy đơn hàng", text)
        self.assertIn("`ORDER_HEADER` exists in the graph as TABLE", text)
        self.assertIn("`OrderService.RefundAll` NOT found", text)
        self.assertIn('"xử lý"', text)

    def test_file_context_and_status(self) -> None:
        self.assertIn("CancelOrder", self.text("codemap_file_context", {"path": "a/Orders.cs"}))
        self.assertIn("declares nothing", self.text("codemap_file_context", {"path": "nope.cs"}))
        status = self.text("codemap_status", {})
        self.assertIn("## Knowledge base", status)
        self.assertIn("DYNAMIC_SQL", status)

    def test_calls_are_logged_with_empty_results(self) -> None:
        self.text("codemap_search", {"question": "zzqqxx"})
        with closing(sqlite3.connect(self.knowledge)) as db:
            rows = db.execute("SELECT tool, empty, arguments_json FROM mcp_calls").fetchall()
        self.assertEqual(rows, [("codemap_search", 1, '{"question": "zzqqxx"}')])

    def test_prompts(self) -> None:
        prompts = self.server.handle(rpc("prompts/list"))["result"]["prompts"]
        self.assertIn("impact", [prompt["name"] for prompt in prompts])
        got = self.server.handle(rpc("prompts/get", {"name": "as-is", "arguments": {"topic": "hủy đơn"}}))
        self.assertIn("codemap_current_behavior", got["result"]["messages"][0]["content"]["text"])
        missing = self.server.handle(rpc("prompts/get", {"name": "as-is", "arguments": {}}))
        self.assertEqual(missing["error"]["code"], -32602)

    def test_missing_extract_is_a_tool_error(self) -> None:
        server = McpServer(Workspace(output=self.output / "missing", knowledge=None))
        reply = server.handle(rpc("tools/call", {"name": "codemap_search", "arguments": {"question": "x"}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("no extract", reply["result"]["content"][0]["text"])

    def test_stdio_transport(self) -> None:
        lines = "\n".join(json.dumps(message) for message in [
            rpc("initialize", {"protocolVersion": "2025-06-18"}, 1),
            rpc("notifications/initialized", request_id=None),
            rpc("tools/call", {"name": "codemap_search", "arguments": {"question": "ORDER_HEADER"}}, 2),
        ]) + "\n"
        completed = subprocess.run(
            [sys.executable, "-m", "web.backend.codemap_mcp", "--graph-output", str(self.output),
             "--knowledge-db", str(self.knowledge)],
            input=lines.encode("utf-8"), capture_output=True, cwd=PROJECT_ROOT, timeout=120, check=True,
        )
        replies = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertIn("ORDER_HEADER", replies[1]["result"]["content"][0]["text"])


class McpProposalTests(McpFixture):
    """Agents propose; nothing they write is approved."""

    def writer(self) -> CallContext:
        return CallContext(user="copilot:an", allow_write=True)

    def write(self, tool: str, arguments: dict) -> dict:
        reply = self.server.handle(rpc("tools/call", {"name": tool, "arguments": arguments}), self.writer())
        return reply["result"]

    def test_write_tools_are_hidden_without_permission(self) -> None:
        hidden = [tool["name"] for tool in self.server.handle(rpc("tools/list"))["result"]["tools"]]
        self.assertNotIn("codemap_propose_rule", hidden)
        self.assertIn("codemap_get_flow_doc", hidden)
        shown = [tool["name"] for tool in self.server.handle(rpc("tools/list"), self.writer())["result"]["tools"]]
        self.assertIn("codemap_propose_rule", shown)
        refused = self.server.handle(rpc("tools/call", {"name": "codemap_propose_rule", "arguments": {}}))
        self.assertEqual(refused["error"]["code"], -32602)

    def test_proposed_rule_question_and_decision_are_drafts(self) -> None:
        result = self.write("codemap_propose_rule", {"title": "Đơn đã hủy không cập nhật lại", "evidence": "a/Orders.cs:42",
                                                     "nodes": ["CancelOrder", "ORDER_HEADER"], "domain": "ORD"})
        self.assertFalse(result["isError"], result)
        self.assertIn("RULE-ORD-001 (draft)", result["content"][0]["text"])
        missing = self.write("codemap_propose_rule", {"title": "x", "nodes": ["ORDER_HEADER"]})
        self.assertTrue(missing["isError"])
        unknown = self.write("codemap_propose_rule", {"title": "x", "evidence": "a:1", "nodes": ["NOPE_TABLE"]})
        self.assertTrue(unknown["isError"])
        self.assertIn("NOPE_TABLE", unknown["content"][0]["text"])
        question = self.write("codemap_propose_question", {"text": "Hủy có hoàn tiền?", "nodes": ["CancelOrder"],
                                                           "options": [{"title": "Có"}, {"title": "Không"}]})
        self.assertIn("Q-0001 (open)", question["content"][0]["text"])
        decision = self.write("codemap_propose_decision", {"title": "Cách hủy", "context": "c", "options": ["A", "B"]})
        self.assertIn("DEC-0001 (proposed)", decision["content"][0]["text"])
        with closing(sqlite3.connect(self.knowledge)) as db:
            self.assertEqual(db.execute("SELECT status, source, proposed_by FROM biz_rules").fetchall(),
                             [("draft", "ai", "copilot:an")])
        # The proposal is visible to other agents as a draft, never as a confirmed rule.
        text = self.text("codemap_current_behavior", {"topic": "CancelOrder"})
        self.assertIn("RULE-ORD-001 (draft, NOT approved)", text)
        self.assertIn("Draft rules not approved by anyone yet: RULE-ORD-001", text)

    def test_flow_document_round_trip(self) -> None:
        got = self.server.handle(rpc("tools/call", {"name": "codemap_get_flow_doc", "arguments": {"flow": "Allocate orders"}}))
        text = got["result"]["content"][0]["text"]
        self.assertIn("flow:job:DAILY:ALLOCATE", text)
        import re
        import yaml

        document = yaml.safe_load(re.search(r"```yaml\n(.*?)```", text, re.S).group(1))["flow"]
        document["summary"]["text"] = "Batch đêm phân bổ đơn."
        document["steps"][0].update({"name": "Phân bổ", "text": "Gọi [[allocate]] rồi [[NoSuchThing]].",
                                     "rules": [{"title": "Chỉ phân bổ đơn mở", "status": "ok"}]})
        result = self.write("codemap_propose_flow_doc", {"flow_id": "flow:job:DAILY:ALLOCATE", "document": document})
        message = result["content"][0]["text"]
        self.assertIn("Wrote 2 part(s)", message)
        self.assertIn("NoSuchThing", message)
        self.assertIn("Chỉ phân bổ đơn mở", message)
        with closing(sqlite3.connect(self.knowledge)) as db:
            stored = json.loads(db.execute("SELECT doc_json FROM biz_flows").fetchone()[0])
        self.assertEqual(stored["summary"]["status"], "ai")
        self.assertEqual(stored["proposed_by"], "copilot:an")


class McpHttpTests(McpFixture):
    """The same server over HTTP on the web app."""

    def setUp(self) -> None:
        super().setUp()
        import web.backend.api.app as serving

        self.serving = serving
        self.previous = (serving.GRAPH_OUTPUT, serving.KNOWLEDGE_DB)
        serving.GRAPH_OUTPUT, serving.KNOWLEDGE_DB = self.output, self.knowledge
        self.client = serving.app.test_client()

    def tearDown(self) -> None:
        self.serving.GRAPH_OUTPUT, self.serving.KNOWLEDGE_DB = self.previous
        super().tearDown()

    def post(self, message, **headers):
        return self.client.post("/mcp", data=json.dumps(message), content_type="application/json", headers=headers)

    def test_http_round_trip(self) -> None:
        response = self.post(rpc("tools/call", {"name": "codemap_search", "arguments": {"question": "ORDER_HEADER"}}),
                             **{"X-Code-Map-User": "an"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("ORDER_HEADER", response.get_json()["result"]["content"][0]["text"])
        with closing(sqlite3.connect(self.knowledge)) as db:
            self.assertEqual(db.execute("SELECT user_name FROM mcp_calls").fetchall(), [("an",)])
        self.assertEqual(self.post(rpc("notifications/initialized", request_id=None)).status_code, 202)
        self.assertEqual(self.client.get("/mcp").status_code, 405)
        batch = self.post([rpc("ping", request_id=1), rpc("ping", request_id=2)]).get_json()
        self.assertEqual([reply["id"] for reply in batch], [1, 2])

    def test_foreign_origin_is_rejected(self) -> None:
        response = self.post(rpc("ping"), Origin="https://evil.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(rpc("ping"), Origin="http://localhost:5173").status_code, 200)

    def test_write_tools_need_the_token(self) -> None:
        listed = self.post(rpc("tools/list")).get_json()["result"]["tools"]
        token = os.environ["CODE_MAP_KNOWLEDGE_TOKEN"]
        with_token = self.post(rpc("tools/list"), **{"X-Code-Map-Token": token}).get_json()["result"]["tools"]
        self.assertGreaterEqual(len(with_token), len(listed))


if __name__ == "__main__":
    unittest.main()
