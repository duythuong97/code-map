"""Shared test data: a small engine graph.sqlite and its inputData.

Used by extractor/tests and web/tests so both read the same graph.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

# A semantic tree big enough (> 1 KB) to be kept out of codemap-web.sqlite.
SEMANTIC_PROPERTIES = {"route": "x", "semantic_tree": {"version": 3, "label": "Cancel order", "steps": [{"label": "x" * 1500}]}}

ENGINE_SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT);
CREATE TABLE nodes(node_id INTEGER PRIMARY KEY, stable_id TEXT, node_type TEXT, technical_name TEXT, qualified_name TEXT,
  default_display_name TEXT, system_key TEXT, database_key TEXT, repository_key TEXT, graph_role TEXT, confidence REAL,
  properties_json TEXT, package_key TEXT);
CREATE TABLE edges(edge_id INTEGER PRIMARY KEY, stable_id TEXT, source_node_id INTEGER, target_node_id INTEGER, edge_type TEXT,
  graph_layer TEXT, raw_operation TEXT, confidence REAL, properties_json TEXT, package_key TEXT);
CREATE TABLE evidence(evidence_id INTEGER PRIMARY KEY, stable_id TEXT, target_type TEXT, target_id INTEGER, source_path TEXT,
  start_line INTEGER, end_line INTEGER, start_column INTEGER, end_column INTEGER, evidence_kind TEXT, extractor_name TEXT,
  confidence REAL, snippet TEXT, properties_json TEXT, package_key TEXT);
CREATE TABLE comments(comment_id INTEGER PRIMARY KEY, stable_id TEXT, source_path TEXT, owner_node_id INTEGER, comment_kind TEXT,
  start_line INTEGER, end_line INTEGER, start_column INTEGER, end_column INTEGER, raw_text TEXT, normalized_text TEXT,
  language TEXT, encoding TEXT, properties_json TEXT, package_key TEXT);
CREATE TABLE issues(issue_id INTEGER PRIMARY KEY, stable_id TEXT, issue_type TEXT, severity TEXT, source_node_id INTEGER,
  raw_reference TEXT, database_key TEXT, source_path TEXT, start_line INTEGER, message TEXT, properties_json TEXT, package_key TEXT);
CREATE TABLE flows(flow_id INTEGER PRIMARY KEY, stable_id TEXT, flow_type TEXT, start_node_id INTEGER, target_node_id INTEGER,
  confidence REAL, complete INTEGER, properties_json TEXT);
CREATE TABLE flow_steps(flow_id INTEGER, step_index INTEGER, node_id INTEGER, incoming_edge_id INTEGER);
"""

# Engine ids are 63-bit hashes; codemap.sqlite must renumber them for JavaScript.
BIG = 7_000_000_000_000_000_000


def engine_graph(path: Path) -> None:
    """A small engine graph.sqlite: DELETE /api/orders/{id} -> CancelOrder -UPDATE-> ORDER_HEADER."""
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ENGINE_SCHEMA)
        db.execute("INSERT INTO metadata VALUES('source_name', ?)", (json.dumps("demo"),))
        nodes = [
            (BIG + 1, "api-operation:a:DELETE:/api/orders/{id}", "API_OPERATION", "DELETE /api/orders/{id}", "a/Orders.cs.DELETE"),
            (BIG + 2, "method:a:Orders.CancelOrder", "METHOD", "CancelOrder", "a/Orders.cs.OrderService.CancelOrder"),
            (BIG + 3, "table:DB1:ORDER_HEADER", "TABLE", "ORDER_HEADER", "DB1.APP.ORDER_HEADER"),
            (BIG + 4, "column:DB1:ORDER_HEADER:STATUS", "COLUMN", "STATUS", "DB1.APP.ORDER_HEADER.STATUS"),
            (BIG + 5, "executable:batch:order-fulfillment.exe", "EXECUTABLE", "order-fulfillment.exe", "order-fulfillment.exe"),
            (BIG + 6, "command-mode:order-fulfillment:allocate", "COMMAND_MODE", "allocate", "order-fulfillment.allocate"),
        ]
        for node_id, stable, node_type, name, qualified in nodes:
            properties = SEMANTIC_PROPERTIES if name == "CancelOrder" else {"table": "ORDER_HEADER"} if name == "STATUS" else {}
            db.execute(
                "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, stable, node_type, name, qualified, name, "sys", "DB1", "repo", "MAIN", 1.0,
                 json.dumps(properties), "sources/a"),
            )
        db.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?)", [
            (BIG + 10, "e1", BIG + 1, BIG + 2, "HANDLES_API", "TECHNICAL", "", 1.0, "{}", "sources/a"),
            (BIG + 11, "e2", BIG + 2, BIG + 3, "WRITES_TO", "TECHNICAL", "UPDATE", 1.0, "{}", "sources/a"),
            (BIG + 12, "e3", BIG + 3, BIG + 4, "CONTAINS", "STRUCTURAL", "", 1.0, "{}", "global"),
        ])
        db.executemany("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (20, "ev1", "EDGE", BIG + 11, "a/Orders.cs", 42, 42, 1, 10, "SQL", "dotnet", 1.0, "UPDATE ORDER_HEADER SET STATUS='C'", "{}", "a"),
            (21, "ev2", "NODE", BIG + 2, "a/Orders.cs", 40, 40, 1, 10, "DECLARATION", "dotnet", 1.0, "void CancelOrder()", "{}", "a"),
        ])
        db.execute(
            "INSERT INTO comments VALUES(1,'c1','a/Orders.cs',?,'LINE',39,39,1,10,'// x','Cancelled orders cannot ship','en','utf-8','{}','a')",
            (BIG + 2,),
        )
        db.execute(
            "INSERT INTO issues VALUES(30,'i1','DYNAMIC_SQL','WARNING',?,'x','DB1','a/Orders.cs',42,'dynamic','{}','a')", (BIG + 2,)
        )
        db.execute("INSERT INTO flows VALUES(?,?,?,?,?,1.0,1,'{}')", (BIG + 40, "f1", "API", BIG + 1, BIG + 3))
        db.executemany("INSERT INTO flow_steps VALUES(?,?,?,?)", [
            (BIG + 40, 0, BIG + 1, None), (BIG + 40, 1, BIG + 2, BIG + 10), (BIG + 40, 2, BIG + 3, BIG + 11),
        ])


def business_inputs(root: Path) -> None:
    """inputData: table names, a semantic dictionary entry and a jobnet."""
    (root / "tables").mkdir(parents=True)
    (root / "tables.csv").write_text("database,table_code,table_name_ja,table_name_en\nORDER_DB,ORDER_HEADER,注文,Order Header\n", encoding="utf-8")
    (root / "tables" / "ORDER_HEADER.csv").write_text("column_code,column_name_ja,column_name_en\nSTATUS,ステータス,Status\n", encoding="utf-8")
    (root / "semantic-dictionary.csv").write_text(
        "semantic_id,domain,locale,label,definition,aliases,status\n"
        "semantic:orders:cancel-order,orders,vi,Hủy đơn hàng,Hủy đơn hàng hiện có,CANCEL_ORDER|DELETE order,approved\n",
        encoding="utf-8",
    )
    (root / "jobnet.csv").write_text(
        "jobnet_id,jobnet_name,job_id,job_name,executable_name,arguments,predecessor_job_id\n"
        "DAILY,Daily,ALLOCATE,Allocate orders,OrderFulfillment.exe,--mode allocate,\n"
        "DAILY,Daily,EXPORT,Partner export,PartnerExport.exe,,ALLOCATE\n",
        encoding="utf-8",
    )
    (root / "executable-mappings.csv").write_text(
        "job_system,executable_name,executable_scope,canonical_executable_name,alias\n"
        "batch,OrderFulfillment.exe,batch,orderfulfillment.exe,order-fulfillment\n",
        encoding="utf-8",
    )
