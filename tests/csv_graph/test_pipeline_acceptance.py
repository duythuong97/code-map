from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from flask import Flask

from application.backend.importer.package_validator import CSV_HEADERS, validate_package
from application.backend.importer.pipeline import import_authoritative, import_roots, initialize, materialize, resolve, validation_ids
from contract.graph_contract import canonical_edge_id

ROOT=Path(__file__).resolve().parents[2]
PACKAGES=sorted((
    ROOT/"output/angular/customer-web",
    ROOT/"output/dotnet-api/order-api",
    ROOT/"output/batch/order-fulfillment",
    ROOT/"output/plsql/order-db",
    ROOT/"output/sql-files/order-ops",
))

def _rewrite_manifest(package_root: Path) -> None:
    manifest=json.loads((package_root/'manifest.json').read_text(encoding='utf-8'))
    for name in ('nodes','edges','evidence','issues'):
        path=package_root/f'{name}.csv'
        rows=list(path.read_bytes().splitlines())
        if manifest.get('contractVersion')=='1.0':
            manifest['statistics'][name]=max(0,len(rows)-1)
            manifest.setdefault('checksums',{})[path.name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}
        else:
            manifest['counts'][name]=max(0,len(rows)-1)
            manifest['files'][path.name]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest['files'][path.name]['bytes']=path.stat().st_size
    (package_root/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8')

def _write_csv(path: Path, name: str, rows: list[dict[str, str]]) -> None:
    with path.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=CSV_HEADERS[name],lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)

def _write_minimal_order_api_replacement(package_root: Path) -> None:
    package_root.mkdir(parents=True,exist_ok=True)
    original=ROOT/'output/dotnet-api/order-api'
    removed_node='dotnet-solution:order-api:OrderApi.sln'
    removed_edge='edge:aeedbc3e29fc793b7cac7b577bff9baad9d892b8d88c97ca995bfd854988f339'
    data={}
    for name in CSV_HEADERS:
        with (original/f'{name}.csv').open(encoding='utf-8',newline='') as handle:
            data[name]=list(csv.DictReader(handle))
    data['nodes']=[row for row in data['nodes'] if row['node_id']!=removed_node]
    data['edges']=[row for row in data['edges'] if row['edge_id']!=removed_edge]
    data['evidence']=[row for row in data['evidence'] if row['evidence_id']!='ev:create-api']
    data['issues']=[row for row in data['issues'] if row['issue_id']!='issue:table-not-imported:1']
    data['nodes'].append({
            'node_id':'service:order-api:ReplacementProbe','node_type':'SERVICE','technical_name':'ReplacementProbe',
            'qualified_name':'order-api.ReplacementProbe','default_display_name':'Replacement Probe','system_key':'order-system',
            'database_key':'ORDER_DB','repository_key':'order-api','graph_role':'TECHNICAL','confidence':'1.0','properties_json':'{}'
        })
    manifest={
        'contractVersion':'1.0',
        'extractor':{'name':'dotnet-api-extractor','version':'1.0.0'},
        'source':{'sourceKey':'demo:dotnet-api/order-api','repositoryKey':'order-api','revision':'replacement-fixture'},
        'generatedAt':'2026-07-20T10:00:00+07:00',
        'files':{'nodes':'nodes.csv','edges':'edges.csv','evidence':'evidence.csv','issues':'issues.csv'},
        'statistics':{'filesScanned':1},
        'checksums':{},
        'metadata':{'fixture':'source-replacement-test'}
    }
    for name, rows in data.items():
        path=package_root/f'{name}.csv'; _write_csv(path,name,rows)
        manifest['statistics'][name]=len(rows)
        manifest['checksums'][path.name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}
    (package_root/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8')

class PipelineAcceptance(unittest.TestCase):
    def setUp(self):
        self.maxDiff=None
        self.tmp=tempfile.TemporaryDirectory(); self.db=Path(self.tmp.name)/"graph.sqlite"
        self.counts=import_roots(PACKAGES,self.db,ROOT/"input-data")
        self.conn=sqlite3.connect(self.db)
        self.conn.row_factory=sqlite3.Row
    def tearDown(self): self.conn.close(); self.tmp.cleanup()
    def _reopen(self):
        self.conn.close(); self.conn=sqlite3.connect(self.db); self.conn.row_factory=sqlite3.Row
    def _table_counts(self):
        return {name:self.conn.execute(f"SELECT count(*) FROM graph_{name}").fetchone()[0] for name in ('sources','nodes','edges','evidence','issues','paths')}
    def _source_rows(self):
        return [tuple(row) for row in self.conn.execute("SELECT source_id,package_id,created_at FROM graph_sources ORDER BY source_id")]
    def _source_counts(self, table: str):
        return {row['source_id']:row['count'] for row in self.conn.execute(f"SELECT source_id,count(*) AS count FROM {table} GROUP BY source_id ORDER BY source_id")}
    def _path_counts(self, table_node_id: str):
        rows=self.conn.execute("""
            SELECT actor_node_id,operation,count(*) AS count
            FROM graph_paths WHERE table_node_id=?
            GROUP BY actor_node_id,operation ORDER BY actor_node_id,operation
        """,(table_node_id,)).fetchall()
        return {(row['actor_node_id'],row['operation']):row['count'] for row in rows}
    def test_uc_01_10_demo_journeys_have_write_paths(self):
        expected={
            "screen:customer-web:/orders/new": {"table:ORDER_DB:ORDER_HEADER","table:ORDER_DB:ORDER_LINE","table:ORDER_DB:PAYMENT_TRANSACTION","table:ORDER_DB:INVENTORY_RESERVATION"},
            "api-operation:order-api:GET:/api/orders/{id}": {"table:ORDER_DB:ORDER_HEADER","table:ORDER_DB:ORDER_LINE"},
            "api-operation:order-api:DELETE:/api/orders/{id}": {"table:ORDER_DB:ORDER_HEADER","table:ORDER_DB:INVENTORY_RESERVATION"},
            "executable:batch-system:orderfulfillment.exe": {"table:ORDER_DB:FULFILLMENT_ALLOCATION","table:ORDER_DB:BACKORDER"},
            "screen:warehouse-web:/shipments/{id}": {"table:ORDER_DB:PAYMENT_TRANSACTION"},
            "sql-file:order-ops:sql/reconcile_orders.sql": {"table:ORDER_DB:ORDER_DAILY_SUMMARY"},
        }
        for actor, targets in expected.items():
            actual={row["table_node_id"] for row in self.conn.execute("SELECT table_node_id FROM graph_paths WHERE actor_node_id=?",(actor,))}
            self.assertTrue(targets <= actual, f"{actor} missing {targets-actual}")
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_edges WHERE source_node_id='table:ORDER_DB:SHIPMENT' AND edge_type='TRIGGERS' AND target_node_id='trigger:ORDER_DB:TRG_SHIPMENT_AUDIT'").fetchone())
    def test_uc_11_12_readers_and_writers(self):
        expected={
            ('api-operation:order-api:DELETE:/api/orders/{id}','R'):1,
            ('api-operation:order-api:DELETE:/api/orders/{id}','W'):2,
            ('api-operation:order-api:GET:/api/orders/{id}','R'):1,
            ('api-operation:order-api:GET:/api/orders/{id}','W'):2,
            ('api-operation:order-api:POST:/api/orders','R'):1,
            ('api-operation:order-api:POST:/api/orders','W'):2,
            ('api-operation:order-api:POST:/api/shipments/{id}/confirm','R'):1,
            ('api-operation:order-api:POST:/api/shipments/{id}/confirm','W'):2,
            ('executable:batch-system:orderfulfillment.exe','R'):1,
            ('executable:batch-system:orderfulfillment.exe','W'):1,
            ('job:batch-system:ORDER_FULFILLMENT:ALLOCATE_ORDERS','R'):1,
            ('job:batch-system:ORDER_FULFILLMENT:ALLOCATE_ORDERS','W'):1,
            ('job:batch-system:ORDER_FULFILLMENT:CREATE_SHIPMENTS','R'):1,
            ('job:batch-system:ORDER_FULFILLMENT:CREATE_SHIPMENTS','W'):1,
            ('procedure:ORDER_DB:PKG_ORDER:ALLOCATE_ORDER:NUMBER','R'):1,
            ('procedure:ORDER_DB:PKG_ORDER:ALLOCATE_ORDER:NUMBER','W'):1,
            ('procedure:ORDER_DB:PKG_ORDER:CANCEL_ORDER:NUMBER','W'):1,
            ('procedure:ORDER_DB:PKG_ORDER:CREATE_ORDER:NUMBER_VARCHAR2','W'):1,
            ('screen:customer-web:/orders/new','R'):1,
            ('screen:customer-web:/orders/new','W'):2,
            ('screen:customer-web:/orders/{id}','R'):3,
            ('screen:customer-web:/orders/{id}','W'):6,
            ('screen:warehouse-web:/shipments/{id}','R'):1,
            ('screen:warehouse-web:/shipments/{id}','W'):2,
            ('sql-file:order-ops:sql/reconcile_orders.sql','R'):2,
            ('sql-file:order-ops:sql/reconcile_orders.sql','W'):1,
            ('ui-action:customer-web:cancel-order','R'):2,
            ('ui-action:customer-web:cancel-order','W'):4,
            ('ui-action:customer-web:create-order-submit','R'):2,
            ('ui-action:customer-web:create-order-submit','W'):4,
            ('ui-action:warehouse-web:confirm-shipment','R'):2,
            ('ui-action:warehouse-web:confirm-shipment','W'):4,
        }
        actual=self._path_counts('table:ORDER_DB:ORDER_HEADER')
        self.assertEqual(expected,actual)
        writers={actor for (actor, operation) in actual if operation=='W'}
        readers={actor for (actor, operation) in actual if operation=='R'}
        self.assertIn('api-operation:order-api:POST:/api/orders',writers)
        self.assertIn('executable:batch-system:orderfulfillment.exe',writers)
        self.assertIn('procedure:ORDER_DB:PKG_ORDER:CREATE_ORDER:NUMBER_VARCHAR2',writers)
        self.assertIn('sql-file:order-ops:sql/reconcile_orders.sql',writers)
        self.assertIn('screen:customer-web:/orders/new',readers)
        self.assertIn('job:batch-system:ORDER_FULFILLMENT:ALLOCATE_ORDERS',readers)
    def test_resolver_creates_api_resolution_and_calls_api_edges(self):
        expected={
            ('api-call:customer-web:POST:/order-api/api/orders','RESOLVES_TO','api-operation:order-api:POST:/api/orders'),
            ('screen:customer-web:/orders/new','CALLS_API','api-operation:order-api:POST:/api/orders'),
            ('ui-action:customer-web:create-order-submit','CALLS_API','api-operation:order-api:POST:/api/orders'),
            ('screen:warehouse-web:/shipments/{id}','CALLS_API','api-operation:order-api:POST:/api/shipments/{id}/confirm'),
            ('ui-action:warehouse-web:confirm-shipment','CALLS_API','api-operation:order-api:POST:/api/shipments/{id}/confirm'),
        }
        actual={tuple(row) for row in self.conn.execute("""
            SELECT source_node_id,edge_type,target_node_id
            FROM graph_edges
            WHERE source_id='resolver:cross-source' AND edge_type IN('RESOLVES_TO','CALLS_API')
        """)}
        self.assertTrue(expected <= actual)
    def test_uc_13_column_impact_authoritative(self):
        column=self.conn.execute("SELECT * FROM graph_nodes WHERE node_id='column:ORDER_DB:ORDER_HEADER:STATUS'").fetchone()
        self.assertIsNotNone(column)
        self.assertEqual('COLUMN',column['node_type'])
        self.assertTrue(self.conn.execute("""
            SELECT 1 FROM graph_edges
            WHERE source_node_id='table:ORDER_DB:ORDER_HEADER'
              AND target_node_id='column:ORDER_DB:ORDER_HEADER:STATUS'
              AND edge_type='CONTAINS'
              AND graph_layer='STRUCTURAL'
        """).fetchone())
    def test_uc_14_18_quality_cases(self):
        types={r[0] for r in self.conn.execute("SELECT issue_type FROM graph_issues")}
        self.assertTrue({'DYNAMIC_SQL','EXTERNAL_OBJECT','API_ROUTE_AMBIGUOUS','EXECUTABLE_NOT_MAPPED'}<=types)
    def test_uc_19_21_semantic_and_technical_preserved(self):
        layers={r[0] for r in self.conn.execute("SELECT graph_layer FROM graph_edges")}
        self.assertTrue({'STRUCTURAL','TECHNICAL','DATA_FLOW'}<=layers)
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_paths WHERE json_array_length(edge_path_json)>1").fetchone())
    def test_uc_22_25_34_bounded_graph_inputs_exist(self):
        self.assertGreater(self.counts['edges'],50); self.assertGreater(self.counts['paths'],50)
    def test_materializer_keeps_converging_distinct_paths(self):
        with closing(sqlite3.connect(Path(self.tmp.name)/"converge.sqlite")) as db:
            db.row_factory=sqlite3.Row; initialize(db)
            with db:
                db.execute("INSERT INTO graph_sources VALUES('s','p','now')")
                for node,node_type in (("actor","SCREEN"),("a","SERVICE"),("b","SERVICE"),("c","REPOSITORY"),("table:ORDER_DB:T","TABLE")):
                    db.execute("INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(node,node_type,node,node,node,"","","","MAIN",1,"{}","s"))
                for src,dst,etype in (("actor","a","CALLS"),("actor","b","CALLS"),("a","c","CALLS"),("b","c","CALLS"),("c","table:ORDER_DB:T","WRITES")):
                    eid=canonical_edge_id(src,etype,dst,"","TECHNICAL")
                    db.execute("INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)",(eid,src,dst,etype,"TECHNICAL","",1,"{}","s"))
                materialize(db)
                paths=db.execute("SELECT edge_path_json FROM graph_paths WHERE actor_node_id='actor' AND table_node_id='table:ORDER_DB:T'").fetchall()
                self.assertEqual(2,len(paths))
                self.assertEqual(2,len({tuple(json.loads(row["edge_path_json"])) for row in paths}))
    def test_resolver_uses_complete_executable_mapping_key(self):
        with closing(sqlite3.connect(Path(self.tmp.name)/"resolver.sqlite")) as db:
            db.row_factory=sqlite3.Row; initialize(db)
            with db:
                import_authoritative(db,ROOT/"input-data")
                db.execute("INSERT INTO graph_sources VALUES('pkg','pkg','now')")
                for node_id,name in (("executable:batch-system:orderfulfillment.exe","orderfulfillment.exe"),("executable:other:orderfulfillment.exe","other.exe")):
                    db.execute("INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(node_id,"EXECUTABLE",name,node_id,name,"","","","MAIN",1,"{}","pkg"))
                db.execute("INSERT OR REPLACE INTO graph_executable_mappings VALUES('other-system','OrderFulfillment.exe','batch-system','other.exe','bad')")
                resolve(db)
                rows=db.execute("SELECT target_node_id FROM graph_edges WHERE source_node_id='job:batch-system:ORDER_FULFILLMENT:ALLOCATE_ORDERS' AND edge_type='STARTS'").fetchall()
                self.assertEqual(["executable:batch-system:orderfulfillment.exe"],[row["target_node_id"] for row in rows])
    def test_uc_27_evidence_is_stably_orderable(self):
        ids=[r[0] for r in self.conn.execute("SELECT evidence_id FROM graph_evidence ORDER BY evidence_id")]
        self.assertEqual(ids,sorted(ids)); self.assertTrue(ids)
        edge_ids=[r[0] for r in self.conn.execute("SELECT evidence_id FROM graph_evidence WHERE target_id='edge:2c8e4f2d262bdf50a894027f46b0250e6c251c5602cd0087934e00ade1c8560d' ORDER BY evidence_id")]
        self.assertEqual(['ev:create-service','ev:idempotency'],edge_ids)
    def test_uc_28_paths_are_repository_relative(self):
        self.assertFalse(any(Path(r[0]).is_absolute() or '..' in Path(r[0]).parts for r in self.conn.execute("SELECT source_path FROM graph_evidence")))
    def test_uc_29_localization_fallback_data(self):
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_localization WHERE locale='ja'").fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_localization WHERE locale='en'").fetchone())
    def test_uc_30_knowledge_lifecycle_constraint(self):
        for status in ('draft','pending','approved','rejected'):
            self.conn.execute("INSERT INTO graph_knowledge(target_id,body,status) VALUES('x','b',?)",(status,))
        with self.assertRaises(sqlite3.IntegrityError): self.conn.execute("INSERT INTO graph_knowledge(target_id,body,status) VALUES('x','b','bad')")
    def test_uc_31_invalid_package_rollback(self):
        before_counts=self._table_counts()
        before_sources=self._source_rows()
        bad=Path(self.tmp.name)/"bad"; shutil.copytree(PACKAGES[0],bad)
        (bad/'nodes.csv').write_text('corrupt',encoding='utf-8')
        with self.assertRaises(ValueError): import_roots([bad],self.db,ROOT/'input-data')
        self._reopen()
        self.assertEqual(before_counts,self._table_counts())
        self.assertEqual(before_sources,self._source_rows())
    def test_uc_32_reimport_idempotent(self):
        before=dict(self.counts); after=import_roots(PACKAGES,self.db,ROOT/'input-data')
        self.assertEqual(before,after)
    def test_uc_32_source_replacement_preserves_other_source_evidence(self):
        before_node_sources=self._source_counts('graph_nodes')
        before_edge_sources=self._source_counts('graph_edges')
        before_evidence_sources=self._source_counts('graph_evidence')
        before_issue_sources=self._source_counts('graph_issues')
        plsql_evidence=self.conn.execute("SELECT count(*) FROM graph_evidence WHERE source_id='demo:plsql/order-db'").fetchone()[0]
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_nodes WHERE node_id='dotnet-solution:order-api:OrderApi.sln'").fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_edges WHERE edge_id='edge:aeedbc3e29fc793b7cac7b577bff9baad9d892b8d88c97ca995bfd854988f339'").fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_evidence WHERE evidence_id='ev:create-api'").fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_issues WHERE issue_id='issue:table-not-imported:1'").fetchone())
        replacement=Path(self.tmp.name)/"order-api-replacement"
        _write_minimal_order_api_replacement(replacement)
        import_roots([replacement],self.db,ROOT/'input-data')
        self._reopen()
        self.assertFalse(self.conn.execute("SELECT 1 FROM graph_nodes WHERE node_id='dotnet-solution:order-api:OrderApi.sln'").fetchone())
        self.assertFalse(self.conn.execute("SELECT 1 FROM graph_edges WHERE edge_id='edge:aeedbc3e29fc793b7cac7b577bff9baad9d892b8d88c97ca995bfd854988f339'").fetchone())
        self.assertFalse(self.conn.execute("SELECT 1 FROM graph_evidence WHERE evidence_id='ev:create-api'").fetchone())
        self.assertFalse(self.conn.execute("SELECT 1 FROM graph_issues WHERE issue_id='issue:table-not-imported:1'").fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_nodes WHERE node_id='service:order-api:ReplacementProbe' AND source_id='demo:dotnet-api/order-api'").fetchone())
        self.assertEqual(10,self.conn.execute("SELECT count(*) FROM graph_nodes WHERE source_id='demo:dotnet-api/order-api'").fetchone()[0])
        self.assertEqual(before_edge_sources['demo:dotnet-api/order-api']-1,self.conn.execute("SELECT count(*) FROM graph_edges WHERE source_id='demo:dotnet-api/order-api'").fetchone()[0])
        self.assertEqual(3,self.conn.execute("SELECT count(*) FROM graph_evidence WHERE source_id='demo:dotnet-api/order-api'").fetchone()[0])
        self.assertEqual(3,self.conn.execute("SELECT count(*) FROM graph_issues WHERE source_id='demo:dotnet-api/order-api'").fetchone()[0])
        self.assertEqual(plsql_evidence,self.conn.execute("SELECT count(*) FROM graph_evidence WHERE source_id='demo:plsql/order-db'").fetchone()[0])
        for source,count in before_evidence_sources.items():
            if source!='demo:dotnet-api/order-api':
                self.assertEqual(count,self._source_counts('graph_evidence')[source])
        self.assertEqual(before_node_sources['demo:dotnet-api/order-api'],10)
        self.assertGreater(before_edge_sources['demo:dotnet-api/order-api'],13)
        self.assertEqual(before_evidence_sources['demo:dotnet-api/order-api'],4)
        self.assertEqual(before_issue_sources['demo:dotnet-api/order-api'],4)
    def test_uc_33_external_unresolved_visible(self):
        self.assertTrue(self.conn.execute("SELECT 1 FROM graph_nodes WHERE node_type IN('EXTERNAL_SYSTEM','UNRESOLVED_REFERENCE')").fetchone())
    def test_m2_json_fk_wal(self):
        self.assertEqual('wal',self.conn.execute('PRAGMA journal_mode').fetchone()[0])
        self.assertEqual([],self.conn.execute('PRAGMA foreign_key_check').fetchall())
        self.conn.execute('PRAGMA foreign_keys=ON')
        with self.assertRaises(sqlite3.IntegrityError): self.conn.execute("INSERT INTO graph_edges VALUES('x','missing','missing','CALLS','TECHNICAL','',1,'{}','authoritative:input-data')")
        node_id=self.conn.execute("SELECT node_id FROM graph_nodes LIMIT 1").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError): self.conn.execute("UPDATE graph_nodes SET properties_json='bad' WHERE node_id=?",(node_id,))

class GraphApiAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=Path(self.tmp.name)/"api.sqlite"
        import_roots(PACKAGES,self.db,ROOT/"input-data")
        os.environ["CODE_MAP_KNOWLEDGE_TOKEN"]="test-token"
        from application.backend.api import graph_routes
        graph_routes.DB_PATH=self.db
        app=Flask(__name__); app.register_blueprint(graph_routes.bp)
        self.client=app.test_client()
    def tearDown(self):
        os.environ.pop("CODE_MAP_KNOWLEDGE_TOKEN",None); self.tmp.cleanup()
    def test_uc_22_34_one_hop_respects_max_nodes_and_truncates(self):
        response=self.client.get("/api/graph/flow",query_string={"node_id":"screen:customer-web:/orders/new","mode":"1","max_nodes":"1","semantic":"TECHNICAL"})
        self.assertEqual(200,response.status_code)
        payload=response.get_json()
        self.assertLessEqual(len(payload["nodes"]),1)
        self.assertTrue(payload["truncated"])
    def test_uc_23_25_semantic_modes_change_content(self):
        node="screen:customer-web:/orders/new"
        technical=self.client.get("/api/graph/flow",query_string={"node_id":node,"mode":"E","max_nodes":"80","semantic":"TECHNICAL"}).get_json()
        application=self.client.get("/api/graph/flow",query_string={"node_id":node,"mode":"E","max_nodes":"80","semantic":"APPLICATION"}).get_json()
        system=self.client.get("/api/graph/flow",query_string={"node_id":node,"mode":"E","max_nodes":"80","semantic":"SYSTEM"}).get_json()
        self.assertGreater(len(technical["edges"]),len(application["edges"]))
        self.assertTrue(all(edge["graph_layer"]!="STRUCTURAL" for edge in application["edges"]))
        self.assertTrue(all(edge["graph_layer"]=="DATA_FLOW" for edge in system["edges"]))
    def test_uc_25_read_write_modes_follow_terminal_paths_only(self):
        node="screen:customer-web:/orders/new"
        write=self.client.get("/api/graph/flow",query_string={"node_id":node,"mode":"W","direction":"out","max_nodes":"500","semantic_level":"TECHNICAL"})
        read=self.client.get("/api/graph/flow",query_string={"node_id":node,"mode":"R","direction":"out","max_nodes":"500","semantic_level":"TECHNICAL"})
        self.assertEqual(200,write.status_code)
        self.assertEqual(200,read.status_code)
        write_payload=write.get_json(); read_payload=read.get_json()
        write_types={edge["edge_type"] for edge in write_payload["edges"]}
        read_types={edge["edge_type"] for edge in read_payload["edges"]}
        self.assertTrue({"CALLS_API","HANDLED_BY","CALLS"} <= write_types)
        self.assertTrue({"INSERTS","UPDATES","DELETES","WRITES"} & write_types)
        self.assertTrue({"READS","REMOTE_READS"} & read_types)
        self.assertFalse(write_types & {"CONTAINS","BELONGS_TO","NAVIGATES_TO"})
        self.assertFalse(read_types & {"CONTAINS","BELONGS_TO","NAVIGATES_TO"})
        self.assertFalse(read_types & {"INSERTS","UPDATES","DELETES","MERGES","WRITES"})
        self.assertTrue(write_payload["paths"])
        self.assertTrue(read_payload["paths"])
    def test_uc_25_detail_exposes_only_canonical_table_flow_facts(self):
        node="screen:customer-web:/orders/new"
        paths=self.client.get("/api/graph/detail",query_string={"target_id":node,"target_type":"NODE"}).get_json()["paths"]
        self.assertTrue(paths)
        for path in paths:
            self.assertIn(path["terminal_edge_type"],{"READS","REMOTE_READS","INSERTS","UPDATES","DELETES","MERGES","WRITES"})
            self.assertIsInstance(path["terminal_raw_operation"],str)
            self.assertIsInstance(path["intermediate_path"],list)
            self.assertEqual(bool(path["terminal_raw_operation"] or any(edge["raw_operation"] or edge["properties"] for edge in path["intermediate_path"])),path["transformation_exact_available"])
    def test_uc_25_read_write_application_level_hides_technical_nodes(self):
        node="screen:customer-web:/orders/new"
        payload=self.client.get("/api/graph/flow",query_string={"node_id":node,"mode":"W","direction":"both","max_nodes":"500","semantic_level":"APPLICATION"}).get_json()
        self.assertTrue(payload["edges"])
        self.assertTrue(payload["paths"])
        self.assertTrue(all(item["graph_role"]=="MAIN" for item in payload["nodes"]))
        self.assertFalse({"CONTROLLER","SERVICE","REPOSITORY"} & {item["node_type"] for item in payload["nodes"]})
        self.assertIn("CALLS_API",{edge["edge_type"] for edge in payload["edges"]})
        self.assertTrue({"INSERTS","UPDATES","DELETES","WRITES"} & {edge["edge_type"] for edge in payload["edges"]})
    def test_uc_25_incoming_write_paths_preserve_exact_unload_units(self):
        payload=self.client.get("/api/graph/flow",query_string={"node_id":"table:ORDER_DB:ORDER_HEADER","mode":"W","direction":"in","max_nodes":"500","semantic_level":"TECHNICAL"}).get_json()
        edge_types={edge["edge_type"] for edge in payload["edges"]}
        node_ids={node["node_id"] for node in payload["nodes"]}
        self.assertIn("screen:customer-web:/orders/new",node_ids)
        self.assertTrue(edge_types & {"INSERTS","UPDATES","DELETES","WRITES"})
        self.assertFalse(edge_types & {"READS","REMOTE_READS","CONTAINS","BELONGS_TO","NAVIGATES_TO"})
        self.assertTrue(payload["paths"])
    def test_uc_25_invalid_mode_rejected(self):
        response=self.client.get("/api/graph/flow",query_string={"node_id":"screen:customer-web:/orders/new","mode":"bad"})
        self.assertEqual(400,response.status_code)
        self.assertEqual("invalid_request",response.get_json()["error"]["code"])
    def test_uc_28_snippet_allowlist_only_evidence_sources(self):
        ok=self.client.get("/api/graph/snippet",query_string={"path":"demo-sources/order-api/OrderFlow.cs"})
        self.assertEqual(200,ok.status_code)
        self.assertIn("OrderController",ok.get_json()["content"])
        blocked=self.client.get("/api/graph/snippet",query_string={"path":"README.md"})
        self.assertEqual(403,blocked.status_code)
    def test_uc_29_localization_ja_en_default_fallback(self):
        localized=self.client.get("/api/graph/localization",query_string={"locale":"ja","target_id":"table:ORDER_DB:ORDER_HEADER"}).get_json()
        self.assertEqual("注文",localized["items"]["table:ORDER_DB:ORDER_HEADER"]["name"])
        fallback=self.client.get("/api/graph/localization",query_string={"locale":"en","target_id":"table:ORDER_DB:BACKORDER"}).get_json()
        self.assertEqual("BACKORDER",fallback["items"]["table:ORDER_DB:BACKORDER"]["name"])
    def test_uc_13_column_impact_dedupes_parent_table_actors(self):
        payload=self.client.get("/api/graph/detail",query_string={"target_id":"column:ORDER_DB:ORDER_HEADER:STATUS","target_type":"NODE"}).get_json()
        self.assertEqual("COLUMN",payload["node"]["node_type"])
        self.assertEqual("table:ORDER_DB:ORDER_HEADER",payload["table_context"][0]["node_id"])
        pairs={(row["actor_node_id"],row["operation"]) for row in payload["impacts"]}
        self.assertEqual(len(pairs),len(payload["impacts"]))
        self.assertEqual(32,len(pairs))
        self.assertTrue({
            ("api-operation:order-api:POST:/api/orders","W"),
            ("procedure:ORDER_DB:PKG_ORDER:CREATE_ORDER:NUMBER_VARCHAR2","W"),
            ("screen:customer-web:/orders/new","R"),
            ("job:batch-system:ORDER_FULFILLMENT:ALLOCATE_ORDERS","R"),
        } <= pairs)
    def test_uc_27_evidence_pagination_cursor_is_stable(self):
        target="edge:2c8e4f2d262bdf50a894027f46b0250e6c251c5602cd0087934e00ade1c8560d"
        first=self.client.get("/api/graph/evidence",query_string={"target_id":target,"limit":"1"}).get_json()
        self.assertEqual(["ev:create-service"],[item["evidence_id"] for item in first["items"]])
        self.assertEqual(1,first["count"])
        self.assertTrue(first["truncated"])
        self.assertEqual("ev:create-service",first["cursor"])
        second=self.client.get("/api/graph/evidence",query_string={"target_id":target,"limit":"1","cursor":first["cursor"]}).get_json()
        self.assertEqual(["ev:idempotency"],[item["evidence_id"] for item in second["items"]])
        self.assertEqual(1,second["count"])
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["cursor"])
    def test_uc_30_knowledge_requires_token_status_and_existing_target(self):
        body={"target_id":"screen:customer-web:/orders/new","body":"review note","status":"pending"}
        self.assertEqual(403,self.client.post("/api/graph/knowledge",json=body).status_code)
        saved=self.client.post("/api/graph/knowledge",json=body,headers={"X-Code-Map-Token":"test-token"})
        self.assertEqual(201,saved.status_code)
        self.assertEqual("pending",saved.get_json()["status"])
        self.assertEqual(400,self.client.post("/api/graph/knowledge",json={**body,"status":"published"},headers={"X-Code-Map-Token":"test-token"}).status_code)
        self.assertEqual(400,self.client.post("/api/graph/knowledge",json={**body,"target_id":"missing"},headers={"X-Code-Map-Token":"test-token"}).status_code)
    def test_detail_endpoint_returns_table_columns_and_impact(self):
        payload=self.client.get("/api/graph/detail",query_string={"target_id":"table:ORDER_DB:ORDER_HEADER","target_type":"NODE"}).get_json()
        self.assertEqual("TABLE",payload["node"]["node_type"])
        self.assertIn("column:ORDER_DB:ORDER_HEADER:ORDER_ID",{row["node_id"] for row in payload["columns"]})
        self.assertGreater(payload["counts"]["relations"],0)
        self.assertGreater(payload["counts"]["impacts"],0)
        self.assertTrue({"R","W"} & {row["operation"] for row in payload["impacts"]})
    def test_detail_endpoint_returns_edge_trace_for_data_flow_edge(self):
        flow=self.client.get("/api/graph/flow",query_string={"node_id":"screen:customer-web:/orders/new","mode":"E","max_nodes":"80","semantic":"SYSTEM"}).get_json()
        edge=next(item for item in flow["edges"] if item["graph_layer"]=="DATA_FLOW")
        payload=self.client.get("/api/graph/detail",query_string={"target_id":edge["edge_id"],"target_type":"EDGE"}).get_json()
        self.assertEqual(edge["edge_id"],payload["edge"]["edge_id"])
        self.assertTrue(payload["source"])
        self.assertTrue(payload["target"])
        self.assertGreater(payload["counts"]["path_edges"],0)
    def test_edge_evidence_can_include_materialized_trace_evidence(self):
        selected=None
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory=sqlite3.Row
            for row in db.execute("SELECT edge_id,properties_json FROM graph_edges WHERE graph_layer='DATA_FLOW' ORDER BY edge_id"):
                edge_path=json.loads(row["properties_json"] or "{}").get("edge_path") or []
                if not edge_path:
                    continue
                placeholders=','.join('?'*len(edge_path))
                trace_count=db.execute(f"SELECT count(*) FROM graph_evidence WHERE target_id IN ({placeholders})",edge_path).fetchone()[0]
                if trace_count:
                    direct_count=db.execute("SELECT count(*) FROM graph_evidence WHERE target_id=?",(row["edge_id"],)).fetchone()[0]
                    selected=(row["edge_id"],edge_path,direct_count,trace_count)
                    break
        self.assertIsNotNone(selected)
        edge_id,edge_path,direct_count,trace_count=selected
        detail=self.client.get("/api/graph/detail",query_string={"target_id":edge_id,"target_type":"EDGE"}).get_json()
        self.assertEqual(direct_count,detail["counts"]["direct_evidence"])
        self.assertEqual(trace_count,detail["counts"]["trace_evidence"])
        self.assertEqual(direct_count+trace_count,detail["counts"]["related_evidence"])

        exact=self.client.get("/api/graph/evidence",query_string={"target_id":edge_id,"limit":"100"}).get_json()
        self.assertEqual("direct",exact["evidence_scope"])
        self.assertEqual(direct_count,exact["evidence_count"])

        related=self.client.get("/api/graph/evidence",query_string={"target_id":edge_id,"include_related":"trace","limit":"100"}).get_json()
        self.assertEqual("trace",related["evidence_scope"])
        self.assertEqual(direct_count,related["direct_evidence_count"])
        self.assertEqual(trace_count,related["trace_evidence_count"])
        self.assertEqual(direct_count+trace_count,related["evidence_count"])
        self.assertIn(edge_id,related["related_target_ids"])
        self.assertTrue(set(edge_path) <= set(related["related_target_ids"]))
        self.assertTrue({item["target_id"] for item in related["items"]} & set(edge_path))

if __name__=='__main__': unittest.main()
